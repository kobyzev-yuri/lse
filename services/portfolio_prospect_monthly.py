"""
Monthly strategic allocation report for portfolio game.

Answers: where to focus first, and what profit order of magnitude is plausible
(~6m chart path + capture band + current 20d forward) — not a same-day BUY signal.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

BUCKET_RANK = {
    "core_prospect": 0,
    "watch_long": 1,
    "neutral": 2,
    "tactical_avoid": 3,
    "structurally_weak": 4,
}


def default_monthly_review_path(project_root: Path | None = None) -> Path:
    root = project_root or Path(__file__).resolve().parents[1]
    app = Path("/app/logs/ml/ml_data_quality/last_portfolio_prospect_monthly_review.json")
    if app.parent.exists() or Path("/app/logs").exists():
        return app
    return root / "local" / "logs" / "ml_data_quality" / "last_portfolio_prospect_monthly_review.json"


def load_last_monthly_review(path: Path | None = None) -> Optional[Dict[str, Any]]:
    p = path or default_monthly_review_path()
    try:
        if not p.is_file():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def fetch_daily_closes(engine, ticker: str, *, trading_days: int = 126) -> List[float]:
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT close FROM quotes
                WHERE ticker = :t
                ORDER BY date DESC
                LIMIT :n
                """
            ),
            {"t": ticker.strip().upper(), "n": int(trading_days) + 1},
        ).fetchall()
    closes: List[float] = []
    for r in reversed(rows):
        try:
            v = float(r[0])
            if v > 0 and math.isfinite(v):
                closes.append(v)
        except (TypeError, ValueError):
            continue
    return closes


