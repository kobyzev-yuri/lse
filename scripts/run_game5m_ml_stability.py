#!/usr/bin/env python3
"""
Multi-seed stability for best GAME_5M ML tracks (Phase 3).

Runs 3 seeds (default 42,43,44) for:
  - Tabular CatBoost entry E3_full_TNC
  - Tabular CatBoost hold H3_full
  - Chart fusion E3 residual (LSTM OHLCV + tab NC, frozen init per seed)

  python scripts/run_game5m_ml_stability.py \\
    --json-out local/datasets/game5m_ml_stability.json
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _summary(aucs: list[float]) -> dict[str, Any]:
    if not aucs:
        return {"n_runs": 0}
    return {
        "n_runs": len(aucs),
        "auc_valid_min": round(min(aucs), 4),
        "auc_valid_max": round(max(aucs), 4),
        "auc_valid_mean": round(sum(aucs) / len(aucs), 4),
        "auc_valid_std": round((sum((x - sum(aucs) / len(aucs)) ** 2 for x in aucs) / len(aucs)) ** 0.5, 4),
        "auc_valid_all": [round(x, 4) for x in aucs],
    }


def _run_cmd(cmd: list[str]) -> int:
    logger.info("run: %s", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(project_root))


def _tabular_stability(
    *,
    py: str,
    entry_csv: Path,
    hold_csv: Path,
    seeds: list[int],
    valid_ratio: float,
) -> dict[str, Any]:
    from services.game5m_tabular_ablation import ENTRY_ABLATION_TRACKS, HOLD_ABLATION_TRACKS
    from scripts.run_game5m_tabular_ablation import (
        _build_entry_rows,
        _build_hold_rows,
        _load_csv,
        _train_catboost_auc,
    )

    entry_raw = _load_csv(entry_csv)
    hold_raw = _load_csv(hold_csv)
    e3_keys = ENTRY_ABLATION_TRACKS["E3_full_TNC"]
    h3_keys = HOLD_ABLATION_TRACKS["H3_full"]
    e_rows, e_labels, e_ts = _build_entry_rows(entry_raw, e3_keys)
    h_rows, h_labels, h_ts = _build_hold_rows(hold_raw, h3_keys, recovery=False)
    e_feat = ["ticker"] + list(e3_keys)
    h_feat = ["ticker"] + list(h3_keys)

    out: dict[str, Any] = {"runs": [], "summary": {}}
    e3_aucs: list[float] = []
    h3_aucs: list[float] = []

    for seed in seeds:
        e_meta = _train_catboost_auc(
            e_rows, e_labels, e_ts, e_feat, valid_ratio=valid_ratio, seed=seed,
        )
        e_meta["track"] = "E3_full_TNC"
        e_meta["seed"] = seed
        e_meta["model"] = "catboost_entry"
        out["runs"].append(e_meta)
        if e_meta.get("auc_valid") is not None:
            e3_aucs.append(float(e_meta["auc_valid"]))

        h_meta = _train_catboost_auc(
            h_rows, h_labels, h_ts, h_feat, valid_ratio=valid_ratio, seed=seed, cat_features=[0],
        )
        h_meta["track"] = "H3_full"
        h_meta["seed"] = seed
        h_meta["model"] = "catboost_hold"
        out["runs"].append(h_meta)
        if h_meta.get("auc_valid") is not None:
            h3_aucs.append(float(h_meta["auc_valid"]))

    out["summary"]["E3_full_TNC"] = _summary(e3_aucs)
    out["summary"]["H3_full"] = _summary(h3_aucs)
    return out


def _fusion_stability(
    *,
    py: str,
    fusion_npz: Path,
    seeds: list[int],
    epochs: int,
    out_dir: Path,
    model_dir: Path,
) -> dict[str, Any]:
    out: dict[str, Any] = {"runs": [], "summary": {}}
    aucs: list[float] = []

    for seed in seeds:
        lstm_ckpt = model_dir / f"game5m_chart_entry_lstm_fusion_e3_seed{seed}.pt"
        lstm_metrics = out_dir / f"game5m_chart_entry_lstm_fusion_e3_seed{seed}_metrics.json"
        rc = _run_cmd(
            [
                py,
                str(project_root / "scripts/train_game5m_chart_entry_lstm.py"),
                "--npz",
                str(fusion_npz),
                "--seed",
                str(seed),
                "--epochs",
                str(epochs),
                "--out",
                str(lstm_ckpt),
                "--json-metrics-out",
                str(lstm_metrics),
            ]
        )
        if rc != 0 or not lstm_ckpt.is_file():
            logger.warning("LSTM init seed=%s failed rc=%s", seed, rc)
            continue

        fusion_metrics = out_dir / f"game5m_chart_entry_fusion_e3_residual_seed{seed}_metrics.json"
        fusion_ckpt = model_dir / f"game5m_chart_entry_fusion_e3_residual_seed{seed}.pt"
        rc = _run_cmd(
            [
                py,
                str(project_root / "scripts/train_game5m_chart_entry_fusion.py"),
                "--npz",
                str(fusion_npz),
                "--seed",
                str(seed),
                "--epochs",
                str(epochs),
                "--init-lstm",
                str(lstm_ckpt),
                "--fusion-mode",
                "residual",
                "--out",
                str(fusion_ckpt),
                "--json-metrics-out",
                str(fusion_metrics),
            ]
        )
        if rc == 0 and fusion_metrics.is_file():
            row = json.loads(fusion_metrics.read_text(encoding="utf-8"))
            row["model"] = "fusion_e3_residual"
            row["seed"] = seed
            out["runs"].append(row)
            if row.get("auc_valid") is not None:
                aucs.append(float(row["auc_valid"]))

    out["summary"]["fusion_e3_residual"] = _summary(aucs)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="3-seed stability for E3 entry / H3 hold / E3 fusion")
    ap.add_argument("--entry-csv", default="local/datasets/game5m_entry_bar_full.csv")
    ap.add_argument("--hold-csv", default="local/datasets/game5m_hold_bar_dataset.csv")
    ap.add_argument("--fusion-npz", default="local/datasets/game5m_chart_entry_fusion_e3.npz")
    ap.add_argument("--seeds", default="42,43,44")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--valid-ratio", type=float, default=0.2)
    ap.add_argument("--skip-tabular", action="store_true")
    ap.add_argument("--skip-fusion", action="store_true")
    ap.add_argument("--json-out", default="local/datasets/game5m_ml_stability.json")
    ap.add_argument("--python", default=sys.executable, help="Python with catboost+torch (py12)")
    args = ap.parse_args()

    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    py = str(args.python)
    out_dir = project_root / "local" / "datasets"
    model_dir = project_root / "local" / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "script": "run_game5m_ml_stability",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seeds": seeds,
        "epochs": int(args.epochs),
        "valid_ratio": float(args.valid_ratio),
        "entry_csv": str(args.entry_csv),
        "hold_csv": str(args.hold_csv),
        "fusion_npz": str(args.fusion_npz),
    }

    if not args.skip_tabular:
        try:
            import catboost  # noqa: F401
        except ImportError:
            logger.error("catboost missing; use --python pointing to env with catboost")
            return 1
        payload["tabular"] = _tabular_stability(
            py=py,
            entry_csv=Path(args.entry_csv).expanduser(),
            hold_csv=Path(args.hold_csv).expanduser(),
            seeds=seeds,
            valid_ratio=float(args.valid_ratio),
        )

    fusion_npz = Path(args.fusion_npz).expanduser()
    if not args.skip_fusion:
        if not fusion_npz.is_file():
            logger.error("fusion NPZ not found: %s", fusion_npz)
            return 1
        payload["fusion"] = _fusion_stability(
            py=py,
            fusion_npz=fusion_npz,
            seeds=seeds,
            epochs=int(args.epochs),
            out_dir=out_dir,
            model_dir=model_dir,
        )

    out_path = Path(args.json_out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s", out_path)

    for section in ("tabular", "fusion"):
        block = payload.get(section, {}).get("summary", {})
        if block:
            logger.info("=== %s ===", section.upper())
            for name, stats in block.items():
                logger.info(
                    "  %s: mean=%s min=%s max=%s std=%s",
                    name,
                    stats.get("auc_valid_mean"),
                    stats.get("auc_valid_min"),
                    stats.get("auc_valid_max"),
                    stats.get("auc_valid_std"),
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
