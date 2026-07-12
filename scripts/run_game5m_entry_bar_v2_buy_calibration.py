#!/usr/bin/env python3
"""
BUY-only bar v2 retrain + Platt calibration + closed-trade backtest report.

End-to-end calibration workflow (2026-07):
  1. Build / reuse bar dataset
  2. Train CatBoost v2 on BUY/STRONG_BUY bars only
  3. Fit Platt calibrator on valid split; evaluate gates
  4. Write bar_v2_calibration_report.json (live replay on recent closed trades)

  python scripts/run_game5m_entry_bar_v2_buy_calibration.py --full
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, cwd: Path) -> int:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd))


def _ml_base_out() -> Path:
    if os.environ.get("DAILY_ML_OUT_DIR"):
        return Path(os.environ["DAILY_ML_OUT_DIR"])
    if Path("/app/logs").exists():
        return Path("/app/logs/ml")
    return project_root / "local" / "logs" / "ml"


def _env_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        from config_loader import get_config_value

        raw = (get_config_value(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def _closed_trade_backtest(*, days: int, hold_below_p: float) -> dict:
    from services.deal_params_5m import normalize_entry_context
    from services.catboost_5m_signal import attach_catboost_signal, finalize_technical_decision_with_catboost
    from services.trade_effectiveness_analyzer import _load_closed_trades
    from config_loader import get_config_value

    closed = _load_closed_trades(days, "GAME_5M")
    rows: list[dict] = []
    for t in closed:
        ctx = normalize_entry_context(getattr(t, "context_json", None))
        if not ctx:
            continue
        core = str(ctx.get("technical_decision_core") or ctx.get("decision") or "").upper()
        if core not in ("BUY", "STRONG_BUY"):
            continue
        d5 = dict(ctx)
        d5["decision"] = core
        attach_catboost_signal(d5, t.ticker)
        finalize_technical_decision_with_catboost(d5)
        p = d5.get("catboost_entry_proba_good")
        p_raw = d5.get("catboost_entry_proba_good_raw")
        eff = d5.get("technical_decision_effective")
        pnl = getattr(t, "net_pnl_pct", None)
        rows.append(
            {
                "trade_id": getattr(t, "trade_id", None),
                "ticker": t.ticker,
                "entry_ts": str(getattr(t, "entry_ts", "")),
                "core": core,
                "effective": eff,
                "p_fusion": float(p) if p is not None else None,
                "p_raw": float(p_raw) if p_raw is not None else None,
                "would_hold": eff == "HOLD",
                "net_pnl_pct": float(pnl) if pnl is not None else None,
            }
        )

    ps = [r["p_fusion"] for r in rows if r["p_fusion"] is not None]
    would_hold = [r for r in rows if r["would_hold"]]
    wins = [r for r in rows if (r.get("net_pnl_pct") or 0) > 0]
    blocked_wins = [r for r in would_hold if (r.get("net_pnl_pct") or 0) > 0]
    fusion_mode = (get_config_value("GAME_5M_CATBOOST_FUSION", "none") or "none").strip()

    return {
        "days": days,
        "hold_below_p": hold_below_p,
        "fusion_mode_config": fusion_mode,
        "n_buy_trades": len(rows),
        "n_would_hold": len(would_hold),
        "n_wins_total": len(wins),
        "n_wins_blocked": len(blocked_wins),
        "p_fusion_std": round(pstdev(ps), 6) if len(ps) >= 2 else 0.0,
        "p_fusion_min": round(min(ps), 4) if ps else None,
        "p_fusion_max": round(max(ps), 4) if ps else None,
        "p_fusion_mean": round(mean(ps), 4) if ps else None,
        "trades": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="BUY-only bar v2 retrain + calibration report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true", help="Force dataset build + train write")
    ap.add_argument("--skip-datasets", action="store_true")
    ap.add_argument("--skip-train", action="store_true")
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--backtest-days", type=int, default=30)
    args = ap.parse_args()

    py = sys.executable
    base_out = _ml_base_out()
    datasets_dir = base_out / "datasets"
    models_dir = base_out / "models"
    q_dir = base_out / "ml_data_quality" if (base_out / "ml_data_quality").exists() else base_out.parent / "ml_data_quality"
    if not q_dir.is_dir():
        q_dir = base_out / "ml_data_quality"
    q_dir.mkdir(parents=True, exist_ok=True)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    csv_out = datasets_dir / "game5m_entry_bar_dataset.csv"
    stats_out = datasets_dir / "game5m_entry_bar_dataset_stats.json"
    cb_out = models_dir / "game5m_entry_catboost_v2.cbm"
    metrics_path = q_dir / "last_game5m_entry_bar_v2_train_metrics.json"
    report_path = q_dir / "bar_v2_calibration_report.json"
    days = _env_int("GAME_5M_ENTRY_BAR_BUILD_DAYS", 90)

    do_write = args.full and not args.dry_run
    build_rc = 0
    if not args.skip_datasets and do_write:
        build_cmd = [
            py,
            str(project_root / "scripts/build_game5m_entry_bar_dataset.py"),
            "--days",
            str(days),
            "--source",
            "db",
            "--out",
            str(csv_out),
            "--summary-json",
            str(stats_out),
        ]
        build_rc = _run(build_cmd, cwd=project_root)
        if build_rc != 0:
            logger.error("dataset build failed rc=%s", build_rc)
            return build_rc

    train_rc = 0
    train_metrics: dict = {}
    if not args.skip_train and csv_out.is_file():
        train_cmd = [
            py,
            str(project_root / "scripts/train_game5m_catboost.py"),
            "--dataset",
            "bar",
            "--bar-csv",
            str(csv_out),
            "--out",
            str(cb_out),
            "--train-population",
            "buy_only",
            "--calibrate",
            "--json-metrics-out",
            str(metrics_path),
        ]
        if not do_write:
            train_cmd.append("--dry-run")
        train_rc = _run(train_cmd, cwd=project_root)
        if metrics_path.is_file():
            try:
                train_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("read train metrics: %s", e)

    backtest: dict = {}
    if not args.skip_backtest and not args.dry_run and cb_out.is_file():
        try:
            from config_loader import get_config_value

            hold_p = float((get_config_value("GAME_5M_CATBOOST_HOLD_BELOW_P", "0.50") or "0.50").strip())
            backtest = _closed_trade_backtest(days=args.backtest_days, hold_below_p=hold_p)
        except Exception as e:
            logger.warning("closed trade backtest: %s", e)
            backtest = {"error": str(e)}

    meta: dict = {}
    meta_path = cb_out.with_suffix(".meta.json")
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("read meta: %s", e)

    calibration = meta.get("calibration") if isinstance(meta.get("calibration"), dict) else {}
    fusion_ready = bool(meta.get("fusion_calibration_ready") or calibration.get("fusion_calibration_ready"))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workflow": "buy_only_retrain_calibration_v1",
        "dataset_csv": str(csv_out),
        "model_path": str(cb_out),
        "train_population": meta.get("train_population") or "buy_only",
        "train_metrics": train_metrics,
        "calibration": calibration,
        "fusion_calibration_ready": fusion_ready,
        "recommendation": (
            "fusion_ok_use_calibrated_p"
            if fusion_ready
            else "keep_fusion_none_until_gates_green"
        ),
        "closed_trade_backtest": backtest,
        "build_rc": build_rc,
        "train_rc": train_rc,
    }
    if not args.dry_run:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Wrote calibration report: %s", report_path)

    logger.info(
        "Calibration done: fusion_ready=%s train_rc=%s report=%s",
        fusion_ready,
        train_rc,
        report_path,
    )
    return 0 if train_rc in (0, 2) else train_rc


if __name__ == "__main__":
    raise SystemExit(main())
