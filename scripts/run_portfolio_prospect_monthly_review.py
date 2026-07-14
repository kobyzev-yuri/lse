#!/usr/bin/env python3
"""
Monthly portfolio prospectivity review (~6m lookback + current 20d overlay).

Strategic reset: which names still look like portfolio-game candidates after
regime change. Complements nightly 20d retrain (tactical).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_out() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_portfolio_prospect_monthly_review.json")
    return project_root / "local" / "logs" / "ml_data_quality" / "last_portfolio_prospect_monthly_review.json"


def _lookback_return_pct(engine, ticker: str, *, trading_days: int = 126) -> Optional[float]:
    """~6 calendar months ≈ 126 trading days on daily quotes."""
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
    closes = []
    for r in reversed(rows):
        try:
            v = float(r[0])
            if v > 0 and math.isfinite(v):
                closes.append(v)
        except (TypeError, ValueError):
            continue
    if len(closes) < max(40, trading_days // 3):
        return None
    a, b = closes[0], closes[-1]
    if a <= 0:
        return None
    return round((b / a - 1.0) * 100.0, 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Monthly portfolio prospectivity review")
    ap.add_argument("--lookback-days", type=int, default=126, help="Trading days (~6m default)")
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    from report_generator import get_engine
    from services.portfolio_card import get_portfolio_trade_tickers
    from services.portfolio_trend_regime import build_portfolio_trend_regime_review

    eng = get_engine()
    tickers = list(get_portfolio_trade_tickers() or [])
    review = build_portfolio_trend_regime_review(tickers, engine=eng)
    by_t = {str(r.get("ticker") or "").upper(): r for r in (review.get("tickers") or [])}

    rows: List[Dict[str, Any]] = []
    for t in tickers:
        tu = t.strip().upper()
        snap = by_t.get(tu) or {}
        ret6 = _lookback_return_pct(eng, tu, trading_days=int(args.lookback_days))
        tier = snap.get("portfolio_prospect_tier") or "n/a"
        pri = snap.get("portfolio_prospect_priority")
        # Strategic label from ~6m path + current tier
        if ret6 is not None and ret6 >= 40 and tier == "prefer":
            strategic = "core_prospect"
        elif ret6 is not None and ret6 >= 25 and tier in ("prefer", "allow"):
            strategic = "watch_long"
        elif ret6 is not None and ret6 <= -20:
            strategic = "structurally_weak"
        elif tier == "avoid":
            strategic = "tactical_avoid"
        else:
            strategic = "neutral"
        rows.append(
            {
                "ticker": tu,
                "ret_approx_6m_pct": ret6,
                "regime": snap.get("portfolio_trend_regime"),
                "ret_20d_pct": snap.get("portfolio_trend_ret_20d_pct"),
                "score_20d": snap.get("portfolio_ml_20d_entry_score"),
                "exp_20d_pct": snap.get("portfolio_ml_20d_expected_return_pct"),
                "prospect_tier": tier,
                "prospect_priority": pri,
                "strategic_bucket": strategic,
            }
        )

    rows.sort(
        key=lambda r: (
            {"core_prospect": 0, "watch_long": 1, "neutral": 2, "tactical_avoid": 3, "structurally_weak": 4}.get(
                str(r.get("strategic_bucket")), 9
            ),
            -(float(r.get("prospect_priority") or -999)),
        )
    )
    buckets: Dict[str, int] = {}
    for r in rows:
        b = str(r.get("strategic_bucket") or "n/a")
        buckets[b] = buckets.get(b, 0) + 1

    payload: Dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "monthly_prospect_review",
        "lookback_trading_days": int(args.lookback_days),
        "note_ru": (
            "Стратегический пересмотр ~раз в месяц: 5–6м траектория (как на графиках) + "
            "текущий 20d prospect. Nightly 20d — тактика входа; этот отчёт — кого держать в фокусе игры."
        ),
        "bucket_counts": buckets,
        "focus_tickers": [r for r in rows if r.get("strategic_bucket") in ("core_prospect", "watch_long")],
        "avoid_or_weak": [r for r in rows if r.get("strategic_bucket") in ("tactical_avoid", "structurally_weak")],
        "tickers": rows,
        "tactical_snapshot": {
            "prospect_tier_counts": review.get("prospect_tier_counts"),
            "priority_top": review.get("priority_top"),
            "gate_mode": review.get("gate_mode"),
        },
    }
    out = Path(args.out).expanduser() if (args.out or "").strip() else _default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(
        "Wrote %s | buckets=%s focus=%s",
        out,
        buckets,
        [r["ticker"] for r in payload["focus_tickers"]],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
