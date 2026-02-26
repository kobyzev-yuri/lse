"""
LLM сервис для работы с GPT-4o и другими моделями через proxyapi.ru.
Поддержка сравнения нескольких моделей (LLM_COMPARE_MODELS).
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
import re
from openai import OpenAI

logger = logging.getLogger(__name__)

# Базовые URL ProxyAPI по провайдерам (см. proxyapi.ru/docs)
PROXYAPI_OPENAI_BASE = "https://api.proxyapi.ru/openai/v1"
PROXYAPI_ANTHROPIC_BASE = "https://api.proxyapi.ru/anthropic/v1"
PROXYAPI_GOOGLE_BASE = "https://api.proxyapi.ru/google/v1"


def load_config():
    """Загружает конфигурацию из локального config.env или ../brats/config.env"""
    from config_loader import load_config as load_config_base
    return load_config_base()


def parse_compare_models(config: Dict[str, str]) -> List[Tuple[str, str]]:
    """
    Парсит LLM_COMPARE_MODELS в список (base_url, model).
    Формат: через запятую, каждый элемент — "model" (используется OPENAI_BASE_URL) или "provider|model".
    provider: openai, anthropic, google (базовые URL из ProxyAPI).
    Пример: gpt-4o,openai|gpt-5.2,anthropic|claude-opus-4-6,google|gemini-3.1-pro-preview
    """
    raw = config.get("LLM_COMPARE_MODELS", "").strip()
    if not raw:
        return []
    base_default = config.get("OPENAI_BASE_URL", PROXYAPI_OPENAI_BASE)
    provider_bases = {
        "openai": PROXYAPI_OPENAI_BASE,
        "anthropic": PROXYAPI_ANTHROPIC_BASE,
        "google": PROXYAPI_GOOGLE_BASE,
    }
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            left, model = part.split("|", 1)
            left, model = left.strip(), model.strip()
            base = provider_bases.get(left.lower()) or left  # если не provider, считаем полным URL
            result.append((base, model))
        else:
            result.append((base_default, part))
    return result


class LLMService:
    """
    Сервис для работы с LLM (GPT-4o через proxyapi.ru)
    """
    
    def __init__(self):
        """
        Инициализация LLM сервиса
        """
        config = load_config()
        
        self.api_key = config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = config.get("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1")
        self.model = config.get("OPENAI_MODEL", "gpt-4o")
        self.temperature = float(config.get("OPENAI_TEMPERATURE", "0.2"))
        self.timeout = int(config.get("OPENAI_TIMEOUT", "60"))
        
        if not self.api_key:
            logger.warning("⚠️ OPENAI_API_KEY не настроен, LLM функции будут недоступны")
            self.client = None
        else:
            # Инициализируем OpenAI клиент
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout
            )
            logger.info(f"✅ LLMService инициализирован (model={self.model}, base_url={self.base_url})")
    
    def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Генерация ответа от LLM
        
        Args:
            messages: Список сообщений в формате OpenAI (role, content)
            system_prompt: Системный промпт
            **kwargs: Дополнительные параметры (temperature, max_tokens и т.д.)
            
        Returns:
            Ответ от LLM с метаданными
        """
        if not self.client:
            raise ValueError("LLMService не инициализирован (отсутствует OPENAI_API_KEY)")
        
        try:
            # Формируем список сообщений
            formatted_messages = []
            
            # Добавляем системный промпт
            if system_prompt:
                formatted_messages.append({
                    "role": "system",
                    "content": system_prompt
                })
            
            # Добавляем пользовательские сообщения
            formatted_messages.extend(messages)
            
            # Параметры запроса
            request_params = {
                "model": self.model,
                "messages": formatted_messages,
                "temperature": kwargs.get("temperature", self.temperature),
                "max_tokens": kwargs.get("max_tokens", 2000),
                "timeout": self.timeout
            }
            
            # Выполняем запрос
            logger.info(f"Отправка запроса к LLM (model={self.model}, messages={len(formatted_messages)})")
            
            response = self.client.chat.completions.create(**request_params)
            
            # Извлекаем ответ
            assistant_message = response.choices[0].message.content
            
            return {
                "response": assistant_message,
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens
                },
                "finish_reason": response.choices[0].finish_reason
            }
            
        except Exception as e:
            logger.error(f"Ошибка генерации ответа от LLM: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    def generate_response_with_model(
        self,
        base_url: str,
        model: str,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        Один запрос к указанному (base_url, model). Для сравнения моделей.
        Возвращает тот же формат что generate_response или None при ошибке.
        """
        if not self.api_key:
            return None
        try:
            client = OpenAI(
                api_key=self.api_key,
                base_url=base_url,
                timeout=kwargs.get("timeout", self.timeout),
            )
            formatted_messages = []
            if system_prompt:
                formatted_messages.append({"role": "system", "content": system_prompt})
            formatted_messages.extend(messages)
            response = client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=kwargs.get("temperature", self.temperature),
                max_tokens=kwargs.get("max_tokens", 2000),
            )
            msg = response.choices[0].message.content
            return {
                "response": msg,
                "model": getattr(response, "model", model),
                "usage": getattr(response, "usage", None) and {
                    "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                    "total_tokens": getattr(response.usage, "total_tokens", 0),
                } or {},
                "finish_reason": getattr(response.choices[0], "finish_reason", None),
            }
        except Exception as e:
            logger.warning("Сравнение моделей: ошибка для %s: %s", model, e)
            return None

    def analyze_trading_situation(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float
    ) -> Dict[str, Any]:
        """
        Анализ торговой ситуации с помощью LLM
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (цена, SMA, волатильность)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            
        Returns:
            Анализ от LLM с рекомендацией
        """
        system_prompt = """Ты опытный финансовый аналитик, специализирующийся на техническом анализе и анализе новостей.
Твоя задача - проанализировать торговую ситуацию и дать рекомендацию: BUY, STRONG_BUY или HOLD.

Учитывай:
1. Технические индикаторы (тренд, волатильность)
2. Sentiment новостей
3. Контекст новостей
4. Риски

Отвечай в формате JSON:
{
    "decision": "BUY|STRONG_BUY|HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "объяснение решения",
    "risks": ["список рисков"],
    "key_factors": ["ключевые факторы"]
}
"""
        
        # Формируем контекст
        news_summary = "\n".join([
            f"- {news.get('source', 'Unknown')}: {news.get('content', '')[:200]}... (sentiment: {news.get('sentiment_score', 0)})"
            for news in news_data[:5]  # Берем топ-5 новостей
        ])
        
        # Форматируем RSI с интерпретацией
        rsi_value = technical_data.get('rsi')
        rsi_text = ""
        if rsi_value is not None:
            if rsi_value >= 70:
                rsi_status = "перекупленность"
            elif rsi_value <= 30:
                rsi_status = "перепроданность"
            elif rsi_value >= 60:
                rsi_status = "близко к перекупленности"
            elif rsi_value <= 40:
                rsi_status = "близко к перепроданности"
            else:
                rsi_status = "нейтральная зона"
            rsi_text = f"\n- RSI: {rsi_value:.1f} ({rsi_status})"
        
        user_message = f"""Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {technical_data.get('close', 'N/A')}
- SMA_5: {technical_data.get('sma_5', 'N/A')}
- Волатильность (5 дней): {technical_data.get('volatility_5', 'N/A')}
- Средняя волатильность (20 дней): {technical_data.get('avg_volatility_20', 'N/A')}{rsi_text}
- Технический сигнал: {technical_data.get('technical_signal', 'N/A')}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len(news_data)}

