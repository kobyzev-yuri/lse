import pandas as pd
import pytest

from services.premarket_chart import _filter_premarket_bars


def test_filter_premarket_bars_before_open():
    df = pd.DataFrame({
        "Datetime": [
            "2026-05-19 08:00:00-04:00",
            "2026-05-19 09:00:00-04:00",
            "2026-05-19 10:00:00-04:00",
        ],
        "Close": [100.0, 101.0, 102.0],
    })
    out = _filter_premarket_bars(df, "Datetime")
    assert len(out) == 2
    assert float(out["Close"].iloc[-1]) == 101.0
