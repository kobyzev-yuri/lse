#!/usr/bin/env python3
"""
Обучение JSON-артефактов ridge по мультидневным лог-доходностям (GAME_5M multiday LR).

Использует services.multiday_lr_pipeline: дневные close из quotes или Yahoo,
опционально premarket_daily_features (артефакт v2).

Примеры:
  python scripts/train_game5m_multiday_lr.py AAPL MSFT
  python scripts/train_game5m_multiday_lr.py --tickers-source game5m --dry-run
  python scripts/train_game5m_multiday_lr.py --tickers-source config --json-metrics-out /tmp/mlr.json
  python scripts/train_game5m_multiday_lr.py --tickers-source merged --source auto
  python scripts/train_game5m_multiday_lr.py NVDA --no-premarket
  python scripts/train_game5m_multiday_lr.py TSLA --source yahoo --period-days 500

Источники тикеров (как в рантайме / Telegram):
  manual   — явный список в конце командной строки (по умолчанию).
  game5m   — GAME_5M_TICKERS из config.env, иначе TICKERS_FAST (см. services.ticker_groups.get_tickers_game_5m).
  config   — FAST + MEDIUM + LONG без дублей (get_config_ticker_symbols_upper_unique).
  merged   — DISTINCT ticker из quotes ∪ группы конфига, затем sort — как /tickers в telegram_bot.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def resolve_tickers_from_source(source: str, engine: Optional[Any]) -> List[str]:
    """Список тикеров для обучения: см. --tickers-source в docstring."""
    src = (source or "manual").strip().lower()
    if src == "game5m":
        from services.ticker_groups import get_tickers_game_5m

        out = [str(t).strip().upper() for t in get_tickers_game_5m() if str(t).strip()]
        return list(dict.fromkeys(out))
    if src == "config":
        from services.ticker_groups import get_config_ticker_symbols_upper_unique

        return list(get_config_ticker_symbols_upper_unique())
    if src == "merged":
        from sqlalchemy import text

        from services.ticker_groups import get_all_ticker_groups

        from_quotes: List[str] = []
        if engine is not None:
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker"))
                    from_quotes = [str(r[0]).strip().upper() for r in rows if r and r[0]]
            except Exception as e:
                logger.warning("merged: quotes DISTINCT failed: %s", e)
        seen: set[str] = set()
        ordered: List[str] = []
        for t in from_quotes + get_all_ticker_groups():
            u = str(t).strip().upper()
            if u and u not in seen:
                seen.add(u)
                ordered.append(u)
        return sorted(ordered)
    raise ValueError(f"unknown tickers source: {source!r}")


def main() -> int:
    p = argparse.ArgumentParser(description="Train multiday LR ridge JSON artifacts per ticker.")
    p.add_argument(
        "tickers",
        nargs="*",
        default=[],
        help="Ticker symbols when --tickers-source=manual (default). Ignored for game5m/config/merged.",
    )
    p.add_argument(
        "--tickers-source",
        choices=("manual", "game5m", "config", "merged"),
        default="manual",
        help="manual: CLI list; game5m: GAME_5M_TICKERS or TICKERS_FAST; config: FAST+MEDIUM+LONG; merged: quotes∪config (as /tickers)",
    )
    p.add_argument("--period-days", type=int, default=400, help="Yahoo window if source needs Yahoo")
    p.add_argument(
        "--source",
        choices=("auto", "quotes", "yahoo"),
        default="auto",
        help="Daily close source (auto: quotes in DB if enough rows, else Yahoo)",
    )
    p.add_argument(
        "--no-premarket",
        action="store_true",
        help="Disable premarket_daily_features (artifact v1, 7+2 features)",
    )
    p.add_argument("--min-train-rows", type=int, default=80)
    p.add_argument(
        "--json-metrics-out",
        type=str,
        default="",
        help="Write per-ticker training metrics (lambda grid, RMSE per horizon) to this JSON file",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fit and print metrics but do not write JSON artifacts to disk",
    )
    args = p.parse_args()

    from report_generator import get_engine

    from services.multiday_lr_pipeline import (
        fit_artifact_for_ticker,
        resolve_daily_close_series,
        save_artifact,
    )

    engine = None
    try:
        engine = get_engine()
    except Exception as e:
        logger.warning("DB engine unavailable: %s", e)

    if args.tickers_source == "manual":
        ticker_list = [str(x).strip().upper() for x in (args.tickers or []) if str(x).strip()]
        if not ticker_list:
            p.error("with --tickers-source=manual, pass at least one ticker, e.g. scripts/train_game5m_multiday_lr.py SNDK MU")
    else:
        if args.tickers:
            logger.warning("Positional tickers ignored (--tickers-source=%s)", args.tickers_source)
        try:
            ticker_list = resolve_tickers_from_source(args.tickers_source, engine)
        except ValueError as e:
            p.error(str(e))
        if not ticker_list:
            p.error(f"--tickers-source={args.tickers_source!r} produced an empty list")
        logger.info("Resolved %s tickers from %s (showing first 20): %s", len(ticker_list), args.tickers_source, ticker_list[:20])

    use_pm = not args.no_premarket and engine is not None
    if args.no_premarket:
        logger.info("Premarket DB features disabled (--no-premarket).")
    elif engine is None:
        logger.info("No DB engine: training without premarket (v1).")

    ok = 0
    metrics_rows: List[Dict[str, Any]] = []
    for raw in ticker_list:
        t = str(raw).strip().upper()
        if not t:
            continue
        closes, src = resolve_daily_close_series(
            t,
            period_days=int(args.period_days),
            engine=engine,
            source=str(args.source),
        )
        if closes is None or len(closes) < 30:
            logger.warning("%s: no daily closes (source=%s)", t, src)
            continue
        art = fit_artifact_for_ticker(
            t,
            closes,
            engine=engine,
            use_premarket_db=use_pm,
            min_train_rows=int(args.min_train_rows),
            training_source=src,
        )
        if art is None:
            logger.warning("%s: fit failed (min rows / horizons)", t)
            continue
        tr = art.get("training") or {}
        hz = art.get("horizons") or {}
        per_h: Dict[str, Any] = {}
        for hk, block in hz.items():
            if not isinstance(block, dict):
                continue
            per_h[str(hk)] = {
                "train_rmse_in_sample_log": block.get("train_rmse_in_sample_log"),
                "n_train": block.get("n_train"),
            }
        logger.info(
            "%s ver=%s closes=%s selected_λ=%s premarket_db=%s holdout_frac=%s",
            t,
            art.get("artifact_version"),
            tr.get("n_rows"),
            tr.get("ridge_lambda"),
            tr.get("use_premarket_db"),
            tr.get("holdout_frac"),
        )
        if tr.get("lambda_grid_cv"):
            logger.info("%s lambda_grid_cv: %s", t, tr.get("lambda_grid_cv"))
        logger.info("%s per_horizon_in_sample_rmse_log: %s", t, json.dumps(per_h, ensure_ascii=False))
        metrics_rows.append(
            {
                "ticker": t,
                "tickers_source": args.tickers_source,
                "artifact_version": art.get("artifact_version"),
                "training_source": src,
                "training": tr,
                "horizons_metrics": per_h,
            }
        )
        if args.dry_run:
            logger.info("%s dry-run: skip save_artifact", t)
        else:
            path = save_artifact(t, art)
            logger.info("%s saved -> %s", t, path)
        ok += 1

    out_path = (args.json_metrics_out or "").strip()
    if out_path and metrics_rows:
        outp = Path(out_path)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(metrics_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Wrote metrics JSON: %s", outp.resolve())

    logger.info("Done: %s ticker(s) fitted%s.", ok, " (dry-run, no files)" if args.dry_run else "")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
