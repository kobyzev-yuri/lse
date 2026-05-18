# -*- coding: utf-8 -*-
"""Тесты строк премаркет-Telegram: сектор + GAME_5m прогноз/факт."""
from __future__ import annotations

from unittest.mock import patch

from services.macro_premarket_risk import (
    format_sector_and_game5m_gap_lines,
    _format_game5m_ticker_gap_forecast_line,
)


def test_format_sector_and_game5m_gap_lines():
    macro = {
        "enabled": True,
        "macro_predicted_sector_gap_pct": -0.144,
        "macro_sector_proxy": "SMH",
        "game_5m_gaps": [
            {
                "ticker": "SNDK",
                "gap_pct": 5.2,
                "premarket_last": 120.5,
                "source": "premarket",
            },
        ],
    }
    with patch(
        "services.ticker_open_gap_predict.predict_ticker_open_gap_pct",
        return_value=(0.366, "ticker_ols"),
    ):
        lines = format_sector_and_game5m_gap_lines(macro)
    assert any("Сектор SMH" in ln and "-0.14" in ln for ln in lines)
    assert any("SNDK" in ln and "прогноз +0.37%" in ln for ln in lines)
    assert any("премаркет +5.20%" in ln for ln in lines)


def test_game5m_line_sector_proxy_fallback():
    macro = {
        "enabled": True,
        "macro_sector_proxy": "SMH",
        "macro_predicted_sector_gap_pct": -0.1,
    }
    det = {"ticker": "NVDA", "gap_pct": 1.1, "premarket_last": 900.0}
    with patch(
        "services.ticker_open_gap_predict.predict_ticker_open_gap_pct",
        return_value=(-0.144, "sector_proxy"),
    ):
        line = _format_game5m_ticker_gap_forecast_line(det, macro)
    assert line is not None
    assert "NVDA" in line
    assert "прогноз -0.14%" in line
    assert "прокси SMH" in line
    assert "премаркет +1.10%" in line
