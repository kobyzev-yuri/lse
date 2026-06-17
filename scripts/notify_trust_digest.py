#!/usr/bin/env python3
"""Morning Telegram digest: unified trust arbiter (LSE Trust)."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_STATE_NAME = ".trust_digest_sent.json"


def _state_path(project_root: Path) -> Path:
    if Path("/app/logs").exists():
        base = Path("/app/logs/ml/ml_data_quality")
    else:
        base = project_root / "local" / "logs" / "ml_data_quality"
    return base / _STATE_NAME


def _cfg_bool(key: str, default: bool = True) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def main() -> int:
    ap = argparse.ArgumentParser(description="Telegram trust digest")
    ap.add_argument("--force", action="store_true", help="Send even if already sent today (UTC)")
    ap.add_argument("--refresh", action="store_true", help="Rebuild arbiter before send")
    args = ap.parse_args()

    from config_loader import get_config_value
    from services.telegram_signal import get_signal_chat_ids, send_telegram_message
    from services.unified_trust_arbiter import (
        build_unified_trust_arbiter,
        default_trust_arbiter_path,
        write_unified_trust_arbiter,
    )

    if args.refresh:
        write_unified_trust_arbiter(project_root=project_root)
    else:
        path = default_trust_arbiter_path(project_root)
        if not path.is_file():
            write_unified_trust_arbiter(project_root=project_root)

    arbiter = build_unified_trust_arbiter(project_root=project_root)
    text = str(arbiter.get("operator_digest_ru") or "").strip()
    if not text:
        logger.warning("Empty operator_digest_ru")
        return 1

    if not _cfg_bool("TRUST_DIGEST_TELEGRAM", True):
        logger.info("TRUST_DIGEST_TELEGRAM disabled")
        print(text)
        return 0

    token = (get_config_value("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_ids = get_signal_chat_ids()
    if not token or not chat_ids:
        logger.warning("Telegram not configured")
        print(text)
        return 0

    today = datetime.now(timezone.utc).date()
    state = _state_path(project_root)
    if not args.force and state.is_file():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            if str(data.get("last_sent_day") or "")[:10] == today.isoformat():
                logger.info("Trust digest already sent today")
                return 0
        except Exception:
            pass

    sent = False
    for cid in chat_ids:
        if send_telegram_message(token, cid, text, parse_mode=None):
            sent = True
    if sent:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"last_sent_day": today.isoformat(), "sent_at_utc": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        logger.info("Trust digest sent to Telegram")
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
