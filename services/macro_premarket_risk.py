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


def get_indicator_gap_detail(ticker: str) -> Dict[str, Any]:
    """
    Гэп % к предыдущему close: премаркет (PRE_MARKET) или последние два дня в quotes.
    Плюс premarket_last / prev_close для отображения в Telegram.
    """
    t = (ticker or "").strip().upper()
    out: Dict[str, Any] = {
        "ticker": t,
        "gap_pct": None,
        "source": "none",
        "premarket_last": None,
        "prev_close": None,
        "error": None,
    }
    if not t:
        return out

    try:
        from services.market_session import get_market_session_context

        phase = (get_market_session_context().get("session_phase") or "").strip()
        if phase == "PRE_MARKET":
            from services.premarket import get_premarket_context

            pm = get_premarket_context(t)
            if pm.get("error"):
                out["error"] = pm.get("error")
            else:
                out["prev_close"] = pm.get("prev_close")
                out["premarket_last"] = pm.get("premarket_last")
                g = pm.get("premarket_gap_pct")
                if g is not None:
                    out["gap_pct"] = float(g)
                    out["source"] = "premarket"
                    return out
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
            out["premarket_last"] = last
            out["prev_close"] = prev
            if prev > 0:
                out["gap_pct"] = round((last / prev - 1.0) * 100.0, 2)
                out["source"] = "quotes_2d"
    except Exception as e:
        logger.debug("quotes gap %s: %s", t, e)
    return out


def get_indicator_gap_pct(ticker: str) -> Tuple[Optional[float], str]:
    """Совместимость: только (gap_pct, source)."""
    det = get_indicator_gap_detail(ticker)
    return det.get("gap_pct"), str(det.get("source") or "none")


def collect_game_5m_premarket_gaps() -> List[Dict[str, Any]]:
    """Премаркет-гэпы по всем тикерам GAME_5m (для макро-алерта)."""
    if not _cfg_bool("GAME_5M_MACRO_INCLUDE_GAME_5M_GAPS_IN_TELEGRAM", True):
        return []
    try:
        from services.ticker_groups import get_tickers_game_5m

        tickers = [str(x).strip().upper() for x in (get_tickers_game_5m() or []) if str(x).strip()]
    except Exception as e:
        logger.debug("collect_game_5m_premarket_gaps: %s", e)
        return []
    rows: List[Dict[str, Any]] = []
    for t in tickers:
        det = get_indicator_gap_detail(t)
        if det.get("gap_pct") is not None or det.get("premarket_last") is not None:
            rows.append(det)
    return rows


def _format_gap_telegram_line(ticker: str, info: Dict[str, Any]) -> Optional[str]:
    g = info.get("gap_pct")
    last = info.get("premarket_last")
    if g is None and last is None:
        return None
    line = f"• {ticker}:"
    if last is not None:
        line += f" {float(last):.2f}"
    if g is not None:
        if last is not None:
            line += ","
        line += f" гэп {float(g):+.2f}%"
    extras: List[str] = []
    prev = info.get("prev_close")
    if prev is not None:
        extras.append(f"вчера {float(prev):.2f}")
    src = (info.get("source") or "").strip()
    if src and src != "none":
        extras.append(src)
    if extras:
        line += f" ({', '.join(extras)})"
    return line


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
        det = get_indicator_gap_detail(t)
        indicators[t] = {
            "gap_pct": det.get("gap_pct"),
            "source": det.get("source"),
            "premarket_last": det.get("premarket_last"),
            "prev_close": det.get("prev_close"),
        }
    game_5m_gaps = collect_game_5m_premarket_gaps()

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
    if _cfg_bool("GAME_5M_MACRO_PREDICT_SECTOR_GAP_ENABLED", True):
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
        "game_5m_gaps": game_5m_gaps,
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
        pred = macro.get("macro_predicted_sector_gap_pct")
        proxy = (macro.get("macro_sector_proxy") or "SMH").strip()
        pred_note = ""
        if pred is not None:
            pred_note = f"прогноз гэпа {proxy} {float(pred):+.2f}%"
        hint = f"Макро: возможен гэп вверх по риск-активам"
        if pred_note:
            hint = f"{hint} ({pred_note})"
        if macro_note:
            hint = f"{hint}. {macro_note}"
        reason = hint if not reason or reason.startswith("Нет явных") else f"{reason}; {hint}"
        return advice, reason

    return advice, reason


