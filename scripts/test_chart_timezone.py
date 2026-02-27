#!/usr/bin/env python3
"""
Проверка выравнивания маркеров сделок на графике 5m (таймзоны).

- Сделки в БД хранятся в Moscow (ts_timezone).
- Ось графика и маркеры должны быть в ET (America/New_York).
- Тест: Moscow 22:20 -> ET 14:20 (в феврале EST, UTC-5; Moscow UTC+3).
"""
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def test_trade_ts_to_et():
    """22:20 Moscow = 14:20 ET (зима, без DST)."""
    from services.game_5m import trade_ts_to_et, TRADE_HISTORY_TZ

    # 26.02.2026 22:20 Moscow -> 26.02.2026 14:20 ET
    ts_moscow = datetime(2026, 2, 26, 22, 20, 0)
    et = trade_ts_to_et(ts_moscow, source_tz=TRADE_HISTORY_TZ)
    assert et is not None
    # naive ET для графика
    et_naive = et.tz_convert("America/New_York").replace(tzinfo=None) if hasattr(et, "tz_convert") else et
    if hasattr(et_naive, "hour"):
        hour, minute = et_naive.hour, et_naive.minute
    else:
        hour, minute = et.hour, et.minute  # pyright: ignore
    assert hour == 14 and minute == 20, f"Ожидали 14:20 ET, получили {hour}:{minute}"


def test_same_scale_as_candle():
    """Маркер в ET и свеча в ET должны иметь одинаковый date2num (совпадение на оси)."""
    import pandas as pd
    import matplotlib.dates as mdates
    from services.game_5m import trade_ts_to_et, TRADE_HISTORY_TZ

    # Свеча в 14:20 ET (naive)
    candle_et = pd.Timestamp("2026-02-26 14:20:00")
    # Сделка в 22:20 Moscow = 14:20 ET
    ts_moscow = datetime(2026, 2, 26, 22, 20, 0)
    et_ts = trade_ts_to_et(ts_moscow, source_tz=TRADE_HISTORY_TZ)
    marker_et = et_ts.to_pydatetime().replace(tzinfo=None) if et_ts is not None else None
    assert marker_et is not None
    n_candle = mdates.date2num(candle_et)
    n_marker = mdates.date2num(marker_et)
    diff = abs(n_candle - n_marker)
    assert diff < 0.0001, f"Маркер и свеча должны совпадать по оси X, diff={diff}"


if __name__ == "__main__":
    test_trade_ts_to_et()
    test_same_scale_as_candle()
    print("OK: конвертация Moscow->ET и выравнивание с осью графика проверены.")
