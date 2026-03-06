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

    @staticmethod
    def get_entry_decision_system_prompt() -> str:
        """Системный промпт для принятия решения о входе (BUY/STRONG_BUY/HOLD). Единый источник текста."""
        return """Ты опытный финансовый аналитик, специализирующийся на техническом анализе и анализе новостей.
Твоя задача - проанализировать торговую ситуацию и дать рекомендацию: BUY, STRONG_BUY или HOLD.

Учитывай:
1. Технические индикаторы (тренд, волатильность)
2. Sentiment новостей
3. Контекст новостей
4. Риски
5. Геополитический риск: при существенных геополитических изменениях (эскалация, военные действия, удары, санкции, risk-off) — предпочтительнее HOLD и в reasoning/risks укажи рекомендацию рассмотреть превентивный выход по открытым позициям даже с небольшой потерей (история: удержание через такое событие часто ведёт к большим потерям). Одновременно учитывай прогнозы о деэскалации или стабилизации: если в новостях звучит снижение напряжённости, переговоры, «рынок учёл», адаптация — риски могут быть терпимы, не исключай вход или удержание, когда рынок уже адаптировался, чтобы не оставаться вне игры без необходимости.

Отвечай в формате JSON:
{
    "decision": "BUY|STRONG_BUY|HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "объяснение решения",
    "risks": ["список рисков"],
    "key_factors": ["ключевые факторы"]
}
"""

    @staticmethod
    def get_entry_decision_prompt_template() -> Dict[str, str]:
        """Шаблон промпта для решения о входе: system + user (с плейсхолдерами). Для отображения по /prompt_entry."""
        return {
            "system": LLMService.get_entry_decision_system_prompt(),
            "user_template": """Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {close}
- SMA_5: {sma_5}
- Волатильность (5 дней): {volatility_5}
- Средняя волатильность (20 дней): {avg_volatility_20}
- RSI: {rsi} ({rsi_status})
- Технический сигнал: {technical_signal}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len_news}

Новости:
{news_summary}

(Опционально: Контекст сессии: {premarket_note} — учти при рекомендации входа, в премаркете ликвидность ниже.)
(Опционально: Контекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Учти при итоговой рекомендации.)

Дай рекомендацию на основе этих данных.""",
        }

    @staticmethod
    def build_entry_prompt(
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float,
        strategy_name: Optional[str] = None,
        strategy_signal: Optional[str] = None,
        strategy_outcome_stats: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Собирает промпт (system + user) для решения о входе без вызова LLM.
        Те же входы, что у analyze_trading_situation. Для /prompt_entry: всегда заполнять user_prompt в JSON.
        """
        system_prompt = LLMService.get_entry_decision_system_prompt()
        news_summary = "\n".join([
            f"- {news.get('source', 'Unknown')}: {news.get('content', '')[:200]}... (sentiment: {news.get('sentiment_score', 0)})"
            for news in (news_data or [])[:5]
        ])
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
- Количество новостей: {len(news_data or [])}

Новости:
{news_summary if news_summary else "Новостей не найдено"}
"""
        premarket_note = technical_data.get("premarket_note")
        if premarket_note:
            user_message += f"\n\nКонтекст сессии:\n{premarket_note}\n\nУчти это при рекомендации входа (в премаркете ликвидность ниже)."
        if strategy_name and strategy_signal:
            user_message += f"\n\nКонтекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Учти при итоговой рекомендации (можешь согласиться или скорректировать)."
        if strategy_outcome_stats:
            user_message += f"\n\n{strategy_outcome_stats} Учти исходы в похожих ситуациях при рекомендации."
        _geo_keywords = ("israel", "iran", "escalat", "war", "strike", "middle east", "geopolit", "военн", "эскалац", "конфликт")
        _news_text = (news_summary or "").lower()
        if any(kw in _news_text for kw in _geo_keywords):
            _deesc_keywords = ("de-escalat", "deescalat", "stabiliz", "calm", "market priced", "уже учт", "стабилизац", "снижени", "переговор")
            _has_deesc = any(d in _news_text for d in _deesc_keywords)
            user_message += "\n\nВ новостях за период есть упоминания геополитической эскалации или военного конфликта."
            if _has_deesc:
                user_message += " Вместе с тем в новостях звучат деэскалация или стабилизация — учти: риски могут быть терпимы, адаптация рынка могла произойти; не исключай вход или удержание без необходимости."
            else:
                user_message += " Учти при рекомендации: при открытых позициях рассмотреть превентивный выход даже с небольшой потерей."
        user_message += "\n\nДай рекомендацию на основе этих данных."
        return {"prompt_system": system_prompt, "prompt_user": user_message}

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
        sentiment_score: float,
        strategy_name: Optional[str] = None,
        strategy_signal: Optional[str] = None,
        strategy_outcome_stats: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Анализ торговой ситуации с помощью LLM
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (цена, SMA, волатильность)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment score
            strategy_name: Имя выбранной стратегии (Momentum, Mean Reversion и т.д.), если есть
            strategy_signal: Сигнал стратегии (BUY/HOLD/SELL), если есть
            strategy_outcome_stats: Текст со статистикой исходов по стратегиям (закрытые сделки), если есть
            
        Returns:
            Анализ от LLM с рекомендацией
        """
        system_prompt = self.get_entry_decision_system_prompt()
        
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
        if strategy_name and strategy_signal:
            user_message += f"\n\nКонтекст стратегии: выбранная стратегия — {strategy_name}, её сигнал — {strategy_signal}. Учти при итоговой рекомендации (можешь согласиться или скорректировать)."
        if strategy_outcome_stats:
            user_message += f"\n\n{strategy_outcome_stats} Учти исходы в похожих ситуациях при рекомендации."
        # При намёках на геополитику в новостях — явно подсказываем учесть выход по открытым позициям
        _geo_keywords = ("israel", "iran", "escalat", "war", "strike", "middle east", "geopolit", "военн", "эскалац", "конфликт")
        _news_text = (news_summary or "").lower()
        if any(kw in _news_text for kw in _geo_keywords):
            _deesc_keywords = ("de-escalat", "deescalat", "stabiliz", "calm", "market priced", "уже учт", "стабилизац", "снижени", "переговор")
            _has_deesc = any(d in _news_text for d in _deesc_keywords)
            user_message += "\n\nВ новостях за период есть упоминания геополитической эскалации или военного конфликта."
            if _has_deesc:
                user_message += " Вместе с тем в новостях звучат деэскалация или стабилизация — учти: риски могут быть терпимы, адаптация рынка могла произойти; не исключай вход или удержание без необходимости."
            else:
                user_message += " Учти при рекомендации: при открытых позициях рассмотреть превентивный выход даже с небольшой потерей."
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
                "usage": result.get("usage", {}),
                "prompt_system": system_prompt,
                "prompt_user": user_message,
                "llm_response_raw": response_text,
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
                "usage": {},
                "prompt_system": system_prompt,
                "prompt_user": user_message,
                "llm_response_raw": "",
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

    def generate_news_by_topic(
        self, topic: str, tickers: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Генерация группы новостей по заданной теме для ручного ввода в базу знаний.
        LLM возвращает JSON-массив записей с полями ticker, content, sentiment_score, insight, source.
        tickers: список тикеров, по которым нужно сформировать записи (например GC=F, MSFT, AMD, MU, SNDK).
        """
        if not self.client:
            logger.warning("LLM не инициализирован, пропуск generate_news_by_topic")
            return []

        default_tickers = ["GC=F", "MSFT", "AMD", "MU", "SNDK"]
        ticker_list = tickers or default_tickers
        ticker_list_upper = [t.strip().upper() for t in ticker_list if t]
        if not ticker_list_upper:
            ticker_list_upper = [t.upper() for t in default_tickers]
        tickers_str = ", ".join(ticker_list)

        system_prompt = """Ты финансовый обзор. Задача: по заданной теме сформировать группу коротких новостных записей для базы знаний (по одной или несколько на тикер).
Отвечай строго в формате JSON — один массив объектов. Каждый объект:
{
  "ticker": "один из указанных тикеров",
  "content": "2-4 предложения: суть события/ожидания для рынка в связи с темой, релевантно тикеру",
  "sentiment_score": число от 0.0 до 1.0 (0.5 нейтрально, выше — позитив для цены, ниже — негатив),
  "insight": "одна короткая фраза-вывод для трейдера",
  "source": "краткое имя источника, например LLM (тема)"
}
Тикеры для использования: """ + tickers_str + """. Распредели тему по тикерам уместно (золото — GC=F, тех/полупроводники — MSFT, AMD, MU, SNDK). От 3 до 8 записей. Только JSON, без markdown и пояснений до/после."""

        user_message = f"""Тема: {topic}\n\nСформируй группу новостных записей в формате JSON (массив объектов с полями ticker, content, sentiment_score, insight, source)."""

        try:
            result = self.generate_response(
                [{"role": "user", "content": user_message}],
                system_prompt=system_prompt,
                max_tokens=2000,
            )
            text = (result.get("response") or "").strip()
            if not text:
                return []
            # Убрать markdown-обёртку если есть
            if "```json" in text:
                text = text.split("```json")[-1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            data = json.loads(text)
            if not isinstance(data, list):
                data = [data]
            out = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                ticker = (item.get("ticker") or "").strip().upper()
                if not ticker or ticker not in ticker_list_upper:
                    ticker = ticker_list_upper[0]
                content = (item.get("content") or "")[:8000]
                if not content:
                    continue
                try:
                    sent = float(item.get("sentiment_score", 0.5))
                    sent = max(0.0, min(1.0, sent))
                except (TypeError, ValueError):
                    sent = 0.5
                insight = (item.get("insight") or "")[:1000]
                source = (item.get("source") or f"LLM ({self.model})")[:200]
                out.append({
                    "ticker": ticker,
                    "content": content,
                    "sentiment_score": round(sent, 2),
                    "insight": insight,
                    "source": source,
                    "link": (item.get("link") or "")[:500],
                })
            return out
        except json.JSONDecodeError as e:
            logger.warning("generate_news_by_topic: не удалось распарсить JSON: %s", e)
            return []
        except Exception as e:
            logger.exception("Ошибка generate_news_by_topic: %s", e)
            return []


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

