# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from services.premarket import _true_premarket_slice
from services.premarket_chart import _filter_premarket_bars


class TestPremarketTrueSlice(unittest.TestCase):
    def test_context_slice_excludes_rth_rows(self):
        df = pd.DataFrame(
            {
                "Datetime": [
                    pd.Timestamp("2026-05-22 08:30", tz="America/New_York"),
                    pd.Timestamp("2026-05-22 09:29", tz="America/New_York"),
                    pd.Timestamp("2026-05-22 11:07", tz="America/New_York"),
                ],
                "Close": [100.0, 101.0, 110.0],
            }
        )
        et_now = datetime(2026, 5, 22, 11, 10, tzinfo=ZoneInfo("America/New_York"))
        out = _true_premarket_slice(df, "Datetime", et_now=et_now)
        self.assertEqual(len(out), 2)
        self.assertEqual(float(out["Close"].iloc[-1]), 101.0)

    def test_chart_filter_never_falls_back_to_rth(self):
        df = pd.DataFrame(
            {
                "Datetime": [
                    pd.Timestamp("2026-05-22 10:00", tz="America/New_York"),
                    pd.Timestamp("2026-05-22 11:07", tz="America/New_York"),
                ],
                "Close": [100.0, 101.0],
            }
        )
        out = _filter_premarket_bars(df, "Datetime")
        self.assertTrue(out.empty)


if __name__ == "__main__":
    unittest.main()
