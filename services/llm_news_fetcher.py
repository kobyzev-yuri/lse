"""
Источник новостей по тикеру через прямой запрос к LLM (GPT/Gemini и т.д.).

Результат сохраняется в knowledge_base с source='LLM (...)', event_type='NEWS'
и попадает в get_recent_news вместе с RSS, NewsAPI и др.

Включение: USE_LLM_NEWS=true в config.env (и настроенный OPENAI_API_KEY / proxy).

Дедупликация: LLM не даёт реального времени — возвращает одни и те же факты (напр. 2023).
При частом запуске крона получались десятки одинаковых записей в день. Поэтому перед вставкой
проверяем: если по этому тикеру уже есть LLM-запись за последние LLM_NEWS_COOLDOWN_HOURS (по умолч. 24),
новую не сохраняем.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)

# Минимальный интервал (часов) между сохранением LLM-новостей по одному тикеру (избегаем дублей)
DEFAULT_LLM_NEWS_COOLDOWN_HOURS = 24


def fetch_and_save_llm_news(ticker: str = "SNDK") -> Optional[int]:
    """
    Запрашивает у LLM свежие новости по тикеру и сохраняет одну запись в knowledge_base.
    Не сохраняет, если по этому тикеру уже есть LLM-запись за последние N часов (дедупликация).

    Returns:
        id созданной записи в knowledge_base или None при ошибке/отключении/пропуске из-за cooldown.
    """
    if get_config_value("USE_LLM_NEWS", "").strip().lower() not in ("1", "true", "yes"):
        logger.debug("USE_LLM_NEWS не включён, пропуск LLM-новостей для %s", ticker)
        return None

    try:
        cooldown_hours = int(get_config_value("LLM_NEWS_COOLDOWN_HOURS", str(DEFAULT_LLM_NEWS_COOLDOWN_HOURS)).strip() or DEFAULT_LLM_NEWS_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        cooldown_hours = DEFAULT_LLM_NEWS_COOLDOWN_HOURS

    engine = create_engine(get_database_url())
    ticker_upper = ticker.upper()

    # Дедупликация: не сохранять, если недавно уже сохраняли LLM-новость по этому тикеру
    with engine.connect() as conn:
        since = datetime.now() - timedelta(hours=cooldown_hours)
        existing = conn.execute(
            text("""
                SELECT id FROM knowledge_base
                WHERE ticker = :ticker AND source LIKE 'LLM%' AND ts >= :since
                LIMIT 1
            """),
            {"ticker": ticker_upper, "since": since},
        ).fetchone()
        if existing:
            logger.debug("LLM-новость по %s не сохранена: уже есть запись за последние %s ч (id=%s)", ticker, cooldown_hours, existing[0])
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
        from services.ticker_groups import get_tracked_tickers_for_kb
        if ticker_upper not in set(get_tracked_tickers_for_kb()):
            logger.debug("Пропуск LLM-новости по ненаблюдаемому тикеру %s", ticker_upper)
            return None
    except Exception:
        pass
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight, event_type)
                    VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight, 'NEWS')
                """),
                {
                    "ts": ts,
                    "ticker": ticker_upper,
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
