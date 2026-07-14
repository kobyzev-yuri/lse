"""
Портфельная игра: rule-based режим тренда (20d) для входа/выхода.

MVP до CatBoost 20d: melt_up / trend_up / neutral / breakdown.
Снимок пишется в context_json на BUY; exit policy читает regime с входа.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
from sqlalchemy import text

from config_loader import get_config_value

logger = logging.getLogger(__name__)

REGIMES = frozenset({"melt_up", "trend_up", "neutral", "breakdown", "insufficient"})


def _truthy(key: str, default: str = "true") -> bool:
    return (get_config_value(key, default) or default).strip().lower() in ("1", "true", "yes", "on")


def _float_cfg(key: str, default: float) -> float:
    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _int_cfg(key: str, default: int) -> int:
    try:
        return int((get_config_value(key, str(default)) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def trend_regime_thresholds() -> Dict[str, float]:
    return {
        "melt_up_ret_20d_min": _float_cfg("PORTFOLIO_TREND_MELT_UP_MIN_RET_20D_PCT", 12.0),
        "melt_up_sma20_days_min": float(_int_cfg("PORTFOLIO_TREND_MELT_UP_SMA20_DAYS", 12)),
        "trend_up_ret_20d_min": _float_cfg("PORTFOLIO_TREND_TREND_UP_MIN_RET_20D_PCT", 5.0),
        "breakdown_ret_20d_max": _float_cfg("PORTFOLIO_TREND_BREAKDOWN_MAX_RET_20D_PCT", -5.0),
        "near_high_pct": _float_cfg("PORTFOLIO_TREND_NEAR_HIGH_PCT", 2.5),
        "late_chase_ret_20d_min": _float_cfg("PORTFOLIO_TREND_LATE_CHASE_MIN_RET_20D_PCT", 25.0),
    }


def classify_trend_regime_from_closes(
    closes: Sequence[float],
    *,
    thresholds: Optional[Dict[str, float]] = None,
    sma_period: int = 20,
) -> Dict[str, Any]:
    """Pure classifier for tests. closes[-1] = latest."""
    thr = thresholds or trend_regime_thresholds()
    vals = [float(x) for x in closes if x is not None and math.isfinite(float(x)) and float(x) > 0]
    if len(vals) < sma_period + 1:
        return {
            "regime": "insufficient",
            "ret_20d_pct": None,
            "sma20": None,
            "days_above_sma20": 0,
            "near_20d_high": False,
            "drawdown_from_20d_high_pct": None,
            "note_ru": "Мало дневных close для 20d режима",
        }

    s = pd.Series(vals)
    close = float(s.iloc[-1])
    ref = float(s.iloc[-(sma_period + 1)])
    ret_20d = (close / ref - 1.0) * 100.0 if ref > 0 else None
    sma20 = float(s.rolling(sma_period, min_periods=sma_period).mean().iloc[-1])
    tail = s.iloc[-sma_period:]
    sma_tail = s.rolling(sma_period, min_periods=sma_period).mean().iloc[-sma_period:]
    days_above = int((tail.values > sma_tail.values).sum())
    high_20d = float(s.iloc[-sma_period:].max())
    dd_high = (close / high_20d - 1.0) * 100.0 if high_20d > 0 else None
    near_high = dd_high is not None and dd_high >= -float(thr["near_high_pct"])

    regime = "neutral"
    if ret_20d is not None:
        if ret_20d <= float(thr["breakdown_ret_20d_max"]) or (close < sma20 and ret_20d < 0):
            regime = "breakdown"
        elif ret_20d >= float(thr["melt_up_ret_20d_min"]) and days_above >= int(thr["melt_up_sma20_days_min"]):
            regime = "melt_up"
        elif ret_20d >= float(thr["trend_up_ret_20d_min"]):
            regime = "trend_up"

    notes = {
        "melt_up": "Сильный 20d тренд: широкий trailing, не резать на мелком take",
        "trend_up": "Умеренный ап-тренд: стандартный trailing",
        "neutral": "Боковик/слабый тренд",
        "breakdown": "Слабость/просадка 20d: жёстче выход",
        "insufficient": "Недостаточно истории",
    }
    return {
        "regime": regime,
        "ret_20d_pct": round(ret_20d, 3) if ret_20d is not None else None,
        "sma20": round(sma20, 4) if math.isfinite(sma20) else None,
        "days_above_sma20": days_above,
        "near_20d_high": near_high,
        "drawdown_from_20d_high_pct": round(dd_high, 3) if dd_high is not None else None,
        "note_ru": notes.get(regime, ""),
    }


def _load_daily_closes(engine, ticker: str, *, lookback_days: int = 60) -> List[float]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT close FROM quotes
                WHERE ticker = :ticker
                  AND date >= (CURRENT_DATE - CAST(:days AS integer))
                ORDER BY date ASC
                """
            ),
            {"ticker": ticker.strip().upper(), "days": int(lookback_days)},
        ).fetchall()
    out: List[float] = []
    for r in rows:
        try:
            v = float(r[0])
            if v > 0:
                out.append(v)
        except (TypeError, ValueError):
            continue
    return out


