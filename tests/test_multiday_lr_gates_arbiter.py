# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.analyzer_ml_arbiter import build_multiday_lr_gates_arbiter


class TestMultidayLrGatesArbiter(unittest.TestCase):
    def test_insufficient_when_no_telemetry(self):
        env = {
            "GAME_5M_MULTIDAY_LR_REG_ENABLED": "true",
            "GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "log_only",
            "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "log_only",
        }
        with patch.dict(os.environ, env, clear=False):
            out = build_multiday_lr_gates_arbiter(
                {"multiday_lr_reality_check": {"mode": "ok", "walkforward_production_verdict": "caution"}},
                strategy="GAME_5M",
                closed_trades=[],
                effects=[],
            )
        self.assertEqual(out["mode"], "ok")
        self.assertEqual(out["entry_gate"]["verdict"], "insufficient_data")

    def test_ready_entry_when_would_hold_worse(self):
        env = {
            "GAME_5M_MULTIDAY_LR_REG_ENABLED": "true",
            "GAME_5M_MULTIDAY_ENTRY_GATE_MODE": "log_only",
            "GAME_5M_MULTIDAY_HOLD_GATE_MODE": "none",
        }
        trades = []
        effects = []
        for i, (wh, rp) in enumerate([(True, -2.0), (True, -1.5), (True, -0.5), (False, 1.0), (False, 2.0), (False, 1.5)] * 3):
            tid = i + 1
            trades.append(
                SimpleNamespace(
                    trade_id=tid,
                    ticker="AAPL",
                    context_json={
                        "multiday_lr_entry_gate_status": "ok",
                        "multiday_lr_entry_gate_would_hold": wh,
                        "multiday_lr_horizon_1d_pct_vs_spot": -0.5 if wh else 0.3,
                    },
                    exit_context_json={},
                )
            )
            effects.append(
                SimpleNamespace(
                    trade_id=tid,
                    ticker="AAPL",
                    realized_pct=rp,
                    exit_signal="TAKE_PROFIT",
                )
            )
        with patch.dict(os.environ, env, clear=False):
            out = build_multiday_lr_gates_arbiter(
                {"multiday_lr_reality_check": {"mode": "ok", "walkforward_production_verdict": "ready"}},
                strategy="GAME_5M",
                closed_trades=trades,
                effects=effects,
            )
        self.assertEqual(out["entry_gate"]["verdict"], "ready_for_entry_apply")


if __name__ == "__main__":
    unittest.main()
