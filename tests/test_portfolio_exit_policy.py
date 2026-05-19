"""Portfolio exit policy: ML take clamp and trailing."""

import unittest

from services.portfolio_exit_policy import (
    clamp_take_pct,
    compute_ml_adjusted_take,
    trailing_take_should_close,
)


class TestPortfolioExitPolicy(unittest.TestCase):
    def test_clamp_take(self):
        self.assertEqual(clamp_take_pct(20.0, floor_pct=4.0, cap_pct=18.0), 18.0)
        self.assertEqual(clamp_take_pct(2.0, floor_pct=4.0, cap_pct=18.0), 4.0)

    def test_ml_adjusted_take(self):
        eff, note = compute_ml_adjusted_take(10.0, 1.0)
        self.assertGreaterEqual(eff, 10.0)
        self.assertIn("ml_adj", note)

    def test_trailing_take(self):
        from unittest.mock import patch

        with patch(
            "services.portfolio_exit_policy.trailing_take_params",
            return_value=(True, 8.0, 3.0),
        ):
            ok, _ = trailing_take_should_close(12.0, 16.0)
            self.assertTrue(ok)
            ok2, _ = trailing_take_should_close(14.0, 16.0)
            self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