def portfolio_trend_regime_snapshot(ticker: str, *, engine=None) -> Dict[str, Any]:
    """Latest trend regime for portfolio card / BUY context."""
    if not _truthy("PORTFOLIO_TREND_REGIME_ENABLED", "true"):
        return {"portfolio_trend_regime": "disabled", "portfolio_trend_status": "disabled"}
    try:
        eng = engine
        if eng is None:
            from report_generator import get_engine

            eng = get_engine()
        closes = _load_daily_closes(eng, ticker)
        core = classify_trend_regime_from_closes(closes)
        exit_note = exit_policy_note_for_regime(core.get("regime"))
        return {
            "portfolio_trend_regime": core.get("regime"),
            "portfolio_trend_ret_20d_pct": core.get("ret_20d_pct"),
            "portfolio_trend_sma20": core.get("sma20"),
            "portfolio_trend_days_above_sma20": core.get("days_above_sma20"),
            "portfolio_trend_near_20d_high": core.get("near_20d_high"),
            "portfolio_trend_drawdown_from_20d_high_pct": core.get("drawdown_from_20d_high_pct"),
            "portfolio_trend_note_ru": core.get("note_ru"),
            "portfolio_trend_exit_policy_ru": exit_note,
            "portfolio_trend_status": "ok",
        }
    except Exception as e:
        logger.debug("portfolio_trend_regime %s: %s", ticker, e)
        return {"portfolio_trend_regime": "error", "portfolio_trend_status": "error", "portfolio_trend_note_ru": str(e)}


def exit_policy_note_for_regime(regime: Optional[str]) -> str:
    r = (regime or "neutral").strip().lower()
    if r == "melt_up":
        return "melt_up: trailing arm выше, pullback шире, take cap выше"
    if r == "breakdown":
        return "breakdown: trailing arm ниже, pullback уже"
    if r == "trend_up":
        return "trend_up: базовые параметры take/trailing"
    return "neutral: базовые параметры take/trailing"


def regime_from_context(ctx: Optional[Dict[str, Any]]) -> str:
    if not isinstance(ctx, dict):
        return "neutral"
    raw = ctx.get("portfolio_trend_regime") or ctx.get("trend_regime")
    r = str(raw or "neutral").strip().lower()
    return r if r in REGIMES else "neutral"


def portfolio_trend_late_chase_blocks_buy(ticker: str, *, engine=None) -> tuple[bool, str]:
    """Блок входа у 20d high после сильного ралли (INTC Jun-24 кейс)."""
    if not _truthy("PORTFOLIO_TREND_LATE_CHASE_BLOCK_ENABLED", "true"):
        return False, ""
    snap = portfolio_trend_regime_snapshot(ticker, engine=engine)
    if snap.get("portfolio_trend_status") != "ok":
        return False, ""
    ret20 = snap.get("portfolio_trend_ret_20d_pct")
    near = snap.get("portfolio_trend_near_20d_high")
    thr = trend_regime_thresholds()["late_chase_ret_20d_min"]
    try:
        ret_f = float(ret20) if ret20 is not None else None
    except (TypeError, ValueError):
        ret_f = None
    if near and ret_f is not None and ret_f >= thr:
        return True, (
            f"late chase: ret_20d={ret_f:.1f}% >= {thr:.1f}% и цена у 20d high "
            f"(PORTFOLIO_TREND_LATE_CHASE_BLOCK)"
        )
    return False, ""


def build_portfolio_trend_regime_review(tickers: Sequence[str], *, engine=None) -> Dict[str, Any]:
    """Сводка для карточек / analyzer: rule + CatBoost 20d log_only."""
    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    hint_counts: Dict[str, int] = {}
    ml_ok = 0
    for t in tickers:
        snap = portfolio_trend_regime_snapshot(t, engine=engine)
        reg = str(snap.get("portfolio_trend_regime") or "n/a")
        counts[reg] = counts.get(reg, 0) + 1
        row: Dict[str, Any] = {"ticker": t, **snap}
        try:
            from services.portfolio_catboost_signal import (
                portfolio_ml_20d_regime_hint,
                predict_portfolio_expected_return_20d,
            )

            ml20 = predict_portfolio_expected_return_20d(t)
            for k, v in ml20.items():
                if k.startswith("portfolio_ml_20d_"):
                    row[k] = v
            if ml20.get("portfolio_ml_20d_status") == "ok":
                ml_ok += 1
            hint = portfolio_ml_20d_regime_hint(
                ml20.get("portfolio_ml_20d_entry_score"),
                reg,
            )
            row["portfolio_ml_20d_regime_hint"] = hint
            hint_counts[hint] = hint_counts.get(hint, 0) + 1
        except Exception as e:
            row["portfolio_ml_20d_status"] = "error"
            row["portfolio_ml_20d_note"] = str(e)
        rows.append(row)
    return {
        "mode": "rule_plus_catboost_20d_log_only",
        "status": "ok",
        "regime_counts": counts,
        "ml_20d_ok_count": ml_ok,
        "regime_hint_counts": hint_counts,
        "tickers": rows,
        "catboost_horizon": 20,
        "gate_mode": "log_only",
    }
