#!/usr/bin/env python3
"""
Shadow-анализ options_sentiment gate для GAME_5M (фаза 4).

Что делает
----------
1. **Закрытые сделки** (последние N дней): для каждого GAME_5M входа читает context_json,
   извлекает CORE (BUY/STRONG_BUY) и options gate_hint (would_downgrade / would_signal).
   Сопоставляет would_downgrade с realized PnL:
   - false_positive — gate отрезал бы, но сделка в плюсе (ложное отсечение);
   - true_positive — gate совпал с убытком.

2. **Live-срез** (опционально): get_decision_5m по всем тикерам GAME_5M — доля CORE=BUY,
   где сейчас сработал бы shadow-downgrade (prod-вход не меняется).

3. **Рекомендация** ready_for_apply_discussion: эвристика для перехода к фазе 5
   (не включает apply автоматически).

Не меняет торговлю. DECISION_STACK_OPTIONS_SENTIMENT_GATE_MODE остаётся log_only.

Usage
-----
  python3 scripts/analyze_options_gate_shadow.py
  python3 scripts/analyze_options_gate_shadow.py --days 28 --json-out local/logs/ml_data_quality/last_options_gate_shadow.json
  python3 scripts/analyze_options_gate_shadow.py --no-live-scan

На prod (в контейнере):
  docker exec lse-bot python scripts/analyze_options_gate_shadow.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from services.options_gate_shadow import (  # noqa: E402
    build_options_gate_shadow_report,
    default_report_path,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Options sentiment gate shadow report (GAME_5M).")
    ap.add_argument("--days", type=int, default=28, help="Окно закрытых сделок (1–90)")
    ap.add_argument("--live-days", type=int, default=5, help="days= для live get_decision_5m")
    ap.add_argument("--no-live-scan", action="store_true", help="Пропустить live-срез (быстрее)")
    ap.add_argument("--limit-rows", type=int, default=30, help="Сколько recent_rows в JSON")
    ap.add_argument(
        "--focus",
        default="SNDK,MU,LITE",
        help="Тикеры для проверки ложных downgrade (через запятую)",
    )
    ap.add_argument(
        "--json-out",
        default="",
        help="Путь к JSON (по умолчанию: ml_data_quality/last_options_gate_shadow.json)",
    )
    ap.add_argument(
        "--fail-if-ready",
        action="store_true",
        help="Exit 1 если ready_for_apply_discussion=true (cron gate перед apply)",
    )
    args = ap.parse_args()

    focus = [x.strip().upper() for x in (args.focus or "").split(",") if x.strip()]
    report = build_options_gate_shadow_report(
        days=args.days,
        focus_tickers=focus or None,
        limit_rows=max(0, args.limit_rows),
        live_scan=not args.no_live_scan,
        live_days=max(1, min(7, args.live_days)),
    )

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)

    out_path = Path(args.json_out) if args.json_out else default_report_path(project_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"\nWrote: {out_path}", file=sys.stderr)

    closed = report.get("closed_trades") if isinstance(report.get("closed_trades"), dict) else {}
    summary = (
        f"closed={closed.get('total_closed', 0)} "
        f"with_options={closed.get('with_options_context', 0)} "
        f"bull_downgrade={closed.get('bull_with_would_downgrade', 0)} "
        f"false_pos={closed.get('downgrade_false_positive', 0)}"
    )
    print(summary, file=sys.stderr)

    rec = report.get("recommendation") if isinstance(report.get("recommendation"), dict) else {}
    if args.fail_if_ready and rec.get("ready_for_apply_discussion"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
