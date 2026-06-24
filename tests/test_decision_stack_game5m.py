# -*- coding: utf-8 -*-
"""Тесты decision_stack GAME_5M (фазы 1–3)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from services.decision_stack.game5m import (
    build_game5m_decision_snapshot,
    collect_game5m_contributions,
    finalize_game5m_decision_stack,
    resolve_game5m_technical,
    summarize_earnings_trust_impact,
)
from services.decision_stack.game5m_policy import apply_game5m_policy_gates
from services.premarket_gap_baseline import evaluate_premarket_gap_baseline


class TestDecisionStackGame5m(unittest.TestCase):
    def test_mirror_legacy_effective(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "HOLD",
            "catboost_fusion_mode": "hold_if_buy_below_p",
            "catboost_fusion_note": "P=0.3",
            "catboost_entry_proba_good": 0.3,
            "catboost_signal_status": "ok",
            "entry_advice": "ALLOW",
            "kb_news_impact": "нейтрально",
            "multiday_lr_entry_gate": {
                "mode": "log_only",
                "would_hold": True,
                "note": "bearish",
                "horizons_pct": {"1d": -0.5},
            },
        }
        env = {"DECISION_STACK_READINESS_CATBOOST_ENTRY_5M": "production"}
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            snap = build_game5m_decision_snapshot(d5, ticker="NVDA")
        self.assertEqual(snap["effective_decision"], "HOLD")
        self.assertEqual(snap["resolve_mode"], "mirror_legacy")
        self.assertEqual(snap["projected_effective_if_resolve"], "HOLD")
        ids = {c["contour_id"] for c in snap["contributions"]}
        self.assertIn("rules_5m", ids)
        self.assertIn("catboost_entry_5m", ids)

    def test_finalize_attaches_snapshot(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "entry_advice": "CAUTION",
            "entry_advice_reason": "вола",
            "kb_news_impact": "нейтрально",
        }
        with patch("services.decision_stack.game5m_policy.stack_own_finalize_enabled", return_value=False):
            finalize_game5m_decision_stack(d5, ticker="AAPL", kb_news=[])
        self.assertIn("decision_snapshot", d5)
        self.assertEqual(d5["decision_effective"], "BUY")
        self.assertEqual(d5["decision_stack_version"], 1)

    def test_resolve_entry_advice_avoid_apply(self):
        env = {
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "apply",
            "DECISION_STACK_MACRO_GATE_MODE": "none",
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "BUY",
            "entry_advice": "AVOID",
            "entry_advice_reason": "macro",
            "kb_news_impact": "нейтрально",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            contribs = collect_game5m_contributions(d5, ticker="X")
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "HOLD")
        adv = next(c for c in contribs if c["contour_id"] == "entry_advice")
        self.assertEqual(adv["action"], "veto")

    def test_news_fusion_downgrade_when_apply(self):
        env = {
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "apply",
            "DECISION_STACK_NEWS_FUSION_VETO_BELOW": "-0.35",
            "DECISION_STACK_READINESS_NEWS_FUSION": "production",
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "STRONG_BUY",
            "entry_fusion_metrics": {
                "fused_bias_neg1": -0.5,
                "tech_bias_neg1": 0.2,
                "news_bias_kb": -0.8,
            },
            "entry_advice": "ALLOW",
            "kb_news_impact": "негатив",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, str(d) if d is not None else "")):
            contribs = collect_game5m_contributions(d5)
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "HOLD")

    def test_projected_differs_from_legacy(self):
        d5 = {
            "decision": "BUY",
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "entry_advice": "AVOID",
            "entry_advice_reason": "test",
            "kb_news_impact": "нейтрально",
        }
        env = {"DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "apply"}
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            snap = build_game5m_decision_snapshot(d5, ticker="T")
        self.assertTrue(snap["resolve_divergence"])
        self.assertEqual(snap["projected_effective_if_resolve"], "HOLD")
        self.assertEqual(snap["effective_decision"], "BUY")

    def test_premarket_gap_baseline_downgrades_negative_gap(self):
        env = {
            "DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE": "apply",
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "none",
            "DECISION_STACK_MACRO_GATE_MODE": "none",
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "BUY",
            "premarket_gap_pct": -2.4,
            "entry_advice": "ALLOW",
            "kb_news_impact": "нейтрально",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            contribs = collect_game5m_contributions(d5, ticker="MU")
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "HOLD")
        pm = next(c for c in contribs if c["contour_id"] == "premarket_gap_baseline")
        self.assertEqual(pm["action"], "downgrade")
        self.assertEqual(pm["metrics"]["signal"], "bearish_gap")

    def test_premarket_gap_baseline_marks_bullish_gap_as_boost(self):
        sig = evaluate_premarket_gap_baseline(1.6, macro_equity_gap_bias="UP")
        self.assertIsNotNone(sig)
        self.assertEqual(sig["signal"], "bullish_gap")
        self.assertTrue(sig["should_boost_entry"])
        self.assertEqual(sig["action"], "boost")

    def test_strong_gap_up_is_take_watch_not_downgrade_without_fade_risk(self):
        env = {
            "DECISION_STACK_PREMARKET_GAP_BASELINE_GATE_MODE": "apply",
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "none",
            "DECISION_STACK_MACRO_GATE_MODE": "none",
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
        }
        d5 = {
            "technical_decision_core": "BUY",
            "premarket_gap_pct": 4.6,
            "entry_advice": "ALLOW",
            "kb_news_impact": "нейтрально",
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            contribs = collect_game5m_contributions(d5, ticker="MU")
            eff = resolve_game5m_technical(d5, contribs)
        self.assertEqual(eff, "BUY")
        pm = next(c for c in contribs if c["contour_id"] == "premarket_gap_baseline")
        self.assertEqual(pm["action"], "telemetry")
        self.assertEqual(pm["metrics"]["signal"], "strong_gap_up")
        self.assertTrue(pm["metrics"]["should_take_watch"])

    def test_strong_gap_up_with_fade_risk_downgrades(self):
        sig = evaluate_premarket_gap_baseline(4.6, macro_equity_gap_bias="DOWN")
        self.assertIsNotNone(sig)
        self.assertEqual(sig["signal"], "strong_gap_up_fade_risk")
        self.assertEqual(sig["action"], "downgrade")
        self.assertTrue(sig["should_take_watch"])

    def test_earnings_trust_contribution_shadow(self):
        from unittest.mock import patch

        sample = {
            "active": True,
            "runtime_role": "source",
            "strength": -0.4,
            "would_downgrade": True,
            "detail_ru": "ORCL test",
            "event_date": "2026-06-10",
            "source_symbol": "ORCL",
            "trust_labels": {},
        }
        d5 = {"technical_decision_core": "BUY", "ticker": "ORCL"}
        with patch(
            "services.earnings_trust_runtime.build_earnings_trust_runtime",
            return_value=sample,
        ):
            contribs = collect_game5m_contributions(d5, ticker="ORCL")
        et = next(c for c in contribs if c["contour_id"] == "earnings_trust")
        self.assertEqual(et["action"], "telemetry")
        self.assertEqual(et["role"], "advisory_postmortem")
        self.assertLess(float(et["strength"]), 0)

    def test_earnings_trust_resolve_downgrade_apply(self):
        sample = {
            "active": True,
            "runtime_role": "source",
            "strength": -0.4,
            "would_downgrade": True,
            "detail_ru": "ORCL test",
            "event_date": "2026-06-10",
            "source_symbol": "ORCL",
            "trust_labels": {},
        }
        d5 = {
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "ticker": "ORCL",
            "entry_advice": "ALLOW",
            "kb_news_impact": "нейтрально",
        }
        env = {
            "DECISION_STACK_EARNINGS_TRUST_GATE_MODE": "apply",
            "DECISION_STACK_ENTRY_ADVICE_GATE_MODE": "none",
            "DECISION_STACK_MACRO_GATE_MODE": "none",
            "DECISION_STACK_CATBOOST_GATE_MODE": "none",
            "DECISION_STACK_MULTIDAY_GATE_MODE": "none",
            "DECISION_STACK_NEWS_FUSION_GATE_MODE": "none",
        }
        with patch(
            "services.earnings_trust_runtime.build_earnings_trust_runtime",
            return_value=sample,
        ), patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            snap = build_game5m_decision_snapshot(d5, ticker="ORCL")
        self.assertEqual(snap["projected_effective_if_resolve"], "HOLD")
        self.assertEqual(snap["effective_decision"], "BUY")
        et = snap["earnings_trust_impact"]
        self.assertTrue(et.get("active"))
        self.assertTrue(et.get("shadow_would_hold_if_core_bull"))
        self.assertTrue(et.get("changed_projected_resolve"))
        et_c = next(c for c in snap["contributions"] if c["contour_id"] == "earnings_trust")
        self.assertEqual(et_c["action"], "downgrade")

    def test_summarize_earnings_trust_impact_inactive(self):
        out = summarize_earnings_trust_impact([], core="BUY", legacy_eff="BUY", projected="BUY")
        self.assertFalse(out.get("active"))

    def test_options_sentiment_contribution_log_only(self):
        env = {"DECISION_STACK_OPTIONS_SENTIMENT_GATE_MODE": "log_only"}
        d5 = {
            "technical_decision_core": "BUY",
            "technical_decision_effective": "BUY",
            "kb_news_impact": "нейтрально",
            "options_sentiment": {
                "status": "ok",
                "sentiment_label": "BEARISH",
                "sentiment_score": -0.5,
                "pcr_volume": 1.2,
                "gate_hint": "would_downgrade",
                "data_as_of": "live",
            },
        }
        with patch("config_loader.get_config_value", side_effect=lambda k, d=None: env.get(k, d)):
            contribs = collect_game5m_contributions(d5, ticker="MU")
        opt = next(c for c in contribs if c["contour_id"] == "options_sentiment")
        self.assertEqual(opt["action"], "telemetry")
        self.assertTrue(opt["metrics"]["would_downgrade"])
        self.assertEqual(opt["metrics"]["gate_mode"], "log_only")
        snap = build_game5m_decision_snapshot(d5, ticker="MU")
        self.assertEqual(snap["effective_decision"], "BUY")


if __name__ == "__main__":
    unittest.main()
