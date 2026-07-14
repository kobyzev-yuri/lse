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


def compute_portfolio_prospect_priority(
    *,
    regime: Optional[str],
    ret_20d_pct: Optional[float],
    score_20d: Optional[float],
    exp_20d_pct: Optional[float],
    hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Сводка «перспективности» для приоритета портфельной игры.
    Выше — предпочтительнее для нового BUY; ниже — избегать.
    """
    reg = (regime or "neutral").strip().lower()
    try:
        sc = float(score_20d) if score_20d is not None else None
    except (TypeError, ValueError):
        sc = None
    try:
        exp = float(exp_20d_pct) if exp_20d_pct is not None else None
    except (TypeError, ValueError):
        exp = None
    try:
        ret = float(ret_20d_pct) if ret_20d_pct is not None else None
    except (TypeError, ValueError):
        ret = None

    pri = 0.0
    if sc is not None:
        pri += (sc - 50.0) / 10.0
    if exp is not None:
        pri += exp / 2.0
    if ret is not None:
        pri += ret / 10.0
    if reg == "melt_up":
        pri += 1.5
    elif reg == "trend_up":
        pri += 1.0
    elif reg == "breakdown":
        pri -= 1.5
    h = (hint or "").strip().lower()
    if "align_uptrend" in h:
        pri += 1.0
    if "align_breakdown" in h:
        pri -= 1.0
    if "conflict" in h:
        pri -= 0.5

    prefer_min = _float_cfg("PORTFOLIO_PROSPECT_PREFER_MIN", 1.5)
    avoid_max = _float_cfg("PORTFOLIO_PROSPECT_AVOID_MAX", -0.5)
    if pri >= prefer_min:
        tier = "prefer"
    elif pri <= avoid_max:
        tier = "avoid"
    else:
        tier = "allow"

    return {
        "portfolio_prospect_priority": round(pri, 3),
        "portfolio_prospect_tier": tier,
        "portfolio_prospect_note_ru": {
            "prefer": "Перспективный: приоритет для портфельного BUY",
            "allow": "Нейтральный: вход по стратегии без бонуса",
            "avoid": "Слабая перспектива 20d/regime: новый BUY нежелателен",
        }.get(tier, ""),
    }


def portfolio_trend_20d_blocks_buy(ticker: str, *, engine=None) -> tuple[bool, str]:
    """
    Apply-гейт: не открывать BUY по слабой 20d перспективе.
    Включается PORTFOLIO_TREND_20D_BLOCK_BUY_ON_WEAK + gate apply (или явный block flag).
    """
    if not _truthy("PORTFOLIO_TREND_20D_BLOCK_BUY_ON_WEAK", "true"):
        return False, ""
    # Soft on by default once block flag is true; stack gate may still be log_only for resolve path.
    try:
        from services.decision_stack._types import gate_mode

        gm = gate_mode("DECISION_STACK_PORTFOLIO_TREND_CATBOOST_GATE_MODE", "log_only")
        if gm == "none":
            return False, ""
        # log_only: still allow hard block via PORTFOLIO_TREND_20D_BLOCK_BUY_ON_WEAK when apply-ish.
        # Require gate apply OR explicit FORCE.
        force = _truthy("PORTFOLIO_TREND_20D_FORCE_BLOCK", "false")
        if gm != "apply" and not force:
            return False, ""
    except Exception:
        pass

    snap = portfolio_trend_regime_snapshot(ticker, engine=engine)
    try:
        from services.portfolio_catboost_signal import (
            portfolio_ml_20d_regime_hint,
            predict_portfolio_expected_return_20d,
        )

        ml20 = predict_portfolio_expected_return_20d(ticker)
    except Exception as e:
        logger.debug("portfolio_trend_20d_blocks_buy predict %s: %s", ticker, e)
        return False, ""

    if ml20.get("portfolio_ml_20d_status") != "ok":
        return False, ""

    regime = snap.get("portfolio_trend_regime")
    hint = portfolio_ml_20d_regime_hint(ml20.get("portfolio_ml_20d_entry_score"), str(regime) if regime else None)
    prospect = compute_portfolio_prospect_priority(
        regime=str(regime) if regime else None,
        ret_20d_pct=snap.get("portfolio_trend_ret_20d_pct"),
        score_20d=ml20.get("portfolio_ml_20d_entry_score"),
        exp_20d_pct=ml20.get("portfolio_ml_20d_expected_return_pct"),
        hint=hint,
    )
    try:
        min_score = float((get_config_value("PORTFOLIO_TREND_20D_HOLD_BELOW_SCORE", "48") or "48").strip())
    except (TypeError, ValueError):
        min_score = 48.0
    try:
        sc = float(ml20.get("portfolio_ml_20d_entry_score"))
    except (TypeError, ValueError):
        sc = None

    reasons: List[str] = []
    if prospect.get("portfolio_prospect_tier") == "avoid":
        reasons.append(
            f"prospect_tier=avoid (priority={prospect.get('portfolio_prospect_priority')})"
        )
    if sc is not None and sc < min_score and str(regime).lower() in ("breakdown", "neutral", "insufficient"):
        reasons.append(f"20d_score={sc:.1f} < {min_score:.1f} regime={regime}")
    if not reasons:
        return False, ""
    return True, (
        "20d prospect gate: "
        + "; ".join(reasons)
        + " (PORTFOLIO_TREND_20D_BLOCK_BUY_ON_WEAK)"
    )


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
    """Сводка для карточек / analyzer: rule + CatBoost 20d + prospect tiers."""
    rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    hint_counts: Dict[str, int] = {}
    tier_counts: Dict[str, int] = {}
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
            prospect = compute_portfolio_prospect_priority(
                regime=reg,
                ret_20d_pct=snap.get("portfolio_trend_ret_20d_pct"),
                score_20d=ml20.get("portfolio_ml_20d_entry_score"),
                exp_20d_pct=ml20.get("portfolio_ml_20d_expected_return_pct"),
                hint=hint,
            )
            row.update(prospect)
            tier = str(prospect.get("portfolio_prospect_tier") or "n/a")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        except Exception as e:
            row["portfolio_ml_20d_status"] = "error"
            row["portfolio_ml_20d_note"] = str(e)
        rows.append(row)
    rows_sorted = sorted(
        rows,
        key=lambda r: float(r.get("portfolio_prospect_priority") or -999),
        reverse=True,
    )
    out: Dict[str, Any] = {
        "mode": "rule_plus_catboost_20d_prospect",
        "status": "ok",
        "regime_counts": counts,
        "ml_20d_ok_count": ml_ok,
        "regime_hint_counts": hint_counts,
        "prospect_tier_counts": tier_counts,
        "tickers": rows_sorted,
        "priority_top": [
            {
                "ticker": r.get("ticker"),
                "priority": r.get("portfolio_prospect_priority"),
                "tier": r.get("portfolio_prospect_tier"),
                "regime": r.get("portfolio_trend_regime"),
                "score_20d": r.get("portfolio_ml_20d_entry_score"),
            }
            for r in rows_sorted[:8]
        ],
        "catboost_horizon": 20,
        "gate_mode": "log_only",
    }
    try:
        from services.decision_stack._types import gate_mode

        out["gate_mode"] = gate_mode("DECISION_STACK_PORTFOLIO_TREND_CATBOOST_GATE_MODE", "log_only")
    except Exception:
        pass
    return out