def compute_path_metrics(closes: Sequence[float], *, min_bars: int = 40) -> Dict[str, Any]:
    """Multi-month path features from daily closes (oldest → newest)."""
    c = [float(x) for x in closes if x and float(x) > 0 and math.isfinite(float(x))]
    if len(c) < min_bars:
        return {
            "bars": len(c),
            "market_move_6m_pct": None,
            "log_ret_6m": None,
            "mfe_from_start_pct": None,
            "max_drawdown_pct": None,
            "pct_days_above_sma50": None,
            "dist_from_6m_high_pct": None,
            "path_tag": "insufficient",
            "near_6m_high": False,
        }

    first, last = c[0], c[-1]
    market = (last / first - 1.0) * 100.0
    log_ret = math.log(last / first)
    peak = first
    max_dd = 0.0
    run_max = first
    for x in c:
        if x > run_max:
            run_max = x
        if x > peak:
            peak = x
        dd = (x / run_max - 1.0) * 100.0
        if dd < max_dd:
            max_dd = dd
    mfe = (peak / first - 1.0) * 100.0
    dist_high = (last / peak - 1.0) * 100.0 if peak > 0 else None

    sma_win = 50
    above = 0
    counted = 0
    for i in range(sma_win - 1, len(c)):
        window = c[i - sma_win + 1 : i + 1]
        sma = sum(window) / sma_win
        counted += 1
        if c[i] >= sma:
            above += 1
    pct_sma = (100.0 * above / counted) if counted else None

    near_high = dist_high is not None and dist_high >= -5.0
    # Spike: large MFE but finished far below peak (blow-off / failed rally)
    spike_ratio = None
    if mfe > 15 and dist_high is not None and dist_high < -15:
        giveback = abs(dist_high)
        spike_ratio = round(giveback / max(mfe, 1e-6), 3)

    if market <= -20 or (pct_sma is not None and pct_sma < 35 and market < 0):
        path_tag = "breakdown"
    elif near_high and market >= 40 and (pct_sma or 0) >= 60:
        path_tag = "melt_up_path"
    elif (pct_sma or 0) >= 55 and market >= 15:
        path_tag = "grind_up"
    elif spike_ratio is not None and spike_ratio >= 0.45:
        path_tag = "blowoff"
    elif abs(market) < 12 and (pct_sma or 50) < 55:
        path_tag = "chop"
    else:
        path_tag = "mixed"

    return {
        "bars": len(c),
        "market_move_6m_pct": round(market, 2),
        "log_ret_6m": round(log_ret, 6),
        "mfe_from_start_pct": round(mfe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "pct_days_above_sma50": round(pct_sma, 1) if pct_sma is not None else None,
        "dist_from_6m_high_pct": round(dist_high, 2) if dist_high is not None else None,
        "path_tag": path_tag,
        "near_6m_high": bool(near_high),
        "spike_ratio": spike_ratio,
    }


def capture_band_from_path(path: Dict[str, Any]) -> Dict[str, Any]:
    """
    Heuristic swing-capture band vs buy&hold market move.
    Not a backtest — order-of-magnitude for allocation discussion.
    """
    mfe = path.get("mfe_from_start_pct")
    tag = str(path.get("path_tag") or "")
    if mfe is None or not math.isfinite(float(mfe)) or float(mfe) <= 0:
        return {
            "capture_proxy_lo_pct": None,
            "capture_proxy_hi_pct": None,
            "capture_note_ru": "Мало трендового пути для оценки capture.",
        }
    mfe_f = float(mfe)
    if tag in ("grind_up", "melt_up_path"):
        lo_f, hi_f = 0.30, 0.55
    elif tag == "blowoff":
        lo_f, hi_f = 0.10, 0.25
    elif tag == "breakdown":
        lo_f, hi_f = 0.05, 0.15
    elif tag == "chop":
        lo_f, hi_f = 0.10, 0.20
    else:
        lo_f, hi_f = 0.20, 0.40
    return {
        "capture_proxy_lo_pct": round(mfe_f * lo_f, 1),
        "capture_proxy_hi_pct": round(mfe_f * hi_f, 1),
        "capture_note_ru": (
            f"Ориентир swing-capture ~{int(lo_f*100)}–{int(hi_f*100)}% от MFE пути "
            f"({mfe_f:.0f}%), не buy&hold."
        ),
    }


def assign_strategic_bucket(
    *,
    market_move_6m_pct: Optional[float],
    prospect_tier: Optional[str],
    path: Dict[str, Any],
) -> str:
    tier = (prospect_tier or "n/a").strip().lower()
    ret6 = market_move_6m_pct
    tag = str(path.get("path_tag") or "")
    near = bool(path.get("near_6m_high"))

    if ret6 is not None and ret6 <= -20:
        return "structurally_weak"
    if tag == "blowoff" and near:
        return "tactical_avoid"
    if ret6 is not None and ret6 >= 40 and tier == "prefer" and not near:
        return "core_prospect"
    if ret6 is not None and ret6 >= 40 and tier == "prefer" and near:
        # Strong path but already at highs — focus watch, don't chase as core
        return "watch_long"
    if ret6 is not None and ret6 >= 25 and tier in ("prefer", "allow"):
        return "watch_long"
    if tag in ("grind_up", "melt_up_path") and tier in ("prefer", "allow") and (ret6 or 0) >= 15:
        return "watch_long"
    if tier == "avoid":
        return "tactical_avoid"
    return "neutral"


def _allocation_score(
    *,
    bucket: str,
    path: Dict[str, Any],
    prospect_priority: Optional[float],
    exp_20d_pct: Optional[float],
) -> float:
    score = 10.0 - float(BUCKET_RANK.get(bucket, 9))
    tag = str(path.get("path_tag") or "")
    if tag in ("grind_up", "melt_up_path"):
        score += 1.5
    elif tag == "blowoff":
        score -= 1.0
    elif tag == "breakdown":
        score -= 2.0
    dist = path.get("dist_from_6m_high_pct")
    try:
        if dist is not None and float(dist) < -8:
            score += 0.5  # room below high
        if dist is not None and float(dist) >= -2:
            score -= 0.8  # sitting on highs
    except (TypeError, ValueError):
        pass
    try:
        if prospect_priority is not None:
            score += float(prospect_priority) * 0.35
    except (TypeError, ValueError):
        pass
    try:
        if exp_20d_pct is not None:
            score += float(exp_20d_pct) * 0.15
    except (TypeError, ValueError):
        pass
    return round(score, 3)


def focus_action_ru(
    *,
    bucket: str,
    tier: str,
    path: Dict[str, Any],
    capture: Dict[str, Any],
    exp_20d_pct: Optional[float],
) -> str:
    market = path.get("market_move_6m_pct")
    lo = capture.get("capture_proxy_lo_pct")
    hi = capture.get("capture_proxy_hi_pct")
    rem = path.get("dist_from_6m_high_pct")
    parts: List[str] = []
    if bucket == "core_prospect":
        parts.append("Приоритет #1 для капитала/внимания.")
    elif bucket == "watch_long":
        parts.append("Вторая очередь: держать в фокусе, вход по тактике 20d.")
    elif bucket == "tactical_avoid":
        parts.append("Сейчас не наращивать: слабый 20d / late zone.")
    elif bucket == "structurally_weak":
        parts.append("Не ядро портфеля на этом горизонте.")
    else:
        parts.append("Нейтрально: без отдельного фокуса.")

    if market is not None:
        parts.append(f"Рынок ~6м: {market:+.0f}%.")
    if lo is not None and hi is not None:
        parts.append(f"Swing-capture ориентир: {lo:.0f}…{hi:.0f}%.")
    if rem is not None:
        parts.append(f"До 6м-хая: {rem:+.1f}%.")
    if exp_20d_pct is not None:
        parts.append(f"Форвард 20d: {float(exp_20d_pct):+.1f}%.")
    if tier == "avoid":
        parts.append("Тактика: avoid.")
    elif tier == "prefer":
        parts.append("Тактика: prefer.")
    return " ".join(parts)


def build_portfolio_prospect_monthly_report(
    tickers: Sequence[str],
    *,
    engine=None,
    lookback_trading_days: int = 126,
) -> Dict[str, Any]:
    from services.portfolio_trend_regime import build_portfolio_trend_regime_review

    if engine is None:
        from report_generator import get_engine

        engine = get_engine()

    tickers_u = [str(t).strip().upper() for t in tickers if str(t).strip()]
    review = build_portfolio_trend_regime_review(tickers_u, engine=engine)
    by_t = {str(r.get("ticker") or "").upper(): r for r in (review.get("tickers") or [])}

    rows: List[Dict[str, Any]] = []
    for tu in tickers_u:
        snap = by_t.get(tu) or {}
        closes = fetch_daily_closes(engine, tu, trading_days=int(lookback_trading_days))
        path = compute_path_metrics(closes)
        capture = capture_band_from_path(path)
        market = path.get("market_move_6m_pct")
        tier = snap.get("portfolio_prospect_tier") or "n/a"
        pri = snap.get("portfolio_prospect_priority")
        exp20 = snap.get("portfolio_ml_20d_expected_return_pct")
        bucket = assign_strategic_bucket(
            market_move_6m_pct=market if isinstance(market, (int, float)) else None,
            prospect_tier=str(tier),
            path=path,
        )
        alloc = _allocation_score(
            bucket=bucket,
            path=path,
            prospect_priority=float(pri) if pri is not None else None,
            exp_20d_pct=float(exp20) if exp20 is not None else None,
        )
        action = focus_action_ru(
            bucket=bucket,
            tier=str(tier),
            path=path,
            capture=capture,
            exp_20d_pct=float(exp20) if exp20 is not None else None,
        )
        rows.append(
            {
                "ticker": tu,
                "strategic_bucket": bucket,
                "allocation_score": alloc,
                "market_move_6m_pct": market,
                "ret_approx_6m_pct": market,  # alias for older consumers
                "mfe_from_start_pct": path.get("mfe_from_start_pct"),
                "max_drawdown_pct": path.get("max_drawdown_pct"),
                "pct_days_above_sma50": path.get("pct_days_above_sma50"),
                "dist_from_6m_high_pct": path.get("dist_from_6m_high_pct"),
                "path_tag": path.get("path_tag"),
                "near_6m_high": path.get("near_6m_high"),
                "capture_proxy_lo_pct": capture.get("capture_proxy_lo_pct"),
                "capture_proxy_hi_pct": capture.get("capture_proxy_hi_pct"),
                "capture_note_ru": capture.get("capture_note_ru"),
                "regime": snap.get("portfolio_trend_regime"),
                "ret_20d_pct": snap.get("portfolio_trend_ret_20d_pct"),
                "score_20d": snap.get("portfolio_ml_20d_entry_score"),
                "exp_20d_pct": exp20,
                "prospect_tier": tier,
                "prospect_priority": pri,
                "focus_action_ru": action,
            }
        )

    rows.sort(
        key=lambda r: (
            BUCKET_RANK.get(str(r.get("strategic_bucket")), 9),
            -float(r.get("allocation_score") or -999),
            -float(r.get("prospect_priority") or -999),
        )
    )
    for i, r in enumerate(rows, start=1):
        r["allocation_rank"] = i

    buckets: Dict[str, int] = {}
    for r in rows:
        b = str(r.get("strategic_bucket") or "n/a")
        buckets[b] = buckets.get(b, 0) + 1

    invest_first = [r for r in rows if r.get("strategic_bucket") in ("core_prospect", "watch_long")]
    headline = []
    for r in invest_first[:5]:
        lo, hi = r.get("capture_proxy_lo_pct"), r.get("capture_proxy_hi_pct")
        cap = f"{lo:.0f}…{hi:.0f}%" if lo is not None and hi is not None else "n/a"
        mm = r.get("market_move_6m_pct")
        mm_s = f"{mm:+.0f}%" if mm is not None else "n/a"
        headline.append(
            {
                "rank": r.get("allocation_rank"),
                "ticker": r.get("ticker"),
                "bucket": r.get("strategic_bucket"),
                "market_6m": mm_s,
                "capture_band": cap,
                "exp_20d_pct": r.get("exp_20d_pct"),
                "tier": r.get("prospect_tier"),
            }
        )

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "monthly_prospect_allocation",
        "lookback_trading_days": int(lookback_trading_days),
        "note_ru": (
            "Куда в портфельной игре ставить внимание первым и какой порядок прибыли "
            "светит: рынок ~6м / swing-capture band / форвард 20d. Графики ниже — "
            "глобальный взгляд; 20d prefer/avoid — тактика входа. Обновление ~раз в месяц."
        ),
        "columns_ru": {
            "market_move_6m_pct": "Движение рынка за lookback (не наша PnL)",
            "capture_proxy_lo_hi": "Ориентир % пути, который swing мог бы удержать",
            "exp_20d_pct": "Модельный ожидаемый return на ~20 торговых дней",
            "allocation_rank": "Приоритет фокуса капитала/внимания",
        },
        "bucket_counts": buckets,
        "invest_first": invest_first,
        "focus_tickers": invest_first,
        "avoid_or_weak": [
            r for r in rows if r.get("strategic_bucket") in ("tactical_avoid", "structurally_weak")
        ],
        "headline_top": headline,
        "tickers": rows,
        "tactical_snapshot": {
            "prospect_tier_counts": review.get("prospect_tier_counts"),
            "priority_top": review.get("priority_top"),
            "gate_mode": review.get("gate_mode"),
        },
    }


def write_monthly_review_artifact(payload: Dict[str, Any], path: Path | None = None) -> Path:
    out = path or default_monthly_review_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out
