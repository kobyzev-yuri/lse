"""
Общие поля расширенной схемы knowledge_base (exchange, symbol, external_id, content_sha256, raw_payload).

Используются импортёрами (календарь, RSS, NewsAPI, …), чтобы строки KB были согласованы с миграцией
db/knowledge_pg/sql/010_knowledge_base_nyse.sql.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict


def kb_content_sha256(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8", errors="ignore")).hexdigest()


def kb_external_id(exchange: str, symbol: str, link: str, title: str) -> str:
    """
    Детерминированный внешний id для дедупа.
    Совместимо с логикой scripts/import_news_jsonl_to_kb.py (exchange|symbol|url|title → sha256).
    """
    base = f"{(exchange or '').strip().upper()}|{(symbol or '').strip().upper()}|{(link or '').strip()}|{(title or '').strip()}"
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


_PROVIDER_SLUG_EXTERNAL_IDS = frozenset(
    {
        "yfinance",
        "yahoo",
        "newsapi",
        "news_api",
        "finnhub",
        "alphavantage",
        "alpha_vantage",
        "marketaux",
        "investing",
        "rss",
        "polygon",
    }
)


def kb_resolved_external_id(raw: str, exchange: str, symbol: str, link: str, title: str) -> str:
    """
    Источники иногда кладут в external_id «slug провайдера» (одинаковый для всех статей) → ломает UNIQUE(external_id).
    Если raw пустой/подозрительный — генерим детерминированный ключ.
    """
    r = (raw or "").strip()
    rl = r.lower()
    if not r or rl in _PROVIDER_SLUG_EXTERNAL_IDS or len(r) < 24:
        return kb_external_id(exchange, symbol, link, title)
    return r[:512]


def kb_legacy_ticker(symbol: str) -> str:
    """Колонка knowledge_base.ticker часто VARCHAR(10)."""
    s = (symbol or "").strip().upper()
    if not s:
        return ""
    return (s.lstrip("^")[:10] or s[:10]).strip()


def investing_calendar_external_id(
    region: str,
    event_dt: datetime,
    event_name: str,
    event_type: str,
) -> str:
    """Стабильный ключ дедупа для строк Investing.com economic calendar (нет нативного id в HTML)."""
    base = (
        f"inv_cal|{region}|{event_dt.isoformat()}|{event_name.strip()}|{event_type}"
    )
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def investing_calendar_raw_payload(event: Dict[str, Any]) -> str:
    """JSON для raw_payload: datetime → ISO, плюс метка провайдера."""
    out: Dict[str, Any] = {"provider": "investing_calendar"}
    for k, v in event.items():
        if k == "event_date" and isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)