def _format_game5m_ticker_gap_forecast_line(det: Dict[str, Any], macro: Dict[str, Any]) -> Optional[str]:
    """Строка GAME_5m: прогноз гэпа на open (OLS тикер / прокси сектора) + факт премаркета."""
    t = (det.get("ticker") or "?").strip().upper()
    if det.get("error") and det.get("gap_pct") is None and det.get("premarket_last") is None:
        return f"• {t}: нет данных ({det.get('error')})"
    pred: Optional[float] = None
    src = ""
    try:
        from services.ticker_open_gap_predict import predict_ticker_open_gap_pct

        pred, src = predict_ticker_open_gap_pct(t, macro_risk=macro)
    except Exception as e:
        logger.debug("game5m gap forecast line %s: %s", t, e)
    parts: List[str] = []
    if pred is not None:
        src_lbl = {
            "ticker_ols": "OLS тикер",
            "ticker_ols_v2": "OLS v2",
            "ticker_ols_v2_premarket_blend": "OLS v2+премаркет",
            "sector_proxy": f"прокси {(macro.get('macro_sector_proxy') or 'SMH')}",
        }.get(src or "", src or "")
        label = f"прогноз {float(pred):+.2f}%"
        if src_lbl:
            label += f" ({src_lbl})"
        parts.append(label)
    fact = det.get("gap_pct")
    if fact is not None:
        parts.append(f"премаркет {float(fact):+.2f}%")
    last = det.get("premarket_last")
    if not parts:
        if last is not None:
            return f"• {t}: {float(last):.2f} (гэп н/д)"
        return None
    prefix = f"• {t}"
    if last is not None:
        prefix = f"• {t} {float(last):.2f}"
    return f"{prefix}: " + ", ".join(parts)


def format_sector_and_game5m_gap_lines(macro: Dict[str, Any]) -> List[str]:
    """Секторный прогноз гэпа (OLS SMH) и по тикерам GAME_5m: прогноз open + факт премаркета."""
    if not macro.get("enabled"):
        return []
    lines: List[str] = []
    pred = macro.get("macro_predicted_sector_gap_pct")
    proxy = (macro.get("macro_sector_proxy") or "SMH").strip()
    if pred is not None:
        lines.append(f"Сектор {proxy} (прогноз OLS): {float(pred):+.2f}%")
    game_rows = macro.get("game_5m_gaps") or []
    if game_rows:
        if lines:
            lines.append("")
        lines.append("GAME 5m — гэп на open:")
        for det in game_rows:
            row = _format_game5m_ticker_gap_forecast_line(det if isinstance(det, dict) else {}, macro)
            if row:
                lines.append(row)
    return lines


def format_macro_telegram_lines(macro: Dict[str, Any]) -> List[str]:
    """Строки для Telegram (plain text)."""
    if not macro.get("enabled"):
        return []
    lines = [
        f"Макро: {macro.get('risk_level')}, ожидание по риск-активам: {macro.get('equity_gap_bias')}",
    ]
    for t, info in (macro.get("indicators") or {}).items():
        row = _format_gap_telegram_line(t, info if isinstance(info, dict) else {})
        if row:
            lines.append(row)
    gap_block = format_sector_and_game5m_gap_lines(macro)
    if gap_block:
        lines.append("")
        lines.extend(gap_block)
    for r in (macro.get("reasons") or [])[:5]:
        lines.append(f"  — {r}")
    return lines
