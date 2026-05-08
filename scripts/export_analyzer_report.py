#!/usr/bin/env python3
"""
Экспорт JSON анализатора эффективности сделок — тот же расчёт, что GET /api/analyzer (веб).

Важно: вызывает analyze_trade_effectiveness() напрямую, поэтому LLM работает даже при
WEB_DEMO_MODE=1 (веб отключает только HTTP-эндпоинты).

Примеры:
  cd /app && python3 scripts/export_analyzer_report.py --days 1 --use-llm > report.json
  python3 scripts/export_analyzer_report.py --days 1 --strategy GAME_5M --use-llm --output /tmp/a.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.trade_effectiveness_analyzer import analyze_trade_effectiveness
from web_app import _to_jsonable


def main() -> int:
    p = argparse.ArgumentParser(description="Export analyzer JSON (same as /api/analyzer).")
    p.add_argument("--days", type=int, default=1, help="Окно в днях (1–30), по умолчанию 1")
    p.add_argument(
        "--strategy",
        type=str,
        default="GAME_5M",
        help="GAME_5M | ALL | Portfolio (как в веб-анализаторе)",
    )
    p.add_argument(
        "--use-llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Включить блок llm (как галочка в вебе). По умолчанию: включено",
    )
    p.add_argument(
        "--include-trade-details",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Добавить trade_effects (как «json + trade_effects»). По умолчанию: да",
    )
    p.add_argument(
        "--output",
        "-o",
        type=str,
        default="",
        help="Файл; если не задан — печать в stdout (UTF-8)",
    )
    args = p.parse_args()

    payload = analyze_trade_effectiveness(
        days=int(args.days),
        strategy=(args.strategy or "GAME_5M").strip().upper(),
        use_llm=bool(args.use_llm),
        include_trade_details=bool(args.include_trade_details),
        export_recovery_ml=False,
    )
    body = _to_jsonable(payload)
    text = json.dumps(body, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