Новости:
{news_summary if news_summary else "Новостей не найдено"}
"""
        premarket_note = technical_data.get("premarket_note")
        if premarket_note:
            user_message += f"\n\nКонтекст сессии:\n{premarket_note}\n\nУчти это при рекомендации входа (в премаркете ликвидность ниже)."
        user_message += "\n\nДай рекомендацию на основе этих данных."
        
        messages = [{"role": "user", "content": user_message}]
        
        try:
            result = self.generate_response(messages, system_prompt=system_prompt)
            response_text = result["response"]
            
            # Пытаемся распарсить JSON из ответа
            try:
                # Ищем JSON в ответе
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    analysis = json.loads(json_match.group())
                else:
                    # Если JSON не найден, создаем структуру из текста
                    analysis = {
                        "decision": "HOLD",
                        "confidence": 0.5,
                        "reasoning": response_text,
                        "risks": [],
                        "key_factors": []
                    }
            except json.JSONDecodeError:
                # Если не удалось распарсить, используем текст как reasoning
                analysis = {
                    "decision": "HOLD",
                    "confidence": 0.5,
                    "reasoning": response_text,
                    "risks": [],
                    "key_factors": []
                }
            
            return {
                "llm_analysis": analysis,
                "usage": result.get("usage", {})
            }
        except Exception as e:
            logger.error(f"Ошибка анализа через LLM: {e}")
            return {
                "llm_analysis": {
                    "decision": "HOLD",
                    "confidence": 0.0,
                    "reasoning": f"Ошибка анализа: {str(e)}",
                    "risks": ["Ошибка LLM анализа"],
                    "key_factors": []
                },
                "usage": {}
            }

    def fetch_news_for_ticker(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Прямой запрос к LLM: какие новости/события могут влиять на тикер (например SNDK).
        Используется как один из источников новостей наряду с RSS, NewsAPI, KB.

        Важно: LLM не выполняет поиск в интернете в реальном времени — только знание из обучения.
        Свежие breaking news (например шорт от Citron в день публикации) могут не попасть в ответ.
        Чтобы агент реагировал на такие новости, они должны быть уже в knowledge_base:
        cron fetch_news_cron (Investing.com News, NewsAPI, Alpha Vantage и т.д.) и/или
        разовая вставка через scripts/add_manual_news.py.

        Returns:
            dict с ключами content, sentiment_score (0–1 или None), insight, source_label
            или None при ошибке / отсутствии API key.
        """
        if not self.client:
            logger.warning("LLM не инициализирован, пропуск fetch_news_for_ticker")
            return None

        ticker_upper = ticker.upper()
        name_hint = {"SNDK": "Western Digital / SanDisk, память, полупроводники"}.get(
            ticker_upper, ticker_upper
        )

        system_prompt = """Ты финансовый обзор. Задача: кратко перечисли последние новости и события, которые могут влиять на цену указанной бумаги в ближайшие дни.
Формат ответа:
1. Краткий список фактов (дата/источник по возможности, 2–5 пунктов).
2. Строка "SENTIMENT: положительный|негативный|нейтральный" — общая тональность для цены.
3. Строка "INSIGHT: один короткий вывод в одну фразу."
Пиши по-русски, без лишнего вступления."""

        user_message = f"""Какие последние новости и события могут влиять на {ticker_upper} ({name_hint})? Укажи только релевантные факты за последние 1–2 недели."""

        def _parse_llm_news_text(text: str) -> tuple:
            sentiment_score = None
            insight = None
            for line in (text or "").split("\n"):
                line = line.strip()
                if line.upper().startswith("SENTIMENT:"):
                    part = line.split(":", 1)[-1].strip().lower()
                    if "положительн" in part or "позитив" in part:
                        sentiment_score = 0.65
                    elif "негатив" in part or "негативн" in part:
                        sentiment_score = 0.35
                    else:
                        sentiment_score = 0.5
                elif line.upper().startswith("INSIGHT:"):
                    insight = line.split(":", 1)[-1].strip()[:500]
            return sentiment_score, insight

        try:
            result = self.generate_response(
                [{"role": "user", "content": user_message}],
                system_prompt=system_prompt,
                max_tokens=800,
            )
            text = result.get("response", "").strip()
            if not text:
                return None

            sentiment_score, insight = _parse_llm_news_text(text)
            content = text
            source_label = f"LLM ({self.model})"
            out = {
                "content": content[:8000],
                "sentiment_score": sentiment_score,
                "insight": insight,
                "source_label": source_label,
            }

            # Сравнение с другими моделями (LLM_COMPARE_MODELS)
            config = load_config()
            compare_list = parse_compare_models(config)
            if compare_list:
                llm_comparison = []
                for base_url, model in compare_list:
                    if base_url == self.base_url and model == self.model:
                        llm_comparison.append({
                            "model": model,
                            "content": content[:2000],
                            "sentiment_score": sentiment_score,
                            "insight": insight,
                            "source": "primary",
                        })
                        continue
                    res = self.generate_response_with_model(
                        base_url, model,
                        [{"role": "user", "content": user_message}],
                        system_prompt=system_prompt,
                        max_tokens=800,
                    )
                    if not res:
                        llm_comparison.append({"model": model, "error": "no response"})
                        continue
                    text_other = (res.get("response") or "").strip()
                    sent_other, insight_other = _parse_llm_news_text(text_other)
                    llm_comparison.append({
                        "model": model,
                        "content": text_other[:2000],
                        "sentiment_score": sent_other,
                        "insight": insight_other,
                    })
                out["llm_comparison"] = llm_comparison

            return out
        except Exception as e:
            logger.exception("Ошибка fetch_news_for_ticker %s: %s", ticker, e)
            return None


# Глобальный экземпляр сервиса
_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """
    Получить глобальный экземпляр LLM сервиса
    """
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service

