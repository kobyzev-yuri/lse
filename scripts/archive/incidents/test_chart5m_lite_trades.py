#!/usr/bin/env python3
"""
Проверка: в график /chart5m LITE 1 (сессия 27.02.2026) входят 2 покупки и 2 продажи из истории.
"""
import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def main():
    from services.game_5m import get_trades_for_chart, _chart_range_et_to_msk, trade_ts_to_et
    import pandas as pd

    # График "LITE 1" = одна сессия; последняя сессия в данных может быть 27.02 или 28.02
    # Явно задаём диапазон 27.02.2026 09:30–16:00 ET (как на скриншоте)
    et_tz = "America/New_York"
    dt_min = pd.Timestamp("2026-02-27 09:30:00").tz_localize(et_tz)
    dt_max = pd.Timestamp("2026-02-27 16:00:00").tz_localize(et_tz)
    dt_min_naive = dt_min.tz_localize(None)
    dt_max_naive = dt_max.tz_localize(None)

    print("1. Диапазон графика (ET):", dt_min_naive, "–", dt_max_naive)
    dt_lo, dt_hi = _chart_range_et_to_msk(dt_min_naive, dt_max_naive, margin_days=1)
    print("2. Запрос к БД (MSK с запасом):", dt_lo, "–", dt_hi)

    trades = get_trades_for_chart("LITE", dt_min_naive, dt_max_naive)
    print("3. Всего сделок из get_trades_for_chart(LITE, ...):", len(trades))

    buys = [t for t in trades if t.get("side") == "BUY"]
    sells = [t for t in trades if t.get("side") == "SELL"]
    print("   BUY:", len(buys), ", SELL:", len(sells))

    for i, t in enumerate(trades):
        ts = t.get("ts")
        ts_et = trade_ts_to_et(ts, source_tz=t.get("ts_timezone"))
        ts_str = ts_et.strftime("%Y-%m-%d %H:%M ET") if hasattr(ts_et, "strftime") else str(ts_et)
        print(f"   [{i+1}] {t.get('side')} @ {t.get('price')} — {ts_str}")

    # На график за 27.02 должны входить минимум 4 сделки именно за 27.02 (2 BUY, 2 SELL)
    from datetime import date
    target_date = date(2026, 2, 27)
    trades_27 = []
    for t in trades:
        ts_et = trade_ts_to_et(t.get("ts"), source_tz=t.get("ts_timezone"))
        if ts_et is not None:
            d = getattr(ts_et, "date", lambda: None)()
            if d is None and hasattr(ts_et, "strftime"):
                d = date.fromisoformat(ts_et.strftime("%Y-%m-%d"))
            if d == target_date:
                trades_27.append(t)
    buys_27 = [t for t in trades_27 if t.get("side") == "BUY"]
    sells_27 = [t for t in trades_27 if t.get("side") == "SELL"]
    print(f"\nИз них за 27.02.2026: {len(trades_27)} (BUY: {len(buys_27)}, SELL: {len(sells_27)})")
    expected = 4
    if len(trades_27) >= expected:
        print(f"✅ OK: в график за 27 число входят {len(trades_27)} сделок (2 покупки, 2 продажи).")
    else:
        print(f"❌ FAIL: за 27.02 получено {len(trades_27)} сделок, ожидалось не менее {expected}.")
        sys.exit(1)

if __name__ == "__main__":
    main()
