"""Telegram alert when a past earnings event gets LLM extraction (Phase C)."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.earnings_event_brief import build_event_brief
from services.earnings_event_freshness import is_telegram_eligible_event, telegram_max_event_age_days
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

logger = logging.getLogger(__name__)

_STATE_NAME = ".earnings_post_event_brief_sent.json"
_SENT_MAX = 500


def _state_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        base = Path("/app/logs/ml/ml_data_quality")
    else:
        root = project_root or Path(__file__).resolve().parents[1]
        base = root / "local" / "logs" / "ml_data_quality"
    return base / _STATE_NAME


def _event_hash(symbol: str, event_d: date) -> str:
    return hashlib.sha256(f"{symbol.upper()}|{event_d.isoformat()}".encode()).hexdigest()


def _load_sent(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        lst = data if isinstance(data, list) else data.get("hashes", [])
        return set(lst[-_SENT_MAX:])
    except Exception:
        return set()


def _save_sent(path: Path, hashes: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hashes[-_SENT_MAX:], ensure_ascii=False), encoding="utf-8")


def _cfg_bool(key: str, default: bool = True) -> bool:
    raw = (get_config_value(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def load_recent_extracted_events(
    engine: Engine,
    *,
    lookback_days: int = 3,
    limit: int = 30,
    max_event_age_days: int | None = None,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
    max_age = max_event_age_days if max_event_age_days is not None else telegram_max_event_age_days()
    q = text(
        """
        SELECT
          UPPER(TRIM(kb.ticker)) AS symbol,
          kb.ts::date AS event_date,
          ed.updated_at
        FROM earnings_event_detail ed
        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
        WHERE UPPER(COALESCE(kb.event_type, '')) LIKE '%EARNING%'
          AND kb.ts::date <= CURRENT_DATE
          AND kb.ts::date >= CURRENT_DATE - CAST(:max_age_days AS int)
          AND ed.guidance_summary ? 'extraction_meta'
          AND ed.updated_at >= :since
        ORDER BY ed.updated_at DESC
        LIMIT :lim
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(
            q,
            {"since": since, "lim": max(1, int(limit)), "max_age_days": max_age},
        ).mappings().all()
    return [dict(r) for r in rows]


def format_brief_telegram(brief: dict[str, Any], *, base_url: str = "") -> str:
    sym = brief.get("symbol") or "?"
    ev = brief.get("event_date") or "?"
    if brief.get("status") != "ok":
        return f"Earnings brief ({sym} {ev}): {brief.get('status')} — {brief.get('reason') or 'partial'}"

    scen = brief.get("scenario") or {}
    scen_id = scen.get("id") or "—"
    conf = scen.get("confidence") or "—"
    peers = brief.get("peer_spillover_outcomes") or []
    peer_bits = []
    for p in peers[:3]:
        if not isinstance(p, dict):
            continue
        t = p.get("ticker") or p.get("symbol") or "?"
        r5 = p.get("forward_log_ret_5d")
        peer_bits.append(f"{t} 5d={r5}" if r5 is not None else str(t))
    peer_line = ", ".join(peer_bits) if peer_bits else "—"

    lines = [
        f"Earnings: {sym} {ev}",
        f"Сценарий: {scen_id} ({conf})",
        f"Peers: {peer_line}",
    ]
    tone = brief.get("management_tone")
    if tone:
        lines.append(f"Tone: {tone}")
    url = (base_url or "").strip().rstrip("/")
    if url:
        lines.append(f"{url}/earnings")
    return "\n".join(lines)


def notify_new_post_event_briefs(
    engine: Engine,
    *,
    project_root: Path | None = None,
    lookback_days: int = 3,
    limit: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send Telegram once per (symbol, event_date) when extraction is fresh."""
    if not _cfg_bool("EARNINGS_POST_EVENT_BRIEF_TELEGRAM", True):
        return {"sent": 0, "skipped": "disabled"}

    token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        return {"sent": 0, "skipped": "no_telegram"}

    state_path = _state_path(project_root)
    sent_set = _load_sent(state_path)
    sent_list = list(sent_set)

    base_url = (get_config_value("EARNINGS_BRIEF_PUBLIC_BASE_URL") or "http://104.154.205.58:8080").strip()
    events = load_recent_extracted_events(engine, lookback_days=lookback_days, limit=limit)
    sent_count = 0
    candidates = 0

    for ev in events:
        sym = str(ev.get("symbol") or "").strip().upper()
        event_date = ev.get("event_date")
        if not sym or not isinstance(event_date, date):
            continue
        h = _event_hash(sym, event_date)
        if h in sent_set:
            continue
        if not is_telegram_eligible_event(event_date):
            continue
        candidates += 1
        brief = build_event_brief(engine, symbol=sym, event_date=event_date)
        text = format_brief_telegram(brief, base_url=base_url)
        if dry_run:
            logger.info("dry-run brief alert %s %s: %s", sym, event_date, text[:120])
            sent_set.add(h)
            sent_list.append(h)
            sent_count += 1
            continue
        ok = False
        for cid in chat_ids:
            if send_telegram_message(token, cid, text, parse_mode=None):
                ok = True
        if ok:
            sent_set.add(h)
            sent_list.append(h)
            sent_count += 1
            logger.info("Post-event brief Telegram sent: %s %s", sym, event_date)

    if sent_list and not dry_run:
        _save_sent(state_path, sent_list)

    return {"sent": sent_count, "candidates": candidates, "events_scanned": len(events)}
