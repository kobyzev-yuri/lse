"""Audit ERD labeling skip reasons (no_quotes, anchor_unresolved) for ops alerts."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from config_loader import get_config_value
from services.earnings_intelligence_universe import get_event_reaction_symbol_allowlist
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

logger = logging.getLogger(__name__)

DEFAULT_DATASET_VERSION = "v0_expanded_baseline"


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_bool(key: str, default: bool = False) -> bool:
    raw = (get_config_value(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def default_labeling_gap_alert_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_erd_labeling_gap_alert.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_erd_labeling_gap_alert.json"


def audit_erd_labeling_gaps(
    engine: Engine,
    *,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    symbols: list[str] | None = None,
    limit: int = 600,
    feature_builder_version: str | None = None,
) -> dict[str, Any]:
    """
    Probe past ERD rows with empty outcomes; classify skip reasons via compute_row_labeling.

    Future events are counted separately and excluded from alert thresholds.
    """
    from services.event_reaction_labeling import active_feature_builder_version, compute_row_labeling

    sym_list = symbols or get_event_reaction_symbol_allowlist()
    sym_list = sorted({str(s).strip().upper() for s in sym_list if str(s).strip()})
    if not sym_list:
        return {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "dataset_version": dataset_version,
            "error": "empty_symbol_allowlist",
        }

    fbv = (feature_builder_version or active_feature_builder_version()).strip()
    today = date.today()
    q = text(
        """
        SELECT id, symbol, event_time_et, knowledge_base_id
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND UPPER(TRIM(symbol)) = ANY(:symbols)
          AND COALESCE(outcomes_after, '{}'::jsonb) = '{}'::jsonb
        ORDER BY event_time_et DESC
        LIMIT :lim
        """
    ).bindparams(bindparam("symbols", expanding=True))

    params: dict[str, Any] = {
        "dv": dataset_version,
        "symbols": sym_list,
        "lim": max(1, int(limit)),
    }

    skip_reasons: Counter[str] = Counter()
    future_events = 0
    resolved = 0
    sampled = 0

    with engine.connect() as conn:
        rows = conn.execute(q, params).mappings().all()

    for row in rows:
        sampled += 1
        event_time = row.get("event_time_et")
        try:
            event_d = event_time.date() if hasattr(event_time, "date") else today
        except Exception:
            event_d = today
        if event_d > today:
            future_events += 1
            continue

        _, outcomes, _, reason = compute_row_labeling(
            str(row.get("symbol") or ""),
            event_time,
            knowledge_base_id=row.get("knowledge_base_id"),
            feature_builder_version=fbv,
        )
        if outcomes:
            resolved += 1
            continue
        bucket = (reason or "unknown").split(":")[0]
        skip_reasons[bucket] += 1

    no_quotes = int(skip_reasons.get("no_quotes", 0))
    anchor_unresolved = int(skip_reasons.get("anchor_unresolved", 0))
    thresholds = {
        "no_quotes_max": _cfg_int("ERD_LABELING_GAP_NO_QUOTES_MAX", 5),
        "anchor_unresolved_max": _cfg_int("ERD_LABELING_GAP_ANCHOR_UNRESOLVED_MAX", 15),
    }
    over_threshold = (
        no_quotes > thresholds["no_quotes_max"]
        or anchor_unresolved > thresholds["anchor_unresolved_max"]
    )

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_version": dataset_version,
        "symbols_distinct": len(sym_list),
        "rows_sampled": sampled,
        "future_events_excluded": future_events,
        "would_resolve_now": resolved,
        "skip_reasons": dict(skip_reasons),
        "no_quotes": no_quotes,
        "anchor_unresolved": anchor_unresolved,
        "thresholds": thresholds,
        "over_threshold": over_threshold,
        "alert_active": over_threshold,
    }


def write_erd_labeling_gap_alert(payload: dict[str, Any], *, project_root: Path | None = None) -> Path:
    out = default_labeling_gap_alert_path(project_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["active"] = bool(payload.get("alert_active"))
    out.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def clear_erd_labeling_gap_alert(*, project_root: Path | None = None) -> None:
    out = default_labeling_gap_alert_path(project_root)
    if out.is_file():
        out.unlink()


def maybe_send_erd_labeling_gap_telegram(payload: dict[str, Any]) -> bool:
    if not payload.get("alert_active"):
        return False
    if not _cfg_bool("ERD_LABELING_ALERT_TELEGRAM", True):
        return False
    token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        logger.warning("ERD labeling gap alert: Telegram not configured")
        return False

    th = payload.get("thresholds") or {}
    lines = [
        "ERD labeling gaps (past events, empty outcomes):",
        f"no_quotes={payload.get('no_quotes')} (max {th.get('no_quotes_max')})",
        f"anchor_unresolved={payload.get('anchor_unresolved')} (max {th.get('anchor_unresolved_max')})",
        f"sampled={payload.get('rows_sampled')} future_excluded={payload.get('future_events_excluded')}",
        "Fix: seed_quotes / earnings timing / prune pre-IPO skeletons.",
        "JSON: last_erd_labeling_gap_alert.json",
    ]
    text = "\n".join(lines)
    sent = False
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
            sent = True
    return sent
