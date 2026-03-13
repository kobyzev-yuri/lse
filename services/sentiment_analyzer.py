"""
Модуль для автоматического расчета sentiment новостей.

Поддерживает два режима (config: SENTIMENT_METHOD):
- llm — платный LLM (OpenAI/proxyapi, напр. gpt-4o): даёт sentiment + краткий insight.
- transformers — бесплатная локальная модель (HuggingFace): FinBERT для финансовых текстов
  или sentiment-analysis pipeline (distilbert). Без insight (возвращается None).
"""

import logging
from typing import Optional
from config_loader import get_config_value

logger = logging.getLogger(__name__)

# Кэш pipeline для transformers, чтобы не грузить модель при каждом вызове
_sentiment_pipeline = None


def _sentiment_transformers(content: str, model_name: Optional[str] = None) -> tuple[float, Optional[str]]:
    """
    Sentiment через HuggingFace (бесплатно, локально).
    model_name: None или пусто = FinBERT (ProsusAI/finbert), иначе например
    cardiffnlp/twitter-roberta-base-sentiment-latest или distilbert-base-uncased-finetuned-sst-2-english.
    """
    global _sentiment_pipeline
    if not content or not content.strip():
        return 0.5, None
    text = content.strip()[:4000]  # лимит длины для модели
    model = (model_name or get_config_value("SENTIMENT_MODEL", "ProsusAI/finbert")).strip() or "ProsusAI/finbert"
    try:
        from transformers import pipeline
    except ImportError:
        logger.warning("⚠️ transformers не установлен, sentiment через модель недоступен")
        return 0.5, None
    try:
        if _sentiment_pipeline is None:
            _sentiment_pipeline = pipeline(
                "text-classification",
                model=model,
                top_k=None,
                truncation=True,
                max_length=512,
            )
        out = _sentiment_pipeline(text[:512])
        if not out:
            return 0.5, None
        # pipeline возвращает список по входам; один текст -> out[0]
        preds = out[0] if isinstance(out, list) else out
        if isinstance(preds, dict):
            best = preds
        elif isinstance(preds, list) and len(preds) > 0:
            best = max(preds, key=lambda x: x.get("score", 0) if isinstance(x, dict) else 0)
        else:
            return 0.5, None
        label = (best.get("label") or "").lower()
        score = float(best.get("score", 0.5))
        # Приводим к шкале 0.0 (негатив) — 1.0 (позитив)
        if "neg" in label or label == "negative":
            sentiment = 1.0 - score
        elif "pos" in label or label == "positive":
            sentiment = score
        else:
            sentiment = 0.5
        sentiment = max(0.0, min(1.0, sentiment))
        logger.debug("Sentiment (transformers %s): %s -> %.3f", model, label, sentiment)
        return sentiment, None  # insight для локальной модели не извлекаем
    except Exception as e:
        logger.warning("⚠️ Ошибка sentiment (transformers): %s", e)
        return 0.5, None


def calculate_sentiment(content: str) -> tuple[float, Optional[str]]:
    """
    Рассчитывает sentiment score и при возможности insight из текста новости.

    Режим задаётся SENTIMENT_METHOD в config.env:
    - llm (по умолчанию) — через платный LLM (gpt-4o и т.д.), возвращает (score, insight).
    - transformers — бесплатная модель HuggingFace (по умолчанию ProsusAI/finbert), возвращает (score, None).

    Returns:
        tuple: (sentiment_score, insight)
        - sentiment_score: от 0.0 (отрицательный) до 1.0 (положительный)
        - insight: ключевой факт или None (только при method=llm)
    """
    method = (get_config_value("SENTIMENT_METHOD", "llm") or "llm").strip().lower()
    if method == "transformers":
        return _sentiment_transformers(content)

    # LLM (по умолчанию)
    try:
        from services.llm_service import get_llm_service
        import json
        import re

        llm_service = get_llm_service()
        if not getattr(llm_service, "client", None):
            logger.warning("⚠️ LLM недоступен (нет API key), пробуем transformers")
            return _sentiment_transformers(content)

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
        response_text = (result.get("response") or "").strip()
        if not response_text:
            return 0.5, None

        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                sentiment = float(data.get('sentiment', 0.5))
                insight = data.get('insight', None)
                sentiment = max(0.0, min(1.0, sentiment))
                logger.info("✅ Sentiment рассчитан через LLM: %.3f", sentiment)
                return sentiment, insight
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("⚠️ Не удалось распарсить JSON из ответа LLM: %s", e)
        number_match = re.search(r'0?\.?\d+', response_text)
        if number_match:
            sentiment = max(0.0, min(1.0, float(number_match.group())))
            return sentiment, None
        return 0.5, None
    except ImportError:
        logger.warning("⚠️ LLM сервис недоступен, используем transformers")
        return _sentiment_transformers(content)
    except Exception as e:
        logger.error("❌ Ошибка расчета sentiment через LLM: %s", e)
        return _sentiment_transformers(content)  # fallback на бесплатную модель


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

