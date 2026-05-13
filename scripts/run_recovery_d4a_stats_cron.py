#!/usr/bin/env python3
"""
Накопление статистики D4a (recovery TIME_EXIT_EARLY): сводка τ×K в append-only JSONL.

  python scripts/run_recovery_d4a_stats_cron.py --dry-run
  python scripts/run_recovery_d4a_stats_cron.py --days 21

Окно дней: GAME_5M_RECOVERY_D4A_STATS_WINDOW_DAYS (default 21) или --days.
«Shallow» срез по 7/14/21/… дн. (только счётчики TE/gate в БД): GAME_5M_RECOVERY_D4A_STATS_SHALLOW_LOOKBACK_DAYS (default 30).
Пороги подсказки: GAME_5M_RECOVERY_D4A_STATS_SUFFICIENT_GATE_MIN (5), GAME_5M_RECOVERY_D4A_STATS_COMFORTABLE_GATE_MIN (10).
Выход: GAME_5M_RECOVERY_D4A_STATS_JSONL (default /app/logs/ml/recovery_d4a_rollup.jsonl в контейнере,
       иначе local/logs/ml/recovery_d4a_rollup.jsonl).

Сетка K: GAME_5M_RECOVERY_D4A_STATS_K_BARS (comma), плюс всегда включается GAME_5M_RECOVERY_LIVE_DEFER_BARS.
Порог выборки «лучшего» τ: GAME_5M_RECOVERY_D4A_STATS_MIN_DEFER (default 2).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_jsonl_path() -> Path:
    if Path("/app/logs").exists():
        p = Path("/app/logs/ml/recovery_d4a_rollup.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    d = project_root / "local" / "logs" / "ml"
    d.mkdir(parents=True, exist_ok=True)
    return d / "recovery_d4a_rollup.jsonl"


def main() -> int:
    from config_loader import get_config_value

    ap = argparse.ArgumentParser(description="Append D4a recovery τ×K rollup line to JSONL")
    ap.add_argument(
        "--days",
        type=int,
        default=None,
        help="Окно дней (default из GAME_5M_RECOVERY_D4A_STATS_WINDOW_DAYS или 21)",
    )
    ap.add_argument(
        "--jsonl-out",
        type=str,
        default="",
        help="Файл JSONL (default из GAME_5M_RECOVERY_D4A_STATS_JSONL или стандартный путь)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Только stdout, не писать файл")
    args = ap.parse_args()

    raw_days = args.days
    if raw_days is None:
        try:
            raw_days = int((get_config_value("GAME_5M_RECOVERY_D4A_STATS_WINDOW_DAYS", "21") or "21").strip())
        except (TypeError, ValueError):
            raw_days = 21
    days = max(1, min(30, int(raw_days)))

    try:
        lb_raw = (get_config_value("GAME_5M_RECOVERY_D4A_STATS_SHALLOW_LOOKBACK_DAYS", "30") or "30").strip()
        shallow_lb = max(days, min(30, int(lb_raw)))
    except (TypeError, ValueError):
        shallow_lb = max(days, 30)

    out_s = (args.jsonl_out or "").strip() or (get_config_value("GAME_5M_RECOVERY_D4A_STATS_JSONL", "") or "").strip()
    out_path = Path(out_s) if out_s else _default_jsonl_path()

    from services.trade_effectiveness_analyzer import (
        compute_recovery_ml_d4a_live_review_for_window,
        recovery_d4a_rollup_snapshot_for_jsonl,
    )

    review = compute_recovery_ml_d4a_live_review_for_window(
        days=days,
        strategy="GAME_5M",
        shallow_lookback_days=shallow_lb,
    )
    line = recovery_d4a_rollup_snapshot_for_jsonl(review, window_days=days)
    text = json.dumps(line, ensure_ascii=False, separators=(",", ":"))

    if args.dry_run:
        logger.info("dry-run: %s", text[:2000])
        print(text)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    logger.info("appended recovery D4a rollup → %s (mode=%s)", out_path, line.get("mode"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
