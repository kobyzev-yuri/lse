"""Human-readable ML shadow advice for GAME_5M web cards (entry E3 / hold H3)."""
from __future__ import annotations

from typing import Any, Dict, Optional


def _entry_e3_tau() -> float:
    from config_loader import get_config_value

    try:
        return max(0.0, min(1.0, float((get_config_value("GAME_5M_ENTRY_E3_TAU_ENTER", "0.5") or "0.5").strip())))
    except (TypeError, ValueError):
        return 0.5


def _hold_h3_tau() -> float:
    from config_loader import get_config_value

    try:
        return max(0.0, min(1.0, float((get_config_value("GAME_5M_HOLD_QUALITY_TAU_HOLD", "0.55") or "0.55").strip())))
    except (TypeError, ValueError):
        return 0.55


def entry_e3_advice_from_d5(d5: Dict[str, Any]) -> Dict[str, Any]:
    """Shadow entry advice from d5 fields (no extra inference)."""
    status = str(d5.get("entry_e3_signal_status") or "skipped")
    raw_p = d5.get("catboost_entry_proba_good_e3")
    tau = _entry_e3_tau()
    out: Dict[str, Any] = {
        "contour": "entry_e3",
        "log_only": True,
        "status": status,
        "proba": None,
        "tau": tau,
        "would_enter": None,
        "label_ru": "нет данных",
        "detail_ru": (d5.get("entry_e3_signal_note") or "").strip() or None,
    }
    if status != "ok" or raw_p is None:
        return out
    try:
        p = float(raw_p)
    except (TypeError, ValueError):
        return out
    would = p >= tau
    out["proba"] = round(p, 4)
    out["would_enter"] = would
    out["label_ru"] = "за вход" if would else "против входа"
    out["detail_ru"] = f"P(y_entry_good)≈{p:.2f} · порог τ={tau:.2f} · shadow"
    return out


def hold_h3_advice_from_shadow(hq: Dict[str, Any]) -> Dict[str, Any]:
    """Map hold_quality_ml telemetry to card labels."""
    status = str(hq.get("status") or "skipped")
    raw_p = hq.get("hold_quality_proba")
    tau = float(hq.get("tau_hold") if hq.get("tau_hold") is not None else _hold_h3_tau())
    out: Dict[str, Any] = {
        "contour": "hold_h3",
        "log_only": bool(hq.get("log_only", True)),
        "status": status,
        "proba": None,
        "tau": tau,
        "would_defer_exit": hq.get("would_defer_exit"),
        "label_ru": "нет данных",
        "detail_ru": (hq.get("reason") or hq.get("skip_reason") or "").strip() or None,
    }
    if status != "ok" or raw_p is None:
        return out
    try:
        p = float(raw_p)
    except (TypeError, ValueError):
        return out
    would_defer = bool(hq.get("would_defer_exit")) if hq.get("would_defer_exit") is not None else p >= tau
    out["proba"] = round(p, 4)
    out["would_defer_exit"] = would_defer
    # would_defer_exit=True → model prefers holding (not selling now).
    out["label_ru"] = "за удержание" if would_defer else "за выход"
    out["detail_ru"] = f"P(y_hold_good)≈{p:.2f} · τ={tau:.2f} · shadow"
    return out


def enrich_game5m_card_ml_advice(
    card: Dict[str, Any],
    *,
    d5: Optional[Dict[str, Any]] = None,
    open_position: Optional[Dict[str, Any]] = None,
    entry_ctx: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach ml_entry_e3 / ml_hold_h3 blocks for game5m_cards.html."""
    out = dict(card or {})
    src = d5 if isinstance(d5, dict) else out
    out["ml_entry_e3"] = entry_e3_advice_from_d5(src)

    if not open_position or not isinstance(d5, dict):
        return out

    price = d5.get("price")
    try:
        ref_close = float(price) if price is not None and float(price) > 0 else 0.0
    except (TypeError, ValueError):
        ref_close = 0.0
    if ref_close <= 0:
        return out

    try:
        from services.game5m_hold_quality_signal import build_hold_quality_shadow

        ms = d5.get("market_session") if isinstance(d5.get("market_session"), dict) else {}
        bar_et = (
            d5.get("decision_5m_bar_open_et")
            or ms.get("decision_5m_bar_open_et")
            or ms.get("bar_open_et")
        )
        hq = build_hold_quality_shadow(
            ticker=str(out.get("ticker") or open_position.get("ticker") or ""),
            open_position=open_position,
            entry_ctx=entry_ctx if isinstance(entry_ctx, dict) else None,
            ref_close=ref_close,
            bar_time_et=str(bar_et) if bar_et else None,
            exit_features=d5,
            exit_detail="card_live",
        )
        out["ml_hold_h3"] = hold_h3_advice_from_shadow(hq if isinstance(hq, dict) else {})
    except Exception:
        out["ml_hold_h3"] = hold_h3_advice_from_shadow({"status": "error"})

    try:
        entry_p = float(open_position.get("entry_price") or 0)
        if entry_p > 0:
            out["open_position_pnl_pct"] = round((ref_close / entry_p - 1.0) * 100.0, 2)
    except (TypeError, ValueError):
        pass
    out["has_open_position"] = True
    return out


def append_hold_ml_to_close_context(close_ctx: Dict[str, Any], hq: Dict[str, Any]) -> Dict[str, Any]:
    """Patch SELL context with human-readable hold H3 advice (after hold_quality_ml computed)."""
    out = dict(close_ctx or {})
    if not isinstance(hq, dict):
        return out
    adv = hold_h3_advice_from_shadow(hq)
    out["ml_hold_h3"] = adv
    if adv.get("status") != "ok":
        return out
    line = f"Hold H3 (shadow): {adv['label_ru']}"
    if adv.get("detail_ru"):
        line += f" — {adv['detail_ru']}"
    out["hold_ml_advice_ru"] = line
    tfh = str(out.get("trade_for_human") or "").strip()
    if tfh:
        out["trade_for_human"] = f"{tfh} {line}"
    else:
        out["trade_for_human"] = line
    return out


__all__ = [
    "append_hold_ml_to_close_context",
    "enrich_game5m_card_ml_advice",
    "entry_e3_advice_from_d5",
    "hold_h3_advice_from_shadow",
]
