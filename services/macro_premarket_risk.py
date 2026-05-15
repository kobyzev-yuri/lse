"""
Макро-риск премаркета для GAME_5m: VIX, Forex, нефть.

- risk-off (AVOID): рост VIX, слабость Forex (гэп вниз), скачок нефти вверх (гео).
- favorable (ожидание гэпа вверх по риск-активам): падение VIX, укрепление Forex, нефть вниз при спокойном VIX.

Нефть вниз сама по себе не считается стрессом (в отличие от старого PREMARKET_STRESS по CL≤-1.5%).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_bool(key: str, default: bool = True) -> bool:
    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _parse_tickers(raw: str) -> List[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def macro_risk_enabled() -> bool:
    return _cfg_bool("GAME_5M_MACRO_RISK_ENABLED", True)


def get_macro_forex_tickers() -> List[str]:
    raw = get_config_value(
        "GAME_5M_MACRO_FOREX_TICKERS",
        get_config_value("PREMARKET_STRESS_TICKERS", "GBPUSD=X,EURUSD=X") or "GBPUSD=X,EURUSD=X",
    )
    out = [t for t in _parse_tickers(raw or "") if t not in ("CL=F", "BZ=F", "^VIX")]
    return out or ["GBPUSD=X", "EURUSD=X"]


def get_macro_vix_ticker() -> str:
    return (get_config_value("GAME_5M_MACRO_VIX_TICKER", "^VIX") or "^VIX").strip()


def get_macro_oil_ticker() -> str:
    return (get_config_value("GAME_5M_MACRO_OIL_TICKER", "CL=F") or "CL=F").strip()


def get_indicator_gap_pct(ticker: str) -> Tuple[Optional[float], str]:
    """
    Гэп % к предыдущему close: премаркет (PRE_MARKET) или последние два дня в quotes.
    Returns (gap_pct, source).
    """
    t = (ticker or "").strip()
    if not t:
        return None, "none"
    try:
        from services.market_session import get_market_session_context

        phase = (get_market_session_context().get("session_phase") or "").strip()
        if phase == "PRE_MARKET":
            from services.premarket import get_premarket_context

            pm = get_premarket_context(t)
            if pm.get("error"):
                pass
            else:
                g = pm.get("premarket_gap_pct")
                if g is not None:
                    return float(g), "premarket"
    except Exception as e:
        logger.debug("premarket gap %s: %s", t, e)

    try:
        from sqlalchemy import text
        from report_generator import get_engine

        eng = get_engine()
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT close FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 2
                    """
                ),
                {"ticker": t},
            ).fetchall()
        if len(rows) >= 2 and rows[0][0] is not None and rows[1][0] is not None:
            last = float(rows[0][0])
            prev = float(rows[1][0])
            if prev > 0:
                return round((last / prev - 1.0) * 100.0, 2), "quotes_2d"
    except Exception as e:
        logger.debug("quotes gap %s: %s", t, e)
    return None, "none"


