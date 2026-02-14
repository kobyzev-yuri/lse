"""
LLM сервис для работы с GPT-4o через proxyapi.ru
Упрощенная версия для LSE Trading System
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
import re
from openai import OpenAI

logger = logging.getLogger(__name__)


def load_config():
    """Загружает конфигурацию из локального config.env или ../brats/config.env"""
    from config_loader import load_config as load_config_base
    return load_config_base()


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
        
        user_message = f"""Анализ для тикера {ticker}:

Технические данные:
- Текущая цена: {technical_data.get('close', 'N/A')}
- SMA_5: {technical_data.get('sma_5', 'N/A')}
- Волатильность (5 дней): {technical_data.get('volatility_5', 'N/A')}
- Средняя волатильность (20 дней): {technical_data.get('avg_volatility_20', 'N/A')}
- Технический сигнал: {technical_data.get('technical_signal', 'N/A')}

Sentiment анализ:
- Взвешенный sentiment: {sentiment_score:.3f}
- Количество новостей: {len(news_data)}

Новости:
{news_summary if news_summary else "Новостей не найдено"}

Дай рекомендацию на основе этих данных."""
        
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

