"""
Источник новостей по тикеру через прямой запрос к LLM (GPT/Gemini и т.д.).

Результат сохраняется в knowledge_base с source='LLM (...)', event_type='NEWS'
и попадает в get_recent_news вместе с RSS, NewsAPI и др.

Включение: USE_LLM_NEWS=true в config.env (и настроенный OPENAI_API_KEY / proxy).

Дедупликация:
1) По времени: если за последние LLM_NEWS_COOLDOWN_HOURS (по умолч. 168 = 7 дней) уже есть LLM-запись по тикеру — не сохраняем.
2) По содержанию: если последняя LLM-запись по тикеру почти совпадает с новым текстом (одни и те же факты) — не сохраняем.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import create_engine, text

from config_loader import get_config_value, get_database_url
from services.llm_service import get_llm_service

logger = logging.getLogger(__name__)

# Интервал (часов) между сохранением LLM-новостей по одному тикеру. LLM не даёт real-time — 7 дней достаточно.
DEFAULT_LLM_NEWS_COOLDOWN_HOURS = 168  # 7 дней

# Минимальная длина совпадающего начала контента (символов), чтобы считать запись дубликатом
CONTENT_DEDUP_PREFIX_LEN = 400


def _normalize_for_compare(s: str, max_len: int = 500) -> str:
    """Приводит строку к виду для сравнения: без лишних пробелов, по одному пробелу, обрезка."""
    if not s:
        return ""
    t = " ".join(str(s).split())[:max_len]
    return t.strip()


def fetch_and_save_llm_news(ticker: str = "SNDK") -> Tuple[Optional[int], Optional[str]]:
    """
    Запрашивает у LLM свежие новости по тикеру и сохраняет одну запись в knowledge_base.
    Не сохраняет, если (1) уже есть LLM-запись за последние N ч (cooldown) или
    (2) последняя LLM-запись по тикеру имеет очень похожее содержание (одни и те же факты).

    Returns:
        (id созданной записи, None) при успехе; (None, причина_пропуска) при пропуске/ошибке.
    """
    if get_config_value("USE_LLM_NEWS", "").strip().lower() not in ("1", "true", "yes"):
        reason = "USE_LLM_NEWS не включён"
        logger.info("LLM(%s): %s", ticker, reason)
        return (None, reason)

    try:
        cooldown_hours = int(get_config_value("LLM_NEWS_COOLDOWN_HOURS", str(DEFAULT_LLM_NEWS_COOLDOWN_HOURS)).strip() or DEFAULT_LLM_NEWS_COOLDOWN_HOURS)
    except (ValueError, TypeError):
        cooldown_hours = DEFAULT_LLM_NEWS_COOLDOWN_HOURS

    engine = create_engine(get_database_url())
    ticker_upper = ticker.upper()
    try:
        # 1) Дедупликация по времени
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
                reason = f"уже есть запись за последние {cooldown_hours} ч"
                logger.info("LLM(%s): ⏭️ %s", ticker, reason)
                return (None, reason)

        llm = get_llm_service()
        data = llm.fetch_news_for_ticker(ticker)
        if not data:
            reason = "LLM не ответил или не инициализирован"
            logger.info("LLM(%s): ⏭️ %s", ticker, reason)
            return (None, reason)

        content = data.get("content") or ""
        if not content.strip():
            reason = "LLM вернул пустой контент"
            logger.info("LLM(%s): ⏭️ %s", ticker, reason)
            return (None, reason)

        # 2) Дедупликация по содержанию: не сохранять, если последняя LLM-запись по тикеру почти такая же
        with engine.connect() as conn:
            last_row = conn.execute(
                text("""
                    SELECT content FROM knowledge_base
                    WHERE ticker = :ticker AND source LIKE 'LLM%' AND content IS NOT NULL
                    ORDER BY ts DESC LIMIT 1
                """),
                {"ticker": ticker_upper},
            ).fetchone()
            if last_row and last_row[0]:
                prev = _normalize_for_compare(last_row[0], max_len=CONTENT_DEDUP_PREFIX_LEN)
                new_prefix = _normalize_for_compare(content, max_len=CONTENT_DEDUP_PREFIX_LEN)
                if prev and new_prefix and len(prev) >= 100 and (prev == new_prefix or prev in new_prefix or new_prefix in prev):
                    reason = "содержание совпадает с последней записью"
                    logger.info("LLM(%s): ⏭️ %s", ticker, reason)
                    return (None, reason)

        source = data.get("source_label", "LLM")
        sentiment = data.get("sentiment_score")
        insight = (data.get("insight") or "")[:1000]
        ts = datetime.now()

        try:
            from services.ticker_groups import get_tracked_tickers_for_kb
            if ticker_upper not in set(get_tracked_tickers_for_kb()):
                reason = "тикер не в списке для KB"
                logger.info("LLM(%s): ⏭️ %s", ticker, reason)
                return (None, reason)
        except Exception:
            pass
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
        return (news_id, None)
    except Exception as e:
        logger.exception("Ошибка сохранения LLM-новости по %s: %s", ticker, e)
        return (None, str(e))
    finally:
        engine.dispose()
