#!/usr/bin/env python3
"""
Офлайн-анализ точности прогноза гэпа (лог БД + опционально refit OLS).

  python scripts/analyze_game5m_gap_forecast.py --days 120
  python scripts/analyze_game5m_gap_forecast.py --days 400 --suggest-coefs

Печатает MAE/RMSE/знак и рекомендуемые GAME_5M_MACRO_PREDICT_* (refit SMH ~ macro gaps).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def _suggest_ols_coefs(rows: List[Dict[str, Any]], proxy: str) -> Optional[Dict[str, float]]:
    """Refit pred_sector на строках proxy с open + macro gaps из истории (yfinance)."""
    sys.path.insert(0, str(project_root / "scripts"))
    from analyze_macro_gap_indicators import _daily_ohlc_panel  # noqa: E402

    proxy = proxy.strip().upper()
    sub = [r for r in rows if str(r.get("symbol")).upper() == proxy and r.get("open_gap_pct") is not None]
    if len(sub) < 30:
        return None
    dates = sorted({r["trade_date"] for r in sub})
    if not dates:
        return None
    from services.macro_premarket_risk import get_macro_forex_tickers, get_macro_oil_ticker, get_macro_vix_ticker

    vix_t = get_macro_vix_ticker()
    forex = get_macro_forex_tickers()
    oil_t = get_macro_oil_ticker()
    tickers = [proxy, vix_t] + forex + [oil_t]
    panels = []
    for t in tickers:
        p = _daily_ohlc_panel(t, max(400, len(dates) + 50))
        if not p.empty:
            panels.append(p)
    if not panels:
        return None
    import pandas as pd

    df = pd.concat(panels, axis=1, sort=True)
    y_col = f"{proxy}|gap_open"
    if y_col not in df.columns:
        return None
    x_cols = [f"{vix_t}|gap_open"] + [f"{f}|gap_open" for f in forex] + [f"{oil_t}|gap_open"]
    x_cols = [c for c in x_cols if c in df.columns]
    sub_df = df.dropna(subset=[y_col] + x_cols, how="any")
    if len(sub_df) < 30:
        return None
    y = sub_df[y_col].to_numpy(dtype=float)
    X = np.column_stack([np.ones(len(y)), *(sub_df[c].to_numpy(dtype=float) for c in x_cols)])
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    names = ["const", "vix"] + [f"forex{i}" for i in range(len(forex))] + (["oil"] if oil_t in [c.split("|")[0] for c in x_cols] else [])
    out = {"const": round(float(beta[0]), 4)}
    out["beta_vix"] = round(float(beta[1]), 4) if len(beta) > 1 else 0.0
    idx = 2
    for i, f in enumerate(forex):
        if f"{f}|gap_open" in x_cols and idx < len(beta):
            key = "gbp" if "GBP" in f else "eur" if "EUR" in f else f"fx{i}"
            out[f"beta_{key}"] = round(float(beta[idx]), 4)
            idx += 1
    if f"{oil_t}|gap_open" in x_cols and idx < len(beta):
        out["beta_cl"] = round(float(beta[idx]), 4)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--suggest-coefs", action="store_true")
    args = parser.parse_args()

    from config_loader import get_config_value
    from services.game5m_gap_forecast import (
        ensure_gap_forecast_table,
        fetch_gap_forecast_rows,
        pool_gap_forecast_metrics,
    )
    from sqlalchemy import create_engine
    from config_loader import get_database_url

    eng = create_engine(get_database_url())
    ensure_gap_forecast_table(eng)
    proxy = (get_config_value("GAME_5M_MACRO_SECTOR_PROXY", "SMH") or "SMH").strip().upper()
    rows = fetch_gap_forecast_rows(eng, days=args.days)
    pooled = pool_gap_forecast_metrics(rows, sector_proxy=proxy)

    print(f"=== Gap forecast log ({args.days}d) ===")
    print(json.dumps(pooled, ensure_ascii=False, indent=2))

    if args.suggest_coefs:
        sug = _suggest_ols_coefs(rows, proxy)
        if sug:
            print("\n=== Suggested coefs (historical daily open, not from log) ===")
            print(json.dumps(sug, indent=2))
            print("\n# config.env.example mapping:")
            if "const" in sug:
                print(f"GAME_5M_MACRO_PREDICT_CONST={sug['const']}")
            if "beta_vix" in sug:
                print(f"GAME_5M_MACRO_PREDICT_BETA_VIX={sug['beta_vix']}")
        else:
            print("\n(suggest-coefs: недостаточно данных)")


if __name__ == "__main__":
    main()
