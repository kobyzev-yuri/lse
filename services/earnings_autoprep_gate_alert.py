"""Telegram alert when overall_earnings_autoprep_ready flips to true."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import get_config_value
from services.telegram_signal import get_signal_chat_ids, send_telegram_message

logger = logging.getLogger(__name__)


def _cfg_bool(key: str, default: bool = True) -> bool:
    raw = (get_config_value(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def default_gate_state_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_earnings_autoprep_gate_state.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_earnings_autoprep_gate_state.json"


def read_autoprep_gate_state(*, project_root: Path | None = None) -> dict[str, Any]:
    path = default_gate_state_path(project_root)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def write_autoprep_gate_state(payload: dict[str, Any], *, project_root: Path | None = None) -> Path:
    path = default_gate_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def maybe_notify_autoprep_gate_ready(
    gates: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """
    Persist gate state; send Telegram once when overall_earnings_autoprep_ready becomes true.
    """
    current_ready = bool(gates.get("overall_earnings_autoprep_ready"))
    prev = read_autoprep_gate_state(project_root=project_root)
    was_ready = bool(prev.get("was_ready"))
    flipped = current_ready and not was_ready

    ap = gates.get("earnings_autoprep") if isinstance(gates.get("earnings_autoprep"), dict) else {}
    state = {
        "was_ready": current_ready,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_earnings_autoprep_ready": current_ready,
        "reasons": list(ap.get("reasons") or []),
        "llm_scenario_labels": ap.get("llm_scenario_labels"),
        "shadow_n_matured": ap.get("shadow_n_matured"),
        "shadow_sign_accuracy": ap.get("shadow_sign_accuracy"),
        "notified_at_utc": prev.get("notified_at_utc"),
    }

    if flipped and _cfg_bool("EARNINGS_AUTOPREP_GATE_ALERT_TELEGRAM", True):
        token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_ids = get_signal_chat_ids()
        if token and chat_ids:
            lines = [
                "Earnings autoprep gate OPEN",
                f"overall_earnings_autoprep_ready=true",
                f"labels={ap.get('llm_scenario_labels')} shadow_n={ap.get('shadow_n_matured')} "
                f"sign_acc={ap.get('shadow_sign_accuracy')}",
                f"grid={gates.get('overall_grid_ready')} peer={gates.get('overall_peer_spillover_ready')}",
                "Next: Phase C Telegram brief + open-path prerequisites.",
            ]
            text = "\n".join(lines)
            sent = any(send_telegram_message(token, cid, text, parse_mode=None) for cid in chat_ids)
            if sent:
                state["notified_at_utc"] = datetime.now(timezone.utc).isoformat()
                logger.info("Sent earnings autoprep gate ready Telegram alert")
        else:
            logger.warning("Autoprep gate flip: Telegram not configured")

    write_autoprep_gate_state(state, project_root=project_root)
    return {"flipped_to_ready": flipped, "was_ready": was_ready, "current_ready": current_ready}
