#!/usr/bin/env python3
"""
Офлайн-подбор GAME_5M_* по **не закрытым** BUY (по умолчанию) — только trade_history + 5m-бары.

Режимы:
  open (по умолчанию) — все GAME_5M BUY без последующего SELL по тикеру;
  json — legacy: список buy из сохранённого JSON сверки (строки без реплея 5m);
  bundle — полный отчёт анализатора закрытых сделок + блок гипотез (missed upside и т.д.).

Пример на инстансе (Docker):
  docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py \\
    --json-out /app/logs/hanger_tune_open.json

  # строже: учитывать «провисание» по close
  docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py --require-sag \\
    --json-out /app/logs/hanger_tune_open.json
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
    run_hanger_tune_for_open_trades,
    run_hanger_tune_from_take_json,
)
from services.trade_effectiveness_analyzer import analyze_trade_effectiveness


def _resolve_skip_sag(*, mode: str, require_sag: bool, skip_sag: bool) -> bool:
    if require_sag:
        return False
    if skip_sag:
        return True
    return mode == "open"


def main() -> None:
    p = argparse.ArgumentParser(
        description="GAME_5M: подбор потолка тейка по открытым BUY (по умолчанию) или legacy JSON"
    )
    p.add_argument(
        "--mode",
        choices=("open", "json", "bundle"),
        default="open",
        help="open — не закрытые BUY в БД; json — файл сверки; bundle — анализатор + гипотезы",
    )
    p.add_argument(
        "--from-json",
        type=str,
        default="",
        help="Для mode=json: путь к JSON (например logs/game5m_take_5m_vs_30m.json)",
    )
    p.add_argument(
        "--json-both-null",
        action="store_true",
        help="Для mode=json: требовать replay_5m и replay_30m оба null (узкий фильтр)",
    )
    p.add_argument("--require-sag", action="store_true", help="Требовать провисание по close (диагност §7)")
    p.add_argument("--skip-sag", action="store_true", help="Явно отключить фильтр провисания")
    p.add_argument("--hanger-days", type=int, default=6, help="Календарных дней окна для диагноста/свипа")
    p.add_argument("--sag-epsilon-log", type=float, default=0.0, help="Порог log(close/entry) для провисания")
    p.add_argument("--bar-horizon-days", type=int, default=10, help="Загрузка 5m-баров после входа")
    p.add_argument("--exchange", type=str, default="US")
    p.add_argument("--json-out", type=str, default="", help="Куда сохранить JSON")
    p.add_argument("--days", type=int, default=7, help="Для mode=bundle: глубина закрытых сделок")
    p.add_argument(
        "--include-trade-details",
        action="store_true",
        help="Для mode=bundle: trade_effects в JSON",
    )
    args = p.parse_args()

    engine = create_engine(get_database_url())
    skip_sag = _resolve_skip_sag(mode=args.mode, require_sag=args.require_sag, skip_sag=args.skip_sag)
    hd = max(4, min(10, int(args.hanger_days)))

    if args.mode == "open":
        payload = run_hanger_tune_for_open_trades(
            engine=engine,
            exchange=str(args.exchange),
            hanger_calendar_days=hd,
            sag_epsilon_log=float(args.sag_epsilon_log),
            skip_sag_check=skip_sag,
            bar_horizon_days_after_entry=max(6, int(args.bar_horizon_days)),
        )
        n = int((payload.get("meta") or {}).get("open_buys_count", 0))
        hc = payload.get("hanger_hypotheses") or []
        tuned = sum(1 for h in hc if isinstance(h, dict) and h.get("remediation_take_cap"))
        print(f"Открытых BUY: {n} | строк отчёта: {len(hc)} | с предложением cap: {tuned}")
    elif args.mode == "json":
        jp = (args.from_json or "").strip()
        if not jp:
            print("Для mode=json укажите --from-json путь к JSON")
            sys.exit(2)
        path = Path(jp)
        if not path.is_file():
            print(f"Файл не найден: {path.resolve()}")
            sys.exit(2)
        payload = run_hanger_tune_from_take_json(
            engine=engine,
            json_path=path,
            exchange=str(args.exchange),
            hanger_calendar_days=hd,
            sag_epsilon_log=float(args.sag_epsilon_log),
            skip_sag_check=skip_sag,
            require_both_replays_null=bool(args.json_both_null),
            bar_horizon_days_after_entry=max(6, int(args.bar_horizon_days)),
        )
        n = int((payload.get("meta") or {}).get("candidates_in_json", 0))
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
