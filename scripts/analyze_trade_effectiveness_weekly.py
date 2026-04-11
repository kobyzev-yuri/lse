#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.trade_effectiveness_analyzer import analyze_trade_effectiveness, format_trade_effectiveness_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Анализ эффективности закрытых сделок за период")
    parser.add_argument("--days", type=int, default=7, help="Период анализа в днях (по умолчанию 7)")
    parser.add_argument("--strategy", type=str, default="GAME_5M", help="Стратегия (GAME_5M|ALL|...)")
    parser.add_argument("--llm", action="store_true", help="Добавить LLM-рекомендации")
    parser.add_argument(
        "--include-trade-details",
        action="store_true",
        help="В JSON добавить trade_effects — все сделки с метриками (для локального LLM/jq)",
    )
    parser.add_argument("--json-out", type=str, default="", help="Путь для сохранения JSON отчёта")
    args = parser.parse_args()

    days = max(1, min(30, int(args.days)))
    payload = analyze_trade_effectiveness(
        days=days,
        strategy=args.strategy,
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