def evaluate_macro_premarket_risk() -> Dict[str, Any]:
    """
    Сводка макро для GAME_5m / премаркет-крона.

    risk_level: NEUTRAL | CAUTION | AVOID
    equity_gap_bias: NEUTRAL | DOWN | UP
    """
    neutral: Dict[str, Any] = {
        "enabled": False,
        "risk_level": "NEUTRAL",
        "equity_gap_bias": "NEUTRAL",
        "risk_score": 0,
        "favorable_score": 0,
        "indicators": {},
        "reasons": [],
        "close_game_alert": False,
    }
    if not macro_risk_enabled():
        return neutral

    forex = get_macro_forex_tickers()
    vix_t = get_macro_vix_ticker()
    oil_t = get_macro_oil_ticker()

    forex_avoid = _cfg_float("GAME_5M_MACRO_FOREX_GAP_AVOID_PCT", -1.0)
    forex_fav = _cfg_float("GAME_5M_MACRO_FOREX_GAP_FAVORABLE_PCT", 0.5)
    vix_avoid = _cfg_float("GAME_5M_MACRO_VIX_GAP_AVOID_PCT", 1.5)
    vix_fav = _cfg_float("GAME_5M_MACRO_VIX_GAP_FAVORABLE_PCT", -1.0)
    oil_stress_up = _cfg_float("GAME_5M_MACRO_OIL_GAP_STRESS_UP_PCT", 2.0)
    oil_fav_down = _cfg_float("GAME_5M_MACRO_OIL_GAP_FAVORABLE_DOWN_PCT", -1.5)
    vix_calm_for_oil_fav = _cfg_float("GAME_5M_MACRO_VIX_CALM_MAX_FOR_OIL_FAV", 1.0)

    indicators: Dict[str, Dict[str, Any]] = {}
    for t in forex + [vix_t, oil_t]:
        g, src = get_indicator_gap_pct(t)
        indicators[t] = {"gap_pct": g, "source": src}

    risk_score = 0
    fav_score = 0
    reasons: List[str] = []

    vix_gap = (indicators.get(vix_t) or {}).get("gap_pct")
    if vix_gap is not None:
        if float(vix_gap) >= vix_avoid:
            risk_score += 2
            reasons.append(f"VIX {float(vix_gap):+.2f}% (рост страха, порог {vix_avoid:+.1f}%)")
        elif float(vix_gap) <= vix_fav:
            fav_score += 1
            reasons.append(f"VIX {float(vix_gap):+.2f}% (снижение страха)")

    forex_gaps: List[float] = []
    for t in forex:
        g = (indicators.get(t) or {}).get("gap_pct")
        if g is not None:
            forex_gaps.append(float(g))
    if forex_gaps:
        worst = min(forex_gaps)
        best = max(forex_gaps)
        if worst <= forex_avoid:
            risk_score += 2
            reasons.append(f"Forex слабость (худший гэп {worst:+.2f}%, порог {forex_avoid:+.1f}%)")
        if best >= forex_fav and worst > forex_avoid:
            fav_score += 1
            reasons.append(f"Forex укрепление (лучший гэп {best:+.2f}%)")

    oil_gap = (indicators.get(oil_t) or {}).get("gap_pct")
    if oil_gap is not None:
        og = float(oil_gap)
        if og >= oil_stress_up:
            risk_score += 1
            reasons.append(f"Нефть {og:+.2f}% вверх (гео/инфляционный стресс, порог {oil_stress_up:+.1f}%)")
        elif og <= oil_fav_down and vix_gap is not None and float(vix_gap) < vix_calm_for_oil_fav:
            fav_score += 1
            reasons.append(f"Нефть {og:+.2f}% вниз при VIX < {vix_calm_for_oil_fav:.1f}%")

    if risk_score >= 2:
        risk_level = "AVOID"
        bias = "DOWN"
        close_alert = True
    elif risk_score >= 1:
        risk_level = "CAUTION"
        bias = "DOWN"
        close_alert = _cfg_bool("PREMARKET_STRESS_ALERT_ON_CAUTION", False)
    elif fav_score >= 2:
        risk_level = "CAUTION"
        bias = "UP"
        close_alert = False
    elif fav_score >= 1:
        risk_level = "NEUTRAL"
        bias = "UP"
        close_alert = False
    else:
        risk_level = "NEUTRAL"
        bias = "NEUTRAL"
        close_alert = False

    predicted_sector_gap_pct: Optional[float] = None
    sector_proxy = (get_config_value("GAME_5M_MACRO_SECTOR_PROXY", "SMH") or "SMH").strip().upper()
    if _cfg_bool("GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED", False):
        # OLS SMH n≈326 (analyze_macro_gap_indicators); коэффициенты переопределяются в config
        c0 = _cfg_float("GAME_5M_MACRO_PREDICT_CONST", 0.3867)
        b_vix = _cfg_float("GAME_5M_MACRO_PREDICT_BETA_VIX", -0.1842)
        b_gbp = _cfg_float("GAME_5M_MACRO_PREDICT_BETA_GBPUSD", -0.6266)
        b_eur = _cfg_float("GAME_5M_MACRO_PREDICT_BETA_EURUSD", 0.1921)
        b_cl = _cfg_float("GAME_5M_MACRO_PREDICT_BETA_CL", 0.1123)
        g_gbp = (indicators.get("GBPUSD=X") or {}).get("gap_pct")
        g_eur = (indicators.get("EURUSD=X") or {}).get("gap_pct")
        if vix_gap is not None:
            pred = c0 + b_vix * float(vix_gap)
            if g_gbp is not None:
                pred += b_gbp * float(g_gbp)
            if g_eur is not None:
                pred += b_eur * float(g_eur)
            if oil_gap is not None:
                pred += b_cl * float(oil_gap)
            predicted_sector_gap_pct = round(pred, 3)

    return {
        "enabled": True,
        "risk_level": risk_level,
        "equity_gap_bias": bias,
        "risk_score": risk_score,
        "favorable_score": fav_score,
        "indicators": indicators,
        "reasons": reasons,
        "close_game_alert": close_alert,
        "macro_sector_proxy": sector_proxy,
        "macro_predicted_sector_gap_pct": predicted_sector_gap_pct,
        "thresholds": {
            "forex_avoid": forex_avoid,
            "forex_favorable": forex_fav,
            "vix_avoid": vix_avoid,
            "vix_favorable": vix_fav,
            "oil_stress_up": oil_stress_up,
            "oil_favorable_down": oil_fav_down,
        },
    }


