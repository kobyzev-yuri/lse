# -*- coding: utf-8 -*-
"""Unified GAME_5M forecast envelope.

This is a compatibility layer over existing gap and multiday fields. It does
not make trading decisions; policy gates consume the normalized envelope later.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sign(v: Optional[float], tau: float = 0.10) -> int:
    if v is None:
        return 0
    if v > tau:
        return 1
    if v < -tau:
        return -1
    return 0


def build_game5m_forecast_envelope(d5: Dict[str, Any]) -> Dict[str, Any]:
    gap = _f(d5.get("ticker_open_gap_predicted_pct"))
    gap_ml = _f(d5.get("ticker_open_gap_ml_advisory_pct"))
    gap_fact = _f(d5.get("ticker_open_gap_fact_pct"))
    pm_gap = _f(d5.get("premarket_gap_pct"))
    effective_gap = pm_gap if pm_gap is not None else gap
    sector_gap = _f(d5.get("macro_predicted_sector_gap_pct"))
    h1 = _f(d5.get("multiday_lr_horizon_1d_pct_vs_spot"))
    h2 = _f(d5.get("multiday_lr_horizon_2d_pct_vs_spot"))
    h3 = _f(d5.get("multiday_lr_horizon_3d_pct_vs_spot"))
    confidence = _f(d5.get("ticker_open_gap_confidence"))
    uncertainty = _f(d5.get("ticker_open_gap_uncertainty_p80_pp"))

    gap_s = _sign(effective_gap)
    md_vals = [x for x in (h1, h2, h3) if x is not None]
    md_avg = sum(md_vals) / len(md_vals) if md_vals else None
    md_s = _sign(md_avg)
    if gap_s and md_s and gap_s == md_s:
        regime = "aligned_bullish" if gap_s > 0 else "aligned_bearish"
    elif gap_s and md_s and gap_s != md_s:
        regime = "gap_fade_risk" if gap_s > 0 else "gap_reversal_opportunity"
    elif gap_s:
        regime = "gap_driven"
    elif md_s:
        regime = "multiday_driven"
    else:
        regime = "neutral_or_unavailable"

    gap_up_source = None
    gap_up_value = None
    for label, value in (
        ("premarket_gap", pm_gap),
        ("predicted_open_gap", gap),
        ("actual_open_gap", gap_fact),
    ):
        if value is not None and value >= 1.0 and (gap_up_value is None or value > gap_up_value):
            gap_up_source = label
            gap_up_value = value
    macro_bias = str(d5.get("macro_equity_gap_bias") or "").upper()
    sector_confirmed = bool((sector_gap is not None and sector_gap >= 0.3) or macro_bias == "UP")
    multiday_confirmed = md_avg is not None and md_avg >= 0.15
    fade_risk = bool(regime == "gap_fade_risk" or (md_avg is not None and md_avg <= -0.15))
    candidate = gap_up_value is not None
    should_boost = bool(candidate and not fade_risk and (sector_confirmed or multiday_confirmed))
    if should_boost:
        opportunity_reason = "gap_up_confirmed"
    elif candidate and fade_risk:
        opportunity_reason = "gap_up_fade_risk"
    elif candidate:
        opportunity_reason = "gap_up_unconfirmed"
    else:
        opportunity_reason = "no_gap_up"

    ready = bool(gap is not None or pm_gap is not None or md_vals)
    envelope = {
        "version": 1,
        "open_gap": {
            "predicted_pct": effective_gap,
            "effective_pct": effective_gap,
            "model_advisory_pct": gap_ml if gap_ml is not None else gap,
            "fact_pct": gap_fact,
            "premarket_gap_pct": pm_gap,
            "observable_baseline_pct": pm_gap,
            "sector_predicted_pct": sector_gap,
            "source": d5.get("ticker_open_gap_predicted_source"),
            "model_advisory_source": d5.get("ticker_open_gap_ml_advisory_source"),
            "model_version": d5.get("ticker_open_gap_model_version"),
            "confidence": confidence,
            "uncertainty_p80_pp": uncertainty,
        },
        "horizons_pct": {
            "1d": h1,
            "2d": h2,
            "3d": h3,
        },
        "multiday": {
            "bias": d5.get("multiday_lr_bias"),
            "method": d5.get("multiday_lr_method"),
            "daily_close_source": d5.get("multiday_lr_daily_close_source"),
        },
        "gap_up_opportunity": {
            "candidate": candidate,
            "source": gap_up_source,
            "gap_pct": gap_up_value,
            "sector_confirmed": sector_confirmed,
            "multiday_confirmed": multiday_confirmed,
            "fade_risk": fade_risk,
            "should_boost_entry": should_boost,
            "reason": opportunity_reason,
        },
        "regime": regime,
        "ready": ready,
    }
    return envelope


def attach_game5m_forecast_layer(d5: Dict[str, Any]) -> None:
    envelope = build_game5m_forecast_envelope(d5)
    d5["forecast_layer"] = envelope
    og = envelope.get("open_gap") or {}
    d5["forecast_open_gap_pct"] = og.get("predicted_pct")
    d5["forecast_open_gap_fact_pct"] = og.get("fact_pct")
    d5["forecast_open_gap_confidence"] = og.get("confidence")
    d5["forecast_open_gap_source"] = og.get("source")
    d5["forecast_open_gap_uncertainty_p80_pp"] = og.get("uncertainty_p80_pp")
    d5["forecast_horizons_pct"] = envelope.get("horizons_pct")
    d5["forecast_regime"] = envelope.get("regime")
    d5["forecast_ready"] = bool(envelope.get("ready"))
    opp = envelope.get("gap_up_opportunity") if isinstance(envelope.get("gap_up_opportunity"), dict) else {}
    d5["forecast_gap_up_opportunity"] = bool(opp.get("candidate"))
    d5["forecast_gap_up_should_boost_entry"] = bool(opp.get("should_boost_entry"))
    d5["forecast_gap_up_opportunity_reason"] = opp.get("reason")
    d5["forecast_gap_up_opportunity_source"] = opp.get("source")
