#!/usr/bin/env python3
"""
Анализ гэпа на открытие RTH и просадки в начале сессии vs VIX / Forex / нефть.

Метрики (по каждому торговому дню ET):
  - gap_open_pct: Open / PrevClose - 1 (гэп на открытие)
  - drop_day_low_pct: (Low - Open) / Open (внутридневная просадка от open; daily-прокси)
  - drop_first30m_pct: min(Low 9:30–10:00) / Open_9:30 - 1 (только при --intraday-days > 0)

Регрессия (OLS, numpy): gap_open(equity) ~ gap(VIX) + gap(Forex…) + gap(CL) + const.

Запуск:
  python scripts/analyze_macro_gap_indicators.py --days 400 --equity SMH
  python scripts/analyze_macro_gap_indicators.py --days 400 --equity SMH --intraday-days 60
  python scripts/analyze_macro_gap_indicators.py --days 400 --equities SMH,QQQ,SNDK
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

NYSE_OPEN = (9, 30)
FIRST30_END = (10, 0)


def _daily_ohlc_panel(ticker: str, days: int) -> "pd.DataFrame":
    import pandas as pd
    import yfinance as yf

    df = yf.Ticker(ticker).history(period=f"{days}d", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    prev_close = df["Close"].shift(1)
    out = pd.DataFrame(index=df.index)
    out[f"{ticker}|gap_open"] = (df["Open"] / prev_close - 1.0) * 100.0
    out[f"{ticker}|drop_day_low"] = np.where(
        df["Open"] > 0,
        (df["Low"] - df["Open"]) / df["Open"] * 100.0,
        np.nan,
    )
    out[f"{ticker}|ret_oc"] = np.where(
        df["Open"] > 0,
        (df["Close"] - df["Open"]) / df["Open"] * 100.0,
        np.nan,
    )
    return out.dropna(subset=[f"{ticker}|gap_open"])


def _first30m_drop_series(ticker: str, calendar_days: int) -> "pd.Series":
    """Просадка в первые 30 мин RTH: min Low / Open(9:30) - 1, %."""
    import datetime as dtmod

    import pandas as pd
    import yfinance as yf

    df = yf.Ticker(ticker).history(
        period=f"{calendar_days}d", interval="5m", prepost=False, auto_adjust=False
    )
    if df is None or df.empty:
        return pd.Series(dtype=float)
    df = df.rename_axis("Datetime").reset_index()
    dt_col = "Datetime" if "Datetime" in df.columns else "Date"
    dts = pd.to_datetime(df[dt_col])
    if dts.dt.tz is None:
        dts = dts.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        dts = dts.dt.tz_convert("America/New_York")
    df = df.assign(_dt=dts, _date=dts.dt.date, _time=dts.dt.time)
    t_open = dtmod.time(*NYSE_OPEN)
    t_end = dtmod.time(*FIRST30_END)
    rows: Dict = {}
    for day, g in df.groupby("_date"):
        win = g[(g["_time"] >= t_open) & (g["_time"] < t_end)]
        if win.empty or "Open" not in win.columns or "Low" not in win.columns:
            continue
        win = win.sort_values("_dt")
        o0 = float(win["Open"].iloc[0])
        if o0 <= 0:
            continue
        lo = float(win["Low"].min())
        rows[pd.Timestamp(day)] = (lo / o0 - 1.0) * 100.0
    s = pd.Series(rows)
    s.name = f"{ticker}|drop_first30m"
    return s


def _ols_with_stats(
    y: np.ndarray, x_cols: np.ndarray, col_names: List[str]
) -> Tuple[Dict[str, float], float, int, Dict[str, float]]:
    """
    OLS y ~ [1, x_cols]. Returns coef dict, R², n, p-values approx (normal).
    """
    n = len(y)
    k = x_cols.shape[1] + 1
    X = np.column_stack([np.ones(n), x_cols])
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    y_hat = X @ beta
    resid = y - y_hat
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    # standard errors
    dof = max(n - k, 1)
    sigma2 = ss_res / dof
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
        se = np.sqrt(np.diag(cov))
        tstat = beta / np.where(se > 1e-12, se, np.nan)
    except np.linalg.LinAlgError:
        se = np.full(k, np.nan)
        tstat = np.full(k, np.nan)
    names = ["const"] + col_names
    coef = {names[i]: float(beta[i]) for i in range(k)}
    p_approx = {}  # two-sided normal approx
    for i, name in enumerate(names):
        ti = tstat[i]
        if np.isfinite(ti):
            from math import erf, sqrt

            p_approx[name] = 2.0 * (1.0 - 0.5 * (1.0 + erf(abs(ti) / sqrt(2.0))))
        else:
            p_approx[name] = float("nan")
    return coef, r2, n, p_approx


def _print_corr_block(df: "pd.DataFrame", equity: str, targets: List[str], label: str) -> None:
    import pandas as pd

    eq_col = f"{equity}|gap_open"
    if eq_col not in df.columns:
        return
    print(f"\n=== {label}: корреляции с {equity}|gap_open (n={len(df)}) ===")
    for tcol in targets:
        for suffix in ("gap_open", "drop_day_low", "drop_first30m"):
            c = f"{tcol}|{suffix}"
            if c not in df.columns:
                continue
            sub = df[[eq_col, c]].dropna()
            if len(sub) < 20:
                continue
            r = sub[eq_col].corr(sub[c])
            print(f"  {c:28s}  r = {r:+.3f}  (n={len(sub)})")


def _run_regression(
    df: "pd.DataFrame",
    equity: str,
    predictors: List[str],
    y_suffix: str,
    title: str,
) -> Optional[Dict]:
    y_col = f"{equity}|{y_suffix}"
    if y_col not in df.columns:
        return None
    x_cols = []
    x_names = []
    for p in predictors:
        c = f"{p}|gap_open"
        if c in df.columns:
            x_cols.append(c)
            x_names.append(p)
    if not x_cols:
        return None
    sub = df[[y_col] + x_cols].dropna()
    if len(sub) < max(30, len(x_cols) + 5):
        print(f"\n⚠️  {title}: мало наблюдений (n={len(sub)})")
        return None
    y = sub[y_col].to_numpy(dtype=float)
    X = sub[x_cols].to_numpy(dtype=float)
    coef, r2, n, pvals = _ols_with_stats(y, X, x_names)
    print(f"\n=== {title} ===")
    print(f"y = {y_col}")
    print(f"X = {[f'{p}|gap_open' for p in x_names]}")
    print(f"n={n}, R²={r2:.4f}, adj_R²={1-(1-r2)*(n-1)/max(n-len(x_names)-1,1):.4f}")
    print("Коэффициенты (% equity на +1% индикатора):")
    for name, b in coef.items():
        pv = pvals.get(name, float("nan"))
        print(f"  {name:16s}  {b:+.4f}   p≈{pv:.3f}" if np.isfinite(pv) else f"  {name:16s}  {b:+.4f}")
    return {"coef": coef, "r2": r2, "n": n, "y": y_col, "x": x_names}


def main() -> None:
    parser = argparse.ArgumentParser(description="Гэп/просадка equity vs VIX/Forex/нефть + OLS")
    parser.add_argument("--days", type=int, default=400, help="Дневная история")
    parser.add_argument("--equity", default="", help="Один тикер (SMH)")
    parser.add_argument(
        "--equities",
        default="SMH,QQQ",
        help="Список equity для анализа (если --equity пуст)",
    )
    parser.add_argument(
        "--intraday-days",
        type=int,
        default=0,
        help="Догрузить drop_first30m по 5m (медленно, max ~60д yfinance)",
    )
    parser.add_argument("--no-oil", action="store_true", help="Исключить нефть из регрессии")
    args = parser.parse_args()

    import pandas as pd

    try:
        from services.macro_premarket_risk import (
            get_macro_forex_tickers,
            get_macro_oil_ticker,
            get_macro_vix_ticker,
        )
    except ImportError:
        def get_macro_vix_ticker() -> str:
            return "^VIX"

        def get_macro_oil_ticker() -> str:
            return "CL=F"

        def get_macro_forex_tickers() -> List[str]:
            return ["GBPUSD=X", "EURUSD=X"]

    equities = [args.equity.strip().upper()] if args.equity.strip() else [
        t.strip().upper() for t in args.equities.split(",") if t.strip()
    ]
    vix_t = get_macro_vix_ticker()
    oil_t = get_macro_oil_ticker()
    forex = get_macro_forex_tickers()
    macro = [vix_t] + forex + ([] if args.no_oil else [oil_t])

    all_tickers = list(dict.fromkeys(equities + macro))
    panels: List[pd.DataFrame] = []
    for t in all_tickers:
        try:
            p = _daily_ohlc_panel(t, args.days)
            if p.empty:
                print(f"⚠️  {t}: нет дневных данных")
            else:
                panels.append(p)
                print(f"✓ {t}: {len(p)} дней (daily)")
        except Exception as e:
            print(f"⚠️  {t}: {e}")

    if not panels:
        print("❌ Нет данных")
        sys.exit(1)

    df = pd.concat(panels, axis=1, sort=True).sort_index()
    # outer join для регрессии — отдельно dropna
    df_outer = df.copy()

    if args.intraday_days > 0:
        idays = min(args.intraday_days, 60)
        for eq in equities:
            try:
                s = _first30m_drop_series(eq, idays)
                if len(s) >= 10:
                    df_outer[s.name] = s
                    print(f"✓ {eq}: drop_first30m, {len(s)} дней (5m, {idays}d окно)")
            except Exception as e:
                print(f"⚠️  intraday {eq}: {e}")

    report_dir = project_root / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict = {"equities": {}, "days": args.days}

    for equity in equities:
        if f"{equity}|gap_open" not in df_outer.columns:
            continue
        df_eq = df_outer.dropna(subset=[f"{equity}|gap_open"], how="any")
        print(f"\n{'='*60}\n  {equity}\n{'='*60}")

        _print_corr_block(df_eq, equity, macro, "Гэп на открытие")
        _print_corr_block(df_eq, equity, macro, "Просадка от open до day low (daily)")

        # Квартили VIX / Forex
        eq_g = df_eq[f"{equity}|gap_open"]
        vix_c = f"{vix_t}|gap_open"
        if vix_c in df_eq.columns:
            v = df_eq[vix_c].dropna()
            if len(v) >= 20:
                hi = eq_g[df_eq[vix_c] >= v.quantile(0.75)].mean()
                lo = eq_g[df_eq[vix_c] <= v.quantile(0.25)].mean()
                print(f"\n--- Квартили VIX gap → {equity} gap_open ---")
                print(f"  VIX верх. 25%: {equity} gap ср. {hi:+.2f}%")
                print(f"  VIX ниж. 25%: {equity} gap ср. {lo:+.2f}%")

        preds = [vix_t] + forex + ([] if args.no_oil else [oil_t])
        reg_gap = _run_regression(
            df_eq,
            equity,
            preds,
            "gap_open",
            f"OLS: {equity} gap_open ~ VIX + Forex + oil",
        )
        reg_drop = _run_regression(
            df_eq,
            equity,
            preds,
            "drop_day_low",
            f"OLS: {equity} drop_day_low ~ macro gaps (open)",
        )
        if f"{equity}|drop_first30m" in df_eq.columns:
            _print_corr_block(df_eq, equity, macro, "Первые 30 мин RTH (5m)")
            _run_regression(
                df_eq,
                equity,
                preds,
                "drop_first30m",
                f"OLS: {equity} drop_first30m ~ macro gaps",
            )

        # Сохранение
        csv_path = report_dir / f"macro_gap_panel_{equity}_{len(df_eq)}d.csv"
        df_eq.to_csv(csv_path)
        summary["equities"][equity] = {
            "csv": str(csv_path),
            "reg_gap_open": reg_gap,
            "reg_drop_day_low": reg_drop,
        }
        print(f"\nПанель: {csv_path}")

    json_path = report_dir / "macro_gap_regression_summary.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nСводка регрессий: {json_path}")


if __name__ == "__main__":
    main()
