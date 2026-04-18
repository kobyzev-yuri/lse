#!/usr/bin/env python3
"""
Офлайн-подбор GAME_5M_* (выходные / без торгов):

  • висяки — кандидаты из ``game5m_take_5m_vs_30m.json`` (replay_5m/30m null) + сетка снижения
    ``GAME_5M_TAKE_PROFIT_PCT_<TICKER>``;
  • полный bundle — как у анализатора: «старое» окно BUY + недобор по missed upside из закрытых сделок.

Примеры:
  python scripts/backtest_game5m_param_hypotheses.py \\
    --from-json logs/game5m_take_5m_vs_30m.json --skip-sag \\
    --json-out local/hanger_tune_weekend.json

  python scripts/backtest_game5m_param_hypotheses.py --mode bundle --days 7 \\
    --json-out local/game5m_param_bundle.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine

from config_loader import get_database_url
from services.game5m_param_hypothesis_backtest import (
    run_game5m_hypothesis_bundle,
    run_hanger_tune_from_take_json,
)
from services.trade_effectiveness_analyzer import analyze_trade_effectiveness


def main() -> None:
    p = argparse.ArgumentParser(description="GAME_5M: офлайн гипотезы по параметрам (висяки / недобор)")
    p.add_argument(
        "--mode",
        choices=("json-hangers", "bundle"),
        default="json-hangers",
        help="json-hangers — кандидаты из --from-json; bundle — как analyze_trade_effectiveness + гипотезы",
    )
    p.add_argument(
        "--from-json",
        type=str,
        default="",
        help="Путь к game5m_take_5m_vs_30m.json (режим json-hangers)",
    )
    p.add_argument(
        "--candidates-5m-only",
        action="store_true",
        help="Кандидаты только с replay_5m=null (а 30m может быть)",
    )
    p.add_argument("--skip-sag", action="store_true", help="Не требовать «провисание» по close (для крутёжки в выходные)")
    p.add_argument("--hanger-days", type=int, default=6, help="Календарных дней окна для диагноста/свипа")
    p.add_argument("--sag-epsilon-log", type=float, default=0.0, help="Порог log(close/entry) для провисания")
    p.add_argument("--bar-horizon-days", type=int, default=10, help="Загрузка 5m-баров после входа (json-hangers)")
    p.add_argument("--exchange", type=str, default="US")
    p.add_argument("--json-out", type=str, default="", help="Куда сохранить JSON")
    p.add_argument("--days", type=int, default=7, help="Для mode=bundle: глубина закрытых сделок анализатора")
    p.add_argument(
        "--include-trade-details",
        action="store_true",
        help="Для mode=bundle: положить trade_effects в JSON (тяжелее)",
    )
    args = p.parse_args()

    engine = create_engine(get_database_url())

    if args.mode == "json-hangers":
        jp = (args.from_json or "").strip()
        if not jp:
            print("Укажите --from-json путь к JSON от backtest_game5m_take_5m_vs_30m.py")
            sys.exit(2)
        path = Path(jp)
        if not path.is_file():
            print(f"Файл не найден: {path.resolve()}")
            sys.exit(2)
        payload = run_hanger_tune_from_take_json(
            engine=engine,
            json_path=path,
            exchange=str(args.exchange),
            hanger_calendar_days=max(4, min(10, int(args.hanger_days))),
            sag_epsilon_log=float(args.sag_epsilon_log),
            skip_sag_check=bool(args.skip_sag),
            require_both_replays_null=not bool(args.candidates_5m_only),
            bar_horizon_days_after_entry=max(6, int(args.bar_horizon_days)),
        )
        n = int(payload.get("meta") or {}).get("candidates_in_json", 0)  # type: ignore
        hc = payload.get("hanger_hypotheses") or []
        tuned = sum(1 for h in hc if isinstance(h, dict) and h.get("remediation_take_cap"))
        print(f"Кандидатов в JSON: {n} | строк отчёта: {len(hc)} | с предложением cap: {tuned}")
    else:
        rep = analyze_trade_effectiveness(
            days=max(1, min(30, int(args.days))),
            strategy="GAME_5M",
            use_llm=False,
            include_trade_details=bool(args.include_trade_details),
            include_game5m_param_hypothesis_backtest=True,
        )
        payload = {
            "meta": {
                "mode": "bundle",
                "analyzer_days": int(args.days),
                "trades_analyzed": (rep.get("meta") or {}).get("trades_analyzed"),
            },
            "analyzer_summary": rep.get("summary"),
            "game5m_param_hypothesis_backtest": rep.get("game5m_param_hypothesis_backtest"),
        }
        hyp = rep.get("game5m_param_hypothesis_backtest") or {}
        print(
            f"Bundle: trades={(rep.get('meta') or {}).get('trades_analyzed')} | "
            f"hangers={len(hyp.get('hanger_hypotheses') or [])} | "
            f"underprofit={len(hyp.get('underprofit_hypotheses') or [])}"
        )

    if args.json_out.strip():
        out = Path(args.json_out.strip())
        if not out.is_absolute():
            out = project_root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON: {out.resolve()}")


if __name__ == "__main__":
    main()
