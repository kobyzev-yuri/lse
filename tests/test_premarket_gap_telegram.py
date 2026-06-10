# -*- coding: utf-8 -*-
"""Тесты строк премаркет-Telegram: сектор + GAME_5m прогноз/факт."""
from __future__ import annotations

from unittest.mock import patch

from services.macro_premarket_risk import (
    _format_game5m_ticker_gap_forecast_line,
    format_sector_and_game5m_gap_lines,
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
    tg_line = "• SNDK 120.50: PM +5.20%, base→open +5.20%, ML +0.37%, eff=base +5.20%"
    with patch(
        "services.premarket_open_gap_forecast.format_open_gap_forecast_telegram_line",
        return_value=tg_line,
    ):
        lines = format_sector_and_game5m_gap_lines(macro)
    assert any("Сектор SMH" in ln and "-0.14" in ln for ln in lines)
    assert any("SNDK" in ln and "base→open" in ln for ln in lines)
    assert any("прогноз open" in ln for ln in lines)


def test_game5m_line_uses_open_gap_formatter():
    macro = {
        "enabled": True,
        "macro_sector_proxy": "SMH",
        "macro_predicted_sector_gap_pct": -0.1,
    }
    det = {"ticker": "NVDA", "gap_pct": 1.1, "premarket_last": 900.0}
    with patch(
        "services.premarket_open_gap_forecast.format_open_gap_forecast_telegram_line",
        return_value="• NVDA 900.00: PM +1.10%, base→open +1.10%, ML -0.14%, eff=base +1.10%",
    ) as fmt:
        line = _format_game5m_ticker_gap_forecast_line(det, macro)
        fmt.assert_called_once()
    assert line is not None
    assert "NVDA" in line
    assert "base→open" in line
