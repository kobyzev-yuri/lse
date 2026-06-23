# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import patch

from services.multiday_lr_gate import evaluate_multiday_entry_gate
from services.portfolio_entry_guards import portfolio_indicator_blocks_buy


def test_evaluate_multiday_entry_gate_portfolio_env_keys():
    d5 = {
        "multiday_lr_horizon_1d_pct_vs_spot": -0.5,
        "multiday_lr_horizon_2d_pct_vs_spot": -0.2,
        "multiday_lr_horizon_3d_pct_vs_spot": -0.2,
    }

    def _cfg(key, default=""):
        vals = {
            "PORTFOLIO_MULTIDAY_ENTRY_GATE_MODE": "apply",
            "PORTFOLIO_MULTIDAY_ENTRY_TAU_1D_PCT": "0.25",
            "PORTFOLIO_MULTIDAY_ENTRY_TAU_PCT": "0.15",
            "PORTFOLIO_MULTIDAY_ENTRY_NEGATIVE_HORIZONS_MIN": "2",
        }
        return vals.get(key, default)

    with patch("config_loader.get_config_value", side_effect=_cfg):
        gate = evaluate_multiday_entry_gate(d5, mode_env_key="PORTFOLIO_MULTIDAY_ENTRY_GATE_MODE")
    assert gate["would_hold"] is True
    assert gate["mode"] == "apply"


def test_portfolio_indicator_blocks_commodities():
    with patch("services.ticker_groups.get_config_value", return_value="^VIX,GC=F,CL=F,BZ=F"):
        blocked, reason = portfolio_indicator_blocks_buy("GC=F")
    assert blocked is True
    assert "INDICATOR" in reason.upper() or "TICKERS" in reason
