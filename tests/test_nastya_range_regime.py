"""Tests for nastya range-regime helpers (no network)."""

import unittest
from unittest.mock import patch

import pandas as pd

from services.nastya_range_regime import (
    REPORT_SCHEMA_VERSION,
    _blend_band,
    _detect_local,
    _excel_anchors,
    build_nastya_llm_prompts,
    build_nastya_llm_user_content,
    render_nastya_range_chart_png,
    split_nastya_llm_explanation,
)


class TestNastyaRangeRegime(unittest.TestCase):
    def test_detect_local_transition(self):
        idx = pd.date_range("2026-01-01", periods=80, freq="B")
        close = pd.Series(range(80), index=idx, dtype=float) + 100
        high = close + 1
        low = close - 1
        out = _detect_local(close, high, low)
        self.assertIn(out["regime"], {"uptrend", "transition", "range", "downtrend"})
        self.assertIsNotNone(out["channel_lo_20d"])

    def test_blend_bias_near_floor(self):
        local = {"regime": "transition", "channel_lo_20d": 90.0, "channel_hi_20d": 120.0}
        xa = {"drop20_from_52w": 95.0, "target_17pct": 130.0}
        band = _blend_band(96.0, local, xa)
        self.assertEqual(band["bias_exit"], "up")

    def test_excel_missing(self):
        ex = pd.DataFrame()
        self.assertFalse(_excel_anchors(ex, "ARM").get("in_excel"))

    def test_schema_version_bumped_for_trend(self):
        self.assertGreaterEqual(REPORT_SCHEMA_VERSION, 3)

    def test_llm_prompts_cover_nastya_goals(self):
        row = {
            "ticker": "META",
            "status": "ok",
            "regime": "transition",
            "bias_exit": "up",
            "band_floor": 637,
            "band_ceiling": 686,
            "pos_in_band": 0.18,
            "rvol_20": 1.06,
            "rvol_flag": "normal",
            "approx_range_age_days": 75,
            "portfolio_trend_regime": "neutral",
            "portfolio_trend_ret_20d_pct": -2.1,
        }
        system, user = build_nastya_llm_prompts(
            row,
            market={"vix_regime": "calm"},
            portfolio_slim={"in_portfolio_game": True, "decision": "HOLD"},
        )
        self.assertIn("Пол и потолок", system)
        self.assertIn("Боковик", system)
        self.assertIn("RVOL", system)
        self.assertIn("ML portfolio", system)
        self.assertIn("графика", system)
        self.assertIn("Итог", system)
        self.assertIn("META", user)
        self.assertIn("neutral", user)

    def test_split_итог_and_details(self):
        text = (
            "Итог: Скорее отскок от пола и продолжение узкой полки, не свободный рост.\n\n"
            "1) Пол и потолок — у нижней границы.\n"
            "2) Боковик — transition, Age≈75d."
        )
        parts = split_nastya_llm_explanation(text)
        self.assertIn("отскок от пола", parts["summary_ru"])
        self.assertIn("1) Пол", parts["details_ru"])
        self.assertTrue(parts["explanation_ru"].startswith("Итог:"))

    def test_user_content_with_chart_is_multimodal(self):
        content = build_nastya_llm_user_content("hello", chart_png=b"\x89PNG\r\n")
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_render_chart_png_from_synthetic(self):
        idx = pd.date_range("2026-01-01", periods=90, freq="B")
        close = pd.Series(range(90), index=idx, dtype=float) + 100
        df = pd.DataFrame(
            {
                "Open": close,
                "High": close + 1,
                "Low": close - 1,
                "Close": close,
                "Volume": 1e6,
            },
            index=idx,
        )
        with patch("services.nastya_range_regime._load_ohlcv_for_ticker", return_value=df):
            png = render_nastya_range_chart_png(
                "META",
                row={
                    "band_floor": 110.0,
                    "band_ceiling": 170.0,
                    "bias_exit": "up",
                    "portfolio_trend_regime": "melt_up",
                },
            )
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