def apply_macro_to_entry_advice(
    entry_advice: str,
    entry_advice_reason: str,
    macro: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Ужесточает entry_advice по макро; не снимает AVOID из новостей/волы."""
    if not macro or not macro.get("enabled"):
        return entry_advice, entry_advice_reason
    level = (macro.get("risk_level") or "NEUTRAL").strip().upper()
    bias = (macro.get("equity_gap_bias") or "NEUTRAL").strip().upper()
    parts = [p for p in (macro.get("reasons") or []) if p]
    macro_note = "; ".join(parts[:3]) if parts else ""

    advice = (entry_advice or "ALLOW").strip().upper()
    reason = entry_advice_reason or ""

    if level == "AVOID":
        if advice != "AVOID":
            reason = f"Макро risk-off: {macro_note}" if macro_note else "Макро risk-off (VIX/Forex)"
        else:
            reason = (reason + f"; макро: {macro_note}").strip("; ") if macro_note else reason
        return "AVOID", reason

    if level == "CAUTION" and bias == "DOWN":
        if advice == "ALLOW":
            advice = "CAUTION"
            reason = f"Макро: осторожность (гэп вниз по риск-активам). {macro_note}".strip()
        elif advice == "CAUTION" and macro_note:
            reason = (reason + f"; {macro_note}").strip("; ")
        return advice, reason

    if bias == "UP" and advice == "ALLOW":
        hint = f"Макро: возможен гэп вверх по риск-активам. {macro_note}".strip()
        reason = hint if not reason or reason.startswith("Нет явных") else f"{reason}; {macro_note}"
        return advice, reason

    return advice, reason


def format_macro_telegram_lines(macro: Dict[str, Any]) -> List[str]:
    """Строки для Telegram (plain text)."""
    if not macro.get("enabled"):
        return []
    lines = [
        f"Макро: {macro.get('risk_level')}, ожидание по риск-активам: {macro.get('equity_gap_bias')}",
    ]
    for t, info in (macro.get("indicators") or {}).items():
        g = info.get("gap_pct")
        if g is None:
            continue
        src = info.get("source") or ""
        lines.append(f"• {t}: гэп {float(g):+.2f}% ({src})")
    for r in (macro.get("reasons") or [])[:5]:
        lines.append(f"  — {r}")
    return lines
