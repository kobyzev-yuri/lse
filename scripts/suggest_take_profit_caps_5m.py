#!/usr/bin/env python3
"""
Оценка разумных потолков тейка (GAME_5M_TAKE_PROFIT_PCT_<TICKER>) по 5m свечам за 7 торговых дней.

По каждой сессии (9:30–16:00 ET): макс. рост от открытия до хая сессии в %.
По тикерам: медиана, p70, p80 — предлагаемый потолок = округлённый p70 (достижим в ~70% сессий).

Запуск из корня репозитория:
  python scripts/suggest_take_profit_caps_5m.py
  python scripts/suggest_take_profit_caps_5m.py --tickers SNDK,MU,ASML
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def main() -> None:
    import numpy as np
    from services.ticker_groups import get_tickers_game_5m
    from services.recommend_5m import fetch_5m_ohlc, filter_to_last_n_us_sessions
    from config_loader import get_config_value

    tickers = get_tickers_game_5m()
    if not tickers:
        tickers = ["SNDK", "NBIS", "ASML", "MU", "LITE", "CIEN"]
    if "--tickers" in sys.argv:
        i = sys.argv.index("--tickers")
        if i + 1 < len(sys.argv):
            tickers = [t.strip().upper() for t in sys.argv[i + 1].split(",") if t.strip()]
    n_sessions = 7

    print("Тикеры:", ", ".join(tickers))
    print("Сессии: последние", n_sessions, "торговых дней (9:30–16:00 ET)")
    print()
    print(f"{'Ticker':<8} {'Сессий':<8} {'Медиана%':<10} {'p70%':<10} {'p80%':<10} {'Предлаг.':<10} {'Сейчас в конфиге':<20}")
    print("-" * 80)

    for ticker in tickers:
        df = fetch_5m_ohlc(ticker, days=10)
        if df is None or df.empty:
            print(f"{ticker:<8} нет 5m данных")
            continue
        df = filter_to_last_n_us_sessions(df, n=n_sessions)
        if df is None or df.empty or "_session" not in df.columns:
            # filter adds _session; if not present, group by date
            if "datetime" in df.columns:
                dt = df["datetime"]
                if hasattr(dt.dt, "date"):
                    df = df.copy()
                    df["_session"] = dt.dt.date
                else:
                    df["_session"] = dt.dt.normalize()
            else:
                print(f"{ticker:<8} нет сессий после фильтра")
                continue
        sessions = df.groupby("_session", sort=False)
        # Макс. рост от открытия сессии до хая сессии (%)
        up_pcts = []
        for _sess, grp in sessions:
            grp = grp.sort_values("datetime")
            open_ = float(grp["Open"].iloc[0])
            high = float(grp["High"].max())
            low = float(grp["Low"].min())
            if open_ <= 0:
                continue
            up_from_open = (high - open_) / open_ * 100.0
            up_pcts.append(up_from_open)
        if not up_pcts:
            print(f"{ticker:<8} 0 сессий с данными")
            continue
        arr = np.array(up_pcts)
        median_pct = float(np.median(arr))
        p70 = float(np.percentile(arr, 70))
        p80 = float(np.percentile(arr, 80))
        # Предлагаемый потолок: p70, округлённый до 0.5; минимум 2, макс 10
        suggested = round(p70 * 2) / 2.0
        suggested = max(2.0, min(10.0, suggested))
        # Текущее значение из конфига (если есть)
        key = f"GAME_5M_TAKE_PROFIT_PCT_{ticker.upper()}"
        current = get_config_value(key, "").strip()
        if not current:
            current = get_config_value("GAME_5M_TAKE_PROFIT_PCT", "7").strip()
            current = f"(общий {current})"
        print(f"{ticker:<8} {len(up_pcts):<8} {median_pct:<10.2f} {p70:<10.2f} {p80:<10.2f} {suggested:<10.1f} {current:<20}")

    print()
    print("Предлаг. = округлённый p70 (макс. рост от открытия до хая сессии за 7 дней).")
    print("Можно задать в config.env: GAME_5M_TAKE_PROFIT_PCT_<TICKER>=<число> (например GAME_5M_TAKE_PROFIT_PCT_ASML=4).")


if __name__ == "__main__":
    main()
