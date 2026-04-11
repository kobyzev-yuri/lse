#!/usr/bin/env python3
"""
Снимок JSON отчёта анализатора (тот же расчёт, что /api/analyzer), без HTTP.
Для cron: регулярно складывать отчёты на диск для диффов и внешних обработчиков.

Пример crontab (каждый день 06:30 по серверу, каталог рядом с репо ~/lse):
  30 6 * * * cd /home/USER/lse && python3 scripts/snapshot_analyzer_report.py --days 7 >> logs/analyzer_snapshot.log 2>&1
  (по умолчанию снимки в local/analyzer_snapshots/; переопределение: ANALYZER_SNAPSHOT_DIR=...)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.trade_effectiveness_analyzer import analyze_trade_effectiveness


def main() -> None:
    parser = argparse.ArgumentParser(description="Сохранить JSON отчёт анализатора в каталог снимков")
    parser.add_argument("--days", type=int, default=7, help="Окно сделок, дней (1–30)")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="Стратегия")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Каталог для файлов (по умолчанию env ANALYZER_SNAPSHOT_DIR или local/analyzer_snapshots в корне репо)",
    )
    parser.add_argument("--llm", action="store_true", help="Включить LLM (дорого для ежедневного cron)")
    parser.add_argument(
        "--no-trade-details",
        action="store_true",
        help="Не добавлять trade_effects (меньше файлы)",
    )
    parser.add_argument("--quiet", action="store_true", help="Не печатать путь в stdout")
    args = parser.parse_args()

    days = max(1, min(30, int(args.days)))
    strategy = (args.strategy or "GAME_5M").strip().upper()

    raw_dir = (args.out_dir or os.environ.get("ANALYZER_SNAPSHOT_DIR") or "").strip()
    if raw_dir:
        out_dir = Path(raw_dir)
    else:
        out_dir = project_root / "local" / "analyzer_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"analyzer_{strategy}_{days}d_{ts}.json"
    out_path = out_dir / name

    payload = analyze_trade_effectiveness(
        days=days,
        strategy=strategy,
        use_llm=bool(args.llm),
        include_trade_details=not bool(args.no_trade_details),
    )
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(str(out_path))
        print(str(latest))


if __name__ == "__main__":
    main()
