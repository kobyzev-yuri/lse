"""
Модуль для автоматического расчета sentiment новостей через LLM
"""

import logging
from typing import Optional
from config_loader import get_config_value

logger = logging.getLogger(__name__)


def calculate_sentiment(content: str) -> tuple[float, Optional[str]]:
    """
    Рассчитывает sentiment score и извлекает insight из текста новости через LLM
    
    Args:
        content: Текст новости
        
    Returns:
        tuple: (sentiment_score, insight)
        - sentiment_score: от 0.0 (отрицательный) до 1.0 (положительный)
        - insight: ключевой финансовый факт (например, "рост 163%") или None
    """
    try:
        from services.llm_service import get_llm_service
        import json
        import re
        
        llm_service = get_llm_service()
        
        system_prompt = """Ты финансовый аналитик, специализирующийся на анализе sentiment новостей.
Твоя задача - оценить sentiment новости и выделить ключевой финансовый факт.

Отвечай в формате JSON:
{
    "sentiment": 0.0-1.0,  // где 0.0 - очень отрицательный, 0.5 - нейтральный, 1.0 - очень положительный
    "insight": "ключевой финансовый факт"  // например, "рост 163%", "падение на 5%", "новый продукт"
}

Insight должен быть кратким (одно предложение) и содержать конкретный факт из новости.
"""
        
        user_message = f"Оцени sentiment и выдели ключевой факт из следующей новости:\n\n{content}"
        
        messages = [{"role": "user", "content": user_message}]
        
        result = llm_service.generate_response(
            messages,
            system_prompt=system_prompt,
            max_tokens=150,
            temperature=0.1
        )
        
        response_text = result["response"].strip()
        
        # Пытаемся распарсить JSON
        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                sentiment = float(data.get('sentiment', 0.5))
                insight = data.get('insight', None)
                
                # Ограничиваем диапазон 0.0-1.0
                sentiment = max(0.0, min(1.0, sentiment))
                
                logger.info(f"✅ Sentiment рассчитан через LLM: {sentiment:.3f}")
                if insight:
                    logger.info(f"   Insight: {insight[:100]}...")
                
                return sentiment, insight
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"⚠️ Не удалось распарсить JSON из ответа LLM: {e}")
            # Пытаемся извлечь хотя бы число
            number_match = re.search(r'0?\.?\d+', response_text)
            if number_match:
                sentiment = float(number_match.group())
                sentiment = max(0.0, min(1.0, sentiment))
                return sentiment, None
        
        logger.warning(f"⚠️ Не удалось извлечь данные из ответа LLM: {response_text}")
        return 0.5, None  # Нейтральный sentiment по умолчанию
            
    except ImportError:
        logger.warning("⚠️ LLM сервис недоступен, используем нейтральный sentiment")
        return 0.5, None
    except Exception as e:
        logger.error(f"❌ Ошибка расчета sentiment через LLM: {e}")
        return 0.5, None  # Нейтральный sentiment при ошибке


def calculate_sentiment_batch(news_list: list) -> list:
    """
    Рассчитывает sentiment для списка новостей
    
    Args:
        news_list: Список словарей с ключом 'content'
        
    Returns:
        Список кортежей (sentiment_score, insight)
    """
    results = []
    for news in news_list:
        content = news.get('content', '')
        if content:
            sentiment, insight = calculate_sentiment(content)
            results.append((sentiment, insight))
        else:
            results.append((0.5, None))  # Нейтральный для пустого контента
    
    return results

