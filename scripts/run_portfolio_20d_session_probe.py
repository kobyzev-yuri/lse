#!/usr/bin/env python3
"""
Session probe: portfolio rule regime + CatBoost 20d scores for open watchlist.

Writes JSON under ml_data_quality for analyzer / ops to watch game impact (log_only).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_out() -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_portfolio_20d_session_probe.json")
    return project_root / "local" / "logs" / "ml_data_quality" / "last_portfolio_20d_session_probe.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Portfolio 20d session probe")
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    from services.portfolio_card import get_portfolio_trade_tickers
    from services.portfolio_trend_regime import build_portfolio_trend_regime_review

    tickers: List[str] = list(get_portfolio_trade_tickers() or [])
    review = build_portfolio_trend_regime_review(tickers)
    rows = review.get("tickers") or []
    strong = [
        {
            "ticker": r.get("ticker"),
            "regime": r.get("portfolio_trend_regime"),
            "ret_20d_pct": r.get("portfolio_trend_ret_20d_pct"),
            "exp_20d_pct": r.get("portfolio_ml_20d_expected_return_pct"),
            "score": r.get("portfolio_ml_20d_entry_score"),
            "hint": r.get("portfolio_ml_20d_regime_hint"),
        }
        for r in rows
        if (r.get("portfolio_ml_20d_status") == "ok")
        and (
            (r.get("portfolio_ml_20d_entry_score") is not None and float(r["portfolio_ml_20d_entry_score"]) >= 58)
            or (r.get("portfolio_ml_20d_entry_score") is not None and float(r["portfolio_ml_20d_entry_score"]) <= 45)
        )
    ]
    payload: Dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "log_only_observe",
        "n_tickers": len(rows),
        "ml_20d_ok_count": review.get("ml_20d_ok_count"),
        "regime_counts": review.get("regime_counts"),
        "regime_hint_counts": review.get("regime_hint_counts"),
        "extreme_score_rows": strong,
        "tickers": rows,
        "note_ru": (
            "Shadow probe: 20d CatBoost не блокирует BUY и не меняет trailing. "
            "Смотреть extreme_score_rows vs фактические portfolio entries/exits в analyzer."
        ),
    }
    out = Path(args.out).expanduser() if (args.out or "").strip() else _default_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.info(
        "Wrote %s | ml_ok=%s extremes=%s regimes=%s",
        out,
        payload.get("ml_20d_ok_count"),
        len(strong),
        payload.get("regime_counts"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
