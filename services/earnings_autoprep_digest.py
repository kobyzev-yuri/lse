"""Once-per-day Telegram digest for earnings autoprep (ops visibility)."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import get_config_value
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

logger = logging.getLogger(__name__)

_STATE_NAME = ".earnings_autoprep_digest_sent.json"


def _state_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        base = Path("/app/logs/ml/ml_data_quality")
    else:
        root = project_root or Path(__file__).resolve().parents[1]
        base = root / "local" / "logs" / "ml_data_quality"
    return base / _STATE_NAME


def _cfg_bool(key: str, default: bool = True) -> bool:
    raw = (get_config_value(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _load_last_sent_day(path: Path) -> date | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        s = str(data.get("last_sent_day") or "")[:10]
        return date.fromisoformat(s) if s else None
    except Exception:
        return None


def _save_sent_day(path: Path, d: date) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_sent_day": d.isoformat(), "sent_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def format_autoprep_digest_message(summary: dict[str, Any]) -> str:
    steps = summary.get("steps") or {}
    readiness = summary.get("readiness") or {}
    autoprep = readiness.get("earnings_autoprep") if isinstance(readiness.get("earnings_autoprep"), dict) else {}
    pending = summary.get("pending_calendar_events")
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [f"Earnings autoprep · {today}", ""]

    if pending == 0:
        lines.append("Материалы: pending=0 — transcript/PR на месте")
    elif pending is not None:
        lines.append(
            f"Материалы: pending={pending} — нет rich-источника (transcript/PR) ±1д от даты KB"
        )
    else:
        lines.append("Материалы: pending=—")

    if readiness.get("overall_earnings_autoprep_ready"):
        lines.append("Autoprep gate: OPEN ✅")
    else:
        reasons = [str(r) for r in (autoprep.get("reasons") or []) if r][:2]
        hint = "; ".join(reasons) if reasons else "см. analyzer"
        n_labels = autoprep.get("llm_scenario_labels")
        n_shadow = autoprep.get("shadow_n_matured")
        stats = ""
        if n_labels is not None and n_shadow is not None:
            stats = f" (labels {n_labels}/40, shadow {n_shadow}/50)"
        lines.append(f"Autoprep gate: false — {hint}{stats}")

    lines.append(
        f"Grid={readiness.get('overall_grid_ready')} · Peer={readiness.get('overall_peer_spillover_ready')}"
    )
    lines.append(
        "Cron steps: "
        f"sync={steps.get('materials_sync')} "
        f"ingest={steps.get('materials_ingest')} "
        f"extract={steps.get('materials_extract')}"
    )
    bal = summary.get("llm_balance_alert")
    if isinstance(bal, dict) and bal.get("active"):
        lines.append(f"ProxyAPI alert: {bal.get('message') or 'low balance'}")
    lines.append("Runbook: docs/EARNINGS_CALENDAR_RUNBOOK.md")
    return "\n".join(lines)


def maybe_send_autoprep_daily_digest(
    summary: dict[str, Any],
    *,
    project_root: Path | None = None,
    force: bool = False,
) -> bool:
    """Send at most one digest per UTC day when enabled."""
    if not _cfg_bool("EARNINGS_AUTOPREP_DIGEST_TELEGRAM", True):
        return False
    token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        return False

    today = datetime.now(timezone.utc).date()
    state_path = _state_path(project_root)
    if not force and _load_last_sent_day(state_path) == today:
        return False

    text = format_autoprep_digest_message(summary)
    sent = False
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
            sent = True
    if sent:
        _save_sent_day(state_path, today)
        logger.info("Autoprep daily digest sent to Telegram")
    return sent
