#!/usr/bin/env python3
"""
Legacy recovery-пайплайн: экспорт JSONL + train в одном скрипте.

**Рекомендуется:** ``run_recovery_ml_refresh.py`` (dispatcher poll / ``--slot weekly_full``).

  python3 scripts/run_recovery_ml_refresh.py --full

Этот файл сохранён для ручного запуска:
  python3 scripts/run_daily_game5m_recovery_pipeline.py

Cron на хосте (пример; часовой пояс — TZ хоста):
  40 23 * * 1-5 cd /path/to/lse && docker compose exec -T lse python3 scripts/run_daily_game5m_recovery_pipeline.py >> /path/to/lse/logs/game5m_daily_recovery_pipeline.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return default


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily GAME_5M recovery ML: export + train + log")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="Strategy scope (default GAME_5M)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    days = max(7, min(60, _env_int("DAILY_RECOVERY_DAYS", 30)))
    skip_train = _env_bool("DAILY_RECOVERY_SKIP_TRAIN", False)
    export_only = _env_bool("DAILY_RECOVERY_EXPORT_ONLY", False)
    horizon = max(15, min(780, _env_int("DAILY_RECOVERY_HORIZON_MINUTES", 120)))

    # Where to append a daily summary line (optional)
    report_jsonl = os.environ.get("DAILY_RECOVERY_REPORT_JSONL") or (
        "/app/logs/ml/logs/game5m_recovery_daily_report.jsonl" if Path("/app/logs").exists() else str(project_root / "local" / "logs" / "game5m_recovery_daily_report.jsonl")
    )
    report_path = Path(report_jsonl)

    from services.trade_effectiveness_analyzer import analyze_trade_effectiveness

    logger.info("export recovery jsonl: days=%s strategy=%s", days, args.strategy)
    payload = analyze_trade_effectiveness(days=days, strategy=args.strategy, export_recovery_ml=True, use_llm=False)
    exp = payload.get("game5m_hold_recovery_export") if isinstance(payload, dict) else None
    if not isinstance(exp, dict) or exp.get("status") != "ok":
        logger.error("export failed/skipped: %s", exp)
        _append_jsonl(
            report_path,
            {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "status": "export_failed",
                "days": days,
                "strategy": args.strategy,
                "export": exp,
            },
        )
        return 2

    jsonl_path = str(exp.get("path") or "")
    lines = int(exp.get("lines_written") or 0)
    logger.info("export ok: %s (lines=%s)", jsonl_path, lines)

    train_meta: Optional[Dict[str, Any]] = None
    train_status = "skipped"

    if export_only:
        train_status = "skipped_export_only"
    elif skip_train:
        train_status = "skipped_by_env"
    else:
        logger.info("train recovery model: horizon=%s jsonl=%s", horizon, jsonl_path)
        # Import and run the training script as a subprocess for consistency with CLI behavior.
        import subprocess

        cmd = [
            sys.executable,
            str(project_root / "scripts" / "train_game5m_recovery_catboost.py"),
            "--jsonl",
            jsonl_path,
            "--horizon",
            str(horizon),
        ]
        p = subprocess.run(cmd, cwd=str(project_root))
        rc = int(p.returncode)
        train_status = "ok" if rc == 0 else f"exit_{rc}"

        # Attempt to read latest meta written by the train script default path
        try:
            from config_loader import get_config_value
            from services.game5m_recovery_catboost import default_recovery_catboost_model_path

            mp = (get_config_value("GAME_5M_RECOVERY_CATBOOST_MODEL_PATH", "") or "").strip()
            model_path = Path(mp) if mp else default_recovery_catboost_model_path()
            meta_path = model_path.with_suffix(".meta.json")
            if meta_path.is_file():
                train_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            train_meta = None

    _append_jsonl(
        report_path,
        {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "ok",
            "days": days,
            "strategy": args.strategy,
            "export": {"path": jsonl_path, "lines_written": lines},
            "train_status": train_status,
            "train_meta": train_meta,
        },
    )
    logger.info("done: train_status=%s report_jsonl=%s", train_status, report_path)
    return 0 if train_status in ("ok", "skipped_export_only", "skipped_by_env") else 3


if __name__ == "__main__":
    raise SystemExit(main())

