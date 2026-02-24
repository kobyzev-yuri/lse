"""
Источник новостей по тикеру через прямой запрос к LLM (GPT/Gemini и т.д.).

Результат сохраняется в knowledge_base с source='LLM (...)', event_type='NEWS'
и попадает в get_recent_news вместе с RSS, NewsAPI и др.

Включение: USE_LLM_NEWS=true в config.env (и настроенный OPENAI_API_KEY / proxy).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)


def fetch_and_save_llm_news(ticker: str = "SNDK") -> Optional[int]:
    """
    Запрашивает у LLM свежие новости по тикеру и сохраняет одну запись в knowledge_base.

    Returns:
        id созданной записи в knowledge_base или None при ошибке/отключении.
    """
    if get_config_value("USE_LLM_NEWS", "").strip().lower() not in ("1", "true", "yes"):
        logger.debug("USE_LLM_NEWS не включён, пропуск LLM-новостей для %s", ticker)
        return None

    llm = get_llm_service()
    data = llm.fetch_news_for_ticker(ticker)
    if not data:
        return None

    content = data.get("content") or ""
    if not content.strip():
        return None

    source = data.get("source_label", "LLM")
    sentiment = data.get("sentiment_score")
    insight = (data.get("insight") or "")[:1000]
    ts = datetime.now()

    try:
        engine = create_engine(get_database_url())
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight, event_type)
                    VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight, 'NEWS')
                """),
                {
                    "ts": ts,
                    "ticker": ticker.upper(),
                    "source": source,
                    "content": content[:8000],
                    "sentiment_score": sentiment,
                    "insight": insight or None,
                },
            )
            row = conn.execute(text("SELECT LASTVAL()")).fetchone()
            news_id = row[0] if row else None
        logger.info("✅ LLM-новость по %s сохранена в knowledge_base, id=%s", ticker, news_id)
        return news_id
    except Exception as e:
        logger.exception("Ошибка сохранения LLM-новости по %s: %s", ticker, e)
        return None
