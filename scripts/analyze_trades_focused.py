#!/usr/bin/env python3
"""Узкий анализ закрытых сделок за несколько дней с фильтром по тикерам и/или trade_id + подсказки GAME_5M."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.trade_effectiveness_analyzer import (
    analyze_trade_effectiveness_focused,
    format_trade_effectiveness_text,
)


def _parse_tickers(s: str) -> Optional[List[str]]:
    s = (s or "").strip()
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_trade_ids(s: str) -> Optional[List[int]]:
    s = (s or "").strip()
    if not s:
        return None
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out or None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Узкий анализ: последние N дней, опционально только выбранные тикеры и/или trade_id выхода"
    )
    parser.add_argument("--days", type=int, default=4, help="Окно в днях (1–30, по умолчанию 4)")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="Стратегия (GAME_5M|ALL|...)")
    parser.add_argument(
        "--tickers",
        type=str,
        default="",
        help="Через запятую, например SNDK,AAPL (пусто = все сделки в окне)",
    )
    parser.add_argument(
        "--trade-ids",
        type=str,
        default="",
        dest="trade_ids",
        help="Через запятую id закрывающей сделки (trade_id из TradePnL)",
    )
    parser.add_argument("--llm", action="store_true", help="LLM с фокусом на config_env_proposals (GAME_5M_*)")
    parser.add_argument(
        "--include-trade-details",
        action="store_true",
        help="В JSON добавить trade_effects по каждой сделке в выборке",
    )
    parser.add_argument("--json-out", type=str, default="", help="Путь для сохранения JSON отчёта")
    args = parser.parse_args()

    days = max(1, min(30, int(args.days)))
    tickers = _parse_tickers(args.tickers)
    trade_ids = _parse_trade_ids(args.trade_ids)

    payload = analyze_trade_effectiveness_focused(
        days=days,
        strategy=args.strategy,
        tickers=tickers,
        trade_ids=trade_ids,
        use_llm=bool(args.llm),
        include_trade_details=bool(args.include_trade_details),
    )
    print(format_trade_effectiveness_text(payload))

    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = project_root / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON отчёт сохранён: {out_path}")


if __name__ == "__main__":
    main()
