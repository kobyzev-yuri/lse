#!/usr/bin/env python3
"""Walk-forward OOS multiday LR for GAME_5M tickers (v2 vs v3nm).

Writes: /app/logs/ml/ml_data_quality/last_multiday_wf_game5m.json
Usage: python scripts/run_multiday_wf_game5m.py  (~80 min on prod for 8 tickers)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

OUT_PATH = Path("/app/logs/ml/ml_data_quality/last_multiday_wf_game5m.json")


def main() -> int:
    from report_generator import get_engine
    from services.analyzer_ml_arbiter import (
        MULTIDAY_FEATURE_SET_SPECS,
        _multiday_walkforward_verdict,
        _pool_walkforward_runs,
        _ridge_lambda_from_config,
        _run_walkforward_for_feature_set,
    )
    from services.ticker_groups import get_tickers_game_5m

    t0 = time.time()
    eng = get_engine()
    tickers = get_tickers_game_5m()
    lam = _ridge_lambda_from_config()
    results: dict = {}
    log: list[str] = []
    for key in ("v2", "v3nm"):
        spec = MULTIDAY_FEATURE_SET_SPECS[key]
        pt = []
        for i, t in enumerate(tickers):
            msg = f"{key} {i + 1}/{len(tickers)} {t}"
            log.append(msg)
            print(msg, file=sys.stderr, flush=True)
            pt.extend(_run_walkforward_for_feature_set(eng, [t], lam, spec))
        results[key] = {"pool": _pool_walkforward_runs(pt), "per_ticker": pt}

    live = results["v3nm"]["pool"]
    verdict, rationale = _multiday_walkforward_verdict(
        live.get("pooled_rmse") or {},
        live.get("pooled_sign") or {},
        live.get("pooled_n") or {},
    )

    def slim(pt):
        out = []
        for one in pt:
            if one.get("mode") != "ok":
                out.append({"ticker": one.get("ticker"), "mode": one.get("mode")})
                continue
            ph = one.get("per_horizon") or {}
            row = {"ticker": one.get("ticker")}
            for h in ("1", "2", "3"):
                b = ph.get(h) or {}
                row[f"h{h}"] = {
                    "n": b.get("n_points"),
                    "rmse_log": b.get("rmse_oos_log"),
                    "sign": b.get("sign_accuracy"),
                    "mae_log": b.get("mae_oos_log"),
                }
            out.append(row)
        return out

    payload = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - t0, 1),
        "tickers": tickers,
        "ridge_lambda": lam,
        "live_feature_set": "v3nm",
        "verdict": verdict,
        "rationale_ru": rationale,
        "v2_pooled": results["v2"]["pool"].get("pooled_by_horizon"),
        "v3nm_pooled": results["v3nm"]["pool"].get("pooled_by_horizon"),
        "per_ticker_v3nm": slim(results["v3nm"]["per_ticker"]),
        "per_ticker_v2": slim(results["v2"]["per_ticker"]),
        "progress_log": log,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(OUT_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
