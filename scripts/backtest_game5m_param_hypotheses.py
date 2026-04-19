#!/usr/bin/env python3
"""
Офлайн-подбор GAME_5M_* по **не закрытым** BUY (по умолчанию) — только trade_history + 5m-бары.

Режимы:
  open (по умолчанию) — каждая строка GAME_5M BUY без последующего SELL по тикеру;
  open_agg — одна строка на тикер: VWAP по всем открытым BUY; реплей/тейк от **последней** сделки, первая — в JSON как aggregate_first_entry_ts;
  json — legacy: список buy из сохранённого JSON сверки (строки без реплея 5m);
  bundle — полный отчёт анализатора закрытых сделок + блок гипотез (missed upside и т.д.).

Пример на инстансе (Docker):
  docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py \\
    --json-out /app/logs/hanger_tune_open.json

  # одна строка на тикер (VWAP по всем открытым BUY на символ):
  docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py --mode open_agg \\
    --json-out /app/logs/hanger_tune_open_agg.json

  # строже: учитывать «провисание» по close
  docker compose exec lse python scripts/backtest_game5m_param_hypotheses.py --require-sag \\
    --json-out /app/logs/hanger_tune_open.json

  Фон (терминал можно закрыть) — detached exec + лог в томе ``logs/``:
  ./scripts/run_game5m_param_hypotheses_docker_bg.sh --json-out /app/logs/hanger_tune_open.json

  Или вручную:
  docker compose exec -d lse env PYTHONUNBUFFERED=1 python -u scripts/backtest_game5m_param_hypotheses.py \\
    --log-file /app/logs/game5m_param_hypothesis_bg.log --json-out /app/logs/out.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine

from config_loader import get_database_url
from services.game5m_param_hypothesis_backtest import (
    run_hanger_tune_for_open_trades,
    run_hanger_tune_for_open_trades_aggregate,
    run_hanger_tune_from_take_json,
)
from services.trade_effectiveness_analyzer import analyze_trade_effectiveness


def _apply_log_file(path_str: str) -> TextIO:
    """Дублирует stdout/stderr в файл (append). Возвращает открытый файл — не закрывать до конца процесса."""
    p = Path(path_str.strip())
    if not p.is_absolute():
        p = project_root / p
    p.parent.mkdir(parents=True, exist_ok=True)
    fh: TextIO = open(p, "a", encoding="utf-8")
    fh.write(f"\n--- {_dt.datetime.now(_dt.timezone.utc).isoformat()} ---\n")
    fh.flush()

    class _Tee:
        __slots__ = ("_orig", "_fh")

        def __init__(self, orig: TextIO, fh: TextIO) -> None:
            self._orig = orig
            self._fh = fh

        def write(self, s: Any) -> int:
            if isinstance(s, bytes):
                s = s.decode("utf-8", errors="replace")
            elif not isinstance(s, str):
                s = str(s)
            self._orig.write(s)
            self._fh.write(s)
            self._fh.flush()
            return len(s)

        def flush(self) -> None:
            self._orig.flush()
            self._fh.flush()

        def fileno(self) -> int:
            return self._orig.fileno()

    sys.stdout = _Tee(sys.__stdout__, fh)  # type: ignore[assignment]
    sys.stderr = _Tee(sys.__stderr__, fh)  # type: ignore[assignment]
    return fh


def _resolve_skip_sag(*, mode: str, require_sag: bool, skip_sag: bool) -> bool:
    if require_sag:
        return False
    if skip_sag:
        return True
    return mode in ("open", "open_agg")


def _write_error_json(out_path: Path, *, err_type: str, message: str, tb: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "run_status": "error",
                "error_type": err_type,
                "message": message,
                "traceback": tb[:12000],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="GAME_5M: подбор потолка тейка по открытым BUY (по умолчанию) или legacy JSON"
    )
    p.add_argument(
        "--mode",
        choices=("open", "open_agg", "json", "bundle"),
        default="open",
        help="open — каждый открытый BUY; open_agg — агрегат по тикеру (VWAP); json — файл сверки; bundle — анализатор + гипотезы",
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
    p.add_argument("--require-sag", action="store_true", help="Требовать провисание по close (диагност, раздел 7)")
    p.add_argument("--skip-sag", action="store_true", help="Явно отключить фильтр провисания")
    p.add_argument("--hanger-days", type=int, default=6, help="Календарных дней окна для диагноста/свипа")
    p.add_argument("--sag-epsilon-log", type=float, default=0.0, help="Порог log(close/entry) для провисания")
    p.add_argument("--bar-horizon-days", type=int, default=10, help="Загрузка 5m-баров после входа")
    p.add_argument("--exchange", type=str, default="US")
    p.add_argument("--json-out", type=str, default="", help="Куда сохранить JSON")
    p.add_argument(
        "--log-file",
        type=str,
        default="",
        help="Дублировать stdout/stderr в файл (append), удобно с docker compose exec -d",
    )
    p.add_argument("--days", type=int, default=7, help="Для mode=bundle: глубина закрытых сделок")
    p.add_argument(
        "--include-trade-details",
        action="store_true",
        help="Для mode=bundle: trade_effects в JSON",
    )
    args = p.parse_args()

    json_out_arg = (args.json_out or "").strip()

    try:
        if (args.log_file or "").strip():
            _apply_log_file(args.log_file)

        engine = create_engine(get_database_url())
        skip_sag = _resolve_skip_sag(mode=args.mode, require_sag=args.require_sag, skip_sag=args.skip_sag)
        hd = max(4, min(10, int(args.hanger_days)))
        print(
            f"Старт расчёта: mode={args.mode} exchange={args.exchange} "
            f"hanger_days={hd} bar_horizon={max(6, int(args.bar_horizon_days))}",
            flush=True,
        )

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
            errs = sum(1 for h in hc if isinstance(h, dict) and h.get("error"))
            print(
                f"Открытых BUY: {n} | строк отчёта: {len(hc)} | cap: {tuned} | ошибок по строкам: {errs}",
                flush=True,
            )
        elif args.mode == "open_agg":
            payload = run_hanger_tune_for_open_trades_aggregate(
                engine=engine,
                exchange=str(args.exchange),
                hanger_calendar_days=hd,
                sag_epsilon_log=float(args.sag_epsilon_log),
                skip_sag_check=skip_sag,
                bar_horizon_days_after_entry=max(6, int(args.bar_horizon_days)),
            )
            meta = payload.get("meta") or {}
            n_rows = int(meta.get("open_buys_count", 0))
            n_tick = int(meta.get("tickers_count", len(payload.get("hanger_hypotheses") or [])))
            hc = payload.get("hanger_hypotheses") or []
            tuned = sum(1 for h in hc if isinstance(h, dict) and h.get("remediation_take_cap"))
            errs = sum(1 for h in hc if isinstance(h, dict) and h.get("error"))
            print(
                f"Агрегат: тикеров={n_tick} (строк BUY в БД: {n_rows}) | строк отчёта: {len(hc)} | "
                f"cap: {tuned} | ошибок: {errs}",
                flush=True,
            )
        elif args.mode == "json":
            jp = (args.from_json or "").strip()
            if not jp:
                print("Для mode=json укажите --from-json путь к JSON", file=sys.stderr)
                return 2
            path = Path(jp).expanduser()
            if not path.is_file():
                print(f"Файл не найден: {path.resolve()}", file=sys.stderr)
                return 2
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
            errs = sum(1 for h in hc if isinstance(h, dict) and h.get("error"))
            print(
                f"Кандидатов в JSON: {n} | строк отчёта: {len(hc)} | cap: {tuned} | ошибок по строкам: {errs}",
                flush=True,
            )
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
            if isinstance(hyp, dict) and hyp.get("status") == "error":
                print(f"Предупреждение: блок гипотез с ошибкой: {hyp.get('reason')}", file=sys.stderr)
            print(
                f"Bundle: trades={(rep.get('meta') or {}).get('trades_analyzed')} | "
                f"hangers={len(hyp.get('hanger_hypotheses') or [])} | "
                f"underprofit={len(hyp.get('underprofit_hypotheses') or [])}",
                flush=True,
            )

        payload["run_status"] = "ok"
        if json_out_arg:
            out = Path(json_out_arg)
            if not out.is_absolute():
                out = project_root / out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON: {out.resolve()}", flush=True)
        return 0
    except KeyboardInterrupt as e:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if json_out_arg:
            out = Path(json_out_arg)
            if not out.is_absolute():
                out = project_root / out
            try:
                _write_error_json(
                    out,
                    err_type="KeyboardInterrupt",
                    message=str(e) or "Interrupted",
                    tb=tb,
                )
                print(f"Прерывание записано в JSON: {out.resolve()}", file=sys.stderr)
            except OSError as w:
                print(f"Не удалось записать error JSON: {w}", file=sys.stderr)
        # Не re-raise: иначе KeyboardInterrupt вылетает в raise SystemExit(main()) и дублирует traceback.
        return 130
    except Exception as e:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if json_out_arg:
            out = Path(json_out_arg)
            if not out.is_absolute():
                out = project_root / out
            try:
                _write_error_json(
                    out,
                    err_type=type(e).__name__,
                    message=str(e),
                    tb=tb,
                )
                print(f"Ошибка записана в JSON: {out.resolve()}", file=sys.stderr)
            except OSError as w:
                print(f"Не удалось записать error JSON: {w}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
