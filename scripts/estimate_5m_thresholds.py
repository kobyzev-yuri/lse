#!/usr/bin/env python3
"""
Оценка разумных порогов для ATR 5m и объёма (volume vs avg) по историческим 5m данным.

По тикерам игры 5m за последние 7 дней считает atr_5m_pct и volume_vs_avg_pct
на скользящем окне и выводит перцентили. По ним можно задать в config.env:
  GAME_5M_MIN_VOLUME_VS_AVG_PCT  — не входить, если объём < N% от среднего (напр. 40).
  GAME_5M_MAX_ATR_5M_PCT         — не входить или осторожность, если ATR 5m > N% цены (напр. 2.0).

Запуск:
  python scripts/estimate_5m_thresholds.py [тикер1,тикер2,...]
  Без аргументов — тикеры из get_tickers_game_5m().
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pandas as pd
from services.recommend_5m import fetch_5m_ohlc
from services.ticker_groups import get_tickers_game_5m


def atr_pct_and_volume_pct(df: pd.DataFrame, end_idx: int) -> tuple[float | None, float | None]:
    """По срезу df.iloc[:end_idx+1] считает ATR(14) в % от цены и volume vs avg (20 баров)."""
    if end_idx < 14 or "Close" not in df.columns:
        return None, None
    slice_df = df.iloc[: end_idx + 1].copy()
    high = slice_df["High"].astype(float)
    low = slice_df["Low"].astype(float)
    close = slice_df["Close"].astype(float)
    price = float(close.iloc[-1])
    prev_close = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - prev_close).abs(), (low - prev_close).abs()))
    tail = tr.iloc[-14:]
    mean_tr = tail.replace([np.inf, -np.inf], np.nan).dropna().mean()
    atr_pct = (float(mean_tr) / price * 100.0) if pd.notna(mean_tr) and mean_tr > 0 and price > 0 else None
    vol_pct = None
    if "Volume" in slice_df.columns:
        vol = slice_df["Volume"].replace(0, np.nan).dropna()
        if len(vol) >= 2:
            last_vol = float(vol.iloc[-1])
            tail_n = min(20, len(vol) - 1)
            avg_vol = float(vol.iloc[-tail_n - 1 : -1].mean())
            if avg_vol > 0:
                vol_pct = last_vol / avg_vol * 100.0
    return atr_pct, vol_pct


def main():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        tickers = [t.strip().upper() for t in sys.argv[1].strip().split(",") if t.strip()]
    else:
        tickers = get_tickers_game_5m() or ["SNDK", "MU"]
    days = 7
    atr_list = []
    vol_list = []
    step = 12  # каждый 12-й бар (~ раз в час), чтобы не перегружать
    for ticker in tickers:
        df = fetch_5m_ohlc(ticker, days=days)
        if df is None or df.empty or len(df) < 20:
            print(f"  {ticker}: нет данных или мало баров")
            continue
        df = df.sort_values("datetime").reset_index(drop=True)
        for i in range(20, len(df), step):
            atr_pct, vol_pct = atr_pct_and_volume_pct(df, i)
            if atr_pct is not None:
                atr_list.append(atr_pct)
            if vol_pct is not None:
                vol_list.append(vol_pct)
        print(f"  {ticker}: баров {len(df)}, сэмплов atr={sum(1 for j in range(20, len(df), step))}")
    if not atr_list and not vol_list:
        print("Нет данных для расчёта. Проверьте тикеры и доступ 5m (Yahoo).")
        return
    print()
    print("--- ATR 5m (% от цены) ---")
    if atr_list:
        a = np.array(atr_list)
        for p in (10, 25, 50, 75, 90):
            print(f"  p{p}: {np.percentile(a, p):.3f}%")
        print(f"  min: {a.min():.3f}%  max: {a.max():.3f}%")
        p90 = np.percentile(a, 90)
        print(f"  Рекомендация GAME_5M_MAX_ATR_5M_PCT: {p90:.2f} (или 2.0–3.0 для осторожности)")
    else:
        print("  Нет значений (мало данных).")
    print()
    print("--- Volume vs avg (%) ---")
    if vol_list:
        v = np.array(vol_list)
        for p in (10, 25, 50, 75, 90):
            print(f"  p{p}: {np.percentile(v, p):.1f}%")
        print(f"  min: {v.min():.1f}%  max: {v.max():.1f}%")
        p25 = np.percentile(v, 25)
        print(f"  Рекомендация GAME_5M_MIN_VOLUME_VS_AVG_PCT: {max(30, min(50, p25)):.0f} (не входить при объёме ниже)")
    else:
        print("  Нет значений (в 5m данных нет Volume или мало баров).")
    print()
    print("Пример для config.env:")
    if atr_list:
        p90 = np.percentile(np.array(atr_list), 90)
        print(f"  GAME_5M_MAX_ATR_5M_PCT={p90:.2f}")
    if vol_list:
        p25 = np.percentile(np.array(vol_list), 25)
        sug = max(30, min(50, p25))
        print(f"  GAME_5M_MIN_VOLUME_VS_AVG_PCT={sug:.0f}")


if __name__ == "__main__":
    main()
