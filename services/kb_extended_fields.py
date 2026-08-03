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


# Prefixes for external_id — investing keeps historical `inv_cal|…` for dedupe stability.
_MACRO_CAL_ID_PREFIX = {
    "investing": "inv_cal",
    "fred": "fred_cal",
    "fomc": "fomc_cal",
}
_MACRO_CAL_PAYLOAD_SLUG = {
    "investing": "investing_calendar",
    "fred": "fred_calendar",
    "fomc": "fomc_calendar",
}

# Shared SQL fragment for KB macro calendar consumers (Investing + FRED + FOMC).
# Use inside a larger SQL string; single % for ILIKE (not %-format).
MACRO_CALENDAR_KB_SOURCE_SQL = """(
  source ILIKE '%Investing.com%Economic%Calendar%'
  OR source ILIKE 'FRED Economic Calendar%'
  OR source ILIKE 'FOMC Calendar%'
)"""


def is_macro_calendar_kb_source(source: str) -> bool:
    """True if knowledge_base.source is an official/Investing macro calendar row."""
    s = (source or "").strip().lower()
    if not s:
        return False
    if "investing.com" in s and "economic" in s and "calendar" in s:
        return True
    if s.startswith("fred economic calendar"):
        return True
    if s.startswith("fomc calendar"):
        return True
    return False


def macro_calendar_source_label(provider: str, region: str) -> str:
    """Canonical knowledge_base.source for macro calendar rows."""
    p = (provider or "investing").strip().lower()
    r = (region or "").strip() or "USA"
    if p == "fred":
        return "FRED Economic Calendar (USA)"
    if p == "fomc":
        return "FOMC Calendar (USA)"
    return f"Investing.com Economic Calendar ({r})"


def macro_calendar_external_id(
    provider: str,
    region: str,
    event_dt: datetime,
    event_name: str,
    event_type: str,
) -> str:
    """Стабильный ключ дедупа для строк макро-календаря в KB."""
    p = (provider or "investing").strip().lower()
    prefix = _MACRO_CAL_ID_PREFIX.get(p, f"{p}_cal")
    base = (
        f"{prefix}|{region}|{event_dt.isoformat()}|{(event_name or '').strip()}|{event_type}"
    )
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def macro_calendar_raw_payload(provider: str, event: Dict[str, Any]) -> str:
    """JSON для raw_payload: datetime → ISO, плюс метка провайдера."""
    p = (provider or "investing").strip().lower()
    slug = _MACRO_CAL_PAYLOAD_SLUG.get(p, f"{p}_calendar")
    out: Dict[str, Any] = {"provider": slug}
    for k, v in event.items():
        if k == "event_date" and isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)


def investing_calendar_external_id(
    region: str,
    event_dt: datetime,
    event_name: str,
    event_type: str,
) -> str:
    """Стабильный ключ дедупа для строк Investing.com economic calendar (HTML и JSON API)."""
    return macro_calendar_external_id("investing", region, event_dt, event_name, event_type)


def investing_calendar_raw_payload(event: Dict[str, Any]) -> str:
    """JSON для raw_payload: datetime → ISO, плюс метка провайдера."""
    return macro_calendar_raw_payload("investing", event)
