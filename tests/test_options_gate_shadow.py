# -*- coding: utf-8 -*-
from __future__ import annotations

from services.options_gate_shadow import (
    _classify_downgrade_outcome,
    _core_decision_from_context,
    extract_options_gate_from_context,
)


def test_extract_options_gate_from_context_sentiment():
    ctx = {
        "technical_decision_core": "BUY",
        "options_sentiment": {
            "status": "ok",
            "gate_hint": "would_downgrade",
            "sentiment_label": "BEARISH",
            "sentiment_score": -0.4,
            "pcr_volume": 1.2,
        },
    }
    g = extract_options_gate_from_context(ctx)
    assert g["has_context"] is True
    assert g["gate_hint"] == "would_downgrade"
    assert _core_decision_from_context(ctx) == "BUY"


def test_extract_options_gate_from_decision_snapshot():
    ctx = {
        "decision_snapshot": {
            "core_decision": "STRONG_BUY",
            "contributions": [
                {
                    "contour_id": "options_sentiment",
                    "metrics": {
                        "gate_hint": "would_signal",
                        "sentiment_label": "BULLISH",
                        "would_signal": True,
                    },
                }
            ],
        }
    }
    g = extract_options_gate_from_context(ctx)
    assert g["source"] == "decision_snapshot"
    assert g["gate_hint"] == "would_signal"


def test_classify_downgrade_outcome():
    assert _classify_downgrade_outcome(2.5, "would_downgrade") == "false_positive"
    assert _classify_downgrade_outcome(-1.0, "would_downgrade") == "true_positive"
    assert _classify_downgrade_outcome(1.0, "neutral") == "n/a"


def test_build_shadow_report_empty(monkeypatch):
    from services.options_gate_shadow import build_options_gate_shadow_report

    monkeypatch.setattr("services.trade_effectiveness_analyzer._load_closed_trades", lambda **kw: [])
    monkeypatch.setattr("services.trade_effectiveness_analyzer._prepare_ohlc_cache", lambda **kw: {})
    monkeypatch.setattr("services.trade_effectiveness_analyzer._estimate_trade_effects", lambda *a, **k: [])
    monkeypatch.setattr("services.ticker_groups.get_tickers_game_5m", lambda: ["MU"])

    def _fake_d5(t, **kw):
        return {
            "decision": "HOLD",
            "technical_decision_core": "HOLD",
            "options_sentiment": {"status": "ok", "gate_hint": "neutral"},
        }

    monkeypatch.setattr("services.recommend_5m.get_decision_5m", _fake_d5)

    r = build_options_gate_shadow_report(days=7, live_scan=True, limit_rows=5)
    assert r["schema_version"] == 1
    assert r["closed_trades"]["total_closed"] == 0
    assert r["live_scan"]["tickers_scanned"] == 1
    assert r["recommendation"]["ready_for_apply_discussion"] is False
