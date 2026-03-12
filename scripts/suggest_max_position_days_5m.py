#!/usr/bin/env python3
"""
Анализ: за сколько торговых дней цена от открытия сессии достигает уровня тейка (потолок % из конфига).

Нужен для тикеров, которые растут 2–4 дня подряд: за 1 день тейк не достигается, за 2–3 может.
По каждому «виртуальному входу» (открытие сессии S) смотрим: на какой день T (S, S+1, S+2, …)
максимум цен от S до T впервые >= open_S * (1 + take_pct/100). Дни до достижения = T - S + 1.

Вывод: медиана, p70, p80 торговых дней; предлагаемый GAME_5M_MAX_POSITION_DAYS_<TICKER> = ceil(p80).

Запуск из корня репозитория:
  python scripts/suggest_max_position_days_5m.py
  python scripts/suggest_max_position_days_5m.py --tickers NBIS,SNDK,MU --sessions 25
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
    from services.game_5m import _take_profit_cap_pct
    from config_loader import get_config_value

    tickers = get_tickers_game_5m()
    if not tickers:
        tickers = ["SNDK", "NBIS", "ASML", "MU", "LITE", "CIEN"]
    n_sessions = 25
    if "--tickers" in sys.argv:
        i = sys.argv.index("--tickers")
        if i + 1 < len(sys.argv):
            tickers = [t.strip().upper() for t in sys.argv[i + 1].split(",") if t.strip()]
    if "--sessions" in sys.argv:
        i = sys.argv.index("--sessions")
        if i + 1 < len(sys.argv):
            try:
                n_sessions = max(5, int(sys.argv[i + 1]))
            except ValueError:
                pass

    print("Тикеры:", ", ".join(tickers))
    print("Анализ: за сколько торговых дней от open сессии достигается уровень тейка (потолок % из конфига).")
    print("Сессий для анализа:", n_sessions)
    print()
    print(f"{'Ticker':<8} {'Тейк%':<8} {'Сессий':<8} {'Дней до тейка':<18} {'Медиана':<8} {'p70':<6} {'p80':<6} {'Предлаг.':<10} {'В конфиге':<12}")
    print("-" * 100)

    for ticker in tickers:
        take_pct = _take_profit_cap_pct(ticker)
        df = fetch_5m_ohlc(ticker, days=35)
        if df is None or df.empty:
            print(f"{ticker:<8} {take_pct:.1f}%    нет 5m данных")
            continue
        df = filter_to_last_n_us_sessions(df, n=n_sessions)
        if df is None or df.empty or "_session" not in df.columns:
            if "datetime" in df.columns:
                dt = df["datetime"]
                if hasattr(dt.dt, "date"):
                    df = df.copy()
                    df["_session"] = dt.dt.date
                else:
                    df["_session"] = dt.dt.normalize()
            else:
                print(f"{ticker:<8} {take_pct:.1f}%    нет сессий")
                continue
        sessions_order = sorted(df["_session"].unique())
        if len(sessions_order) < 2:
            print(f"{ticker:<8} {take_pct:.1f}%    {len(sessions_order)} сессия — мало для анализа")
            continue

        days_to_reach = []
        for i, sess in enumerate(sessions_order):
            grp = df[df["_session"] == sess].sort_values("datetime")
            if grp.empty:
                continue
            open_s = float(grp["Open"].iloc[0])
            if open_s <= 0:
                continue
            target = open_s * (1 + take_pct / 100.0)
            running_high = open_s
            for j in range(i, len(sessions_order)):
                grp_j = df[df["_session"] == sessions_order[j]]
                if grp_j.empty:
                    continue
                running_high = max(running_high, float(grp_j["High"].max()))
                if running_high >= target:
                    days_to_reach.append(j - i + 1)
                    break

        if not days_to_reach:
            print(f"{ticker:<8} {take_pct:.1f}%    {len(sessions_order):<8} ни разу не достигнут за окно")
            key = f"GAME_5M_MAX_POSITION_DAYS_{ticker.upper()}"
            cur = get_config_value(key, "").strip() or get_config_value("GAME_5M_MAX_POSITION_DAYS", "1")
            print(f"  → в конфиге: {cur}")
            continue

        arr = np.array(days_to_reach)
        median_d = float(np.median(arr))
        p70_d = float(np.percentile(arr, 70))
        p80_d = float(np.percentile(arr, 80))
        suggested = min(7, max(1, int(np.ceil(p80_d))))
        key = f"GAME_5M_MAX_POSITION_DAYS_{ticker.upper()}"
        current = get_config_value(key, "").strip()
        if not current:
            current = get_config_value("GAME_5M_MAX_POSITION_DAYS", "1").strip()
            current = f"(общий {current})"
        dist_str = ", ".join(f"{int(x)}д" for x in arr[:15])
        if len(arr) > 15:
            dist_str += "..."
        print(f"{ticker:<8} {take_pct:<8.1f} {len(sessions_order):<8} {dist_str:<18} {median_d:<8.1f} {p70_d:<6.1f} {p80_d:<6.1f} {suggested:<10} {current:<12}")

    print()
    print("Предлаг. = ceil(p80) торговых дней до достижения тейка (макс. 7).")
    print("В config.env: GAME_5M_MAX_POSITION_DAYS_<TICKER>=<число> (например GAME_5M_MAX_POSITION_DAYS_NBIS=3).")


if __name__ == "__main__":
    main()
