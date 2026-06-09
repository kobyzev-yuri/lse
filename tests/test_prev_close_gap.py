# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from services.recommend_5m import _compute_rth_open_gap_pct


class TestPrevCloseGap(unittest.TestCase):
    def test_rth_open_gap_uses_prev_before_trade_date(self):
        import pandas as pd

        # 2026-06-09 RTH open 908.7 vs prev 911.5 => -0.31%
        rows = [
            {"datetime": "2026-06-09 09:30:00-04:00", "Open": 908.7, "Close": 910.0},
            {"datetime": "2026-06-09 09:35:00-04:00", "Open": 910.0, "Close": 912.0},
        ]
        df = pd.DataFrame(rows)
        with patch("services.recommend_5m._prev_close_before_trade_date", return_value=911.5):
            gap, op, prev = _compute_rth_open_gap_pct(df, "LITE")
        self.assertEqual(prev, 911.5)
        self.assertEqual(op, 908.7)
        self.assertEqual(gap, -0.31)

    @patch("sqlalchemy.create_engine")
    def test_get_prev_close_from_db_excludes_today(self, mock_engine):
        from services.premarket import get_prev_close_from_db

        conn = MagicMock()
        mock_engine.return_value.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = (911.5,)

        with patch("services.premarket._et_now") as mock_et:
            mock_et.return_value = MagicMock(date=lambda: date(2026, 6, 9))
            px = get_prev_close_from_db("LITE")

        self.assertEqual(px, 911.5)
        sql = str(conn.execute.call_args[0][0])
        self.assertIn("date <", sql)


if __name__ == "__main__":
    unittest.main()
