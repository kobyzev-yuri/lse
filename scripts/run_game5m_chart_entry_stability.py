#!/usr/bin/env python3
"""Multi-seed stability eval for chart entry LSTM/CNN (plan phase 3 prep)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> int:
    print("run:", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(project_root))


def main() -> int:
    ap = argparse.ArgumentParser(description="Run LSTM/CNN chart models across seeds")
    ap.add_argument("--npz", default="local/datasets/game5m_chart_entry_v1.npz")
    ap.add_argument("--seeds", default="42,43,44", help="Comma-separated seeds")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--models", default="lstm,cnn", help="lstm,cnn subset")
    ap.add_argument("--json-out", default="local/datasets/game5m_chart_entry_stability.json")
    args = ap.parse_args()

    npz = Path(args.npz).expanduser()
    if not npz.is_file():
        print(f"NPZ not found: {npz}", file=sys.stderr)
        return 1

    py = sys.executable
    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    models = {m.strip().lower() for m in args.models.split(",") if m.strip()}
    out_dir = project_root / "local" / "datasets"
    model_dir = project_root / "local" / "models"
    out_dir.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    results: dict = {
        "npz": str(npz),
        "seeds": seeds,
        "epochs": int(args.epochs),
        "runs": [],
        "summary": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    for seed in seeds:
        if "lstm" in models:
            metrics_path = out_dir / f"game5m_chart_entry_lstm_seed{seed}_metrics.json"
            ckpt = model_dir / f"game5m_chart_entry_lstm_seed{seed}.pt"
            rc = _run(
                [
                    py,
                    str(project_root / "scripts/train_game5m_chart_entry_lstm.py"),
                    "--npz",
                    str(npz),
                    "--seed",
                    str(seed),
                    "--epochs",
                    str(args.epochs),
                    "--json-metrics-out",
                    str(metrics_path),
                    "--out",
                    str(ckpt),
                ]
            )
            if rc == 0 and metrics_path.is_file():
                row = json.loads(metrics_path.read_text(encoding="utf-8"))
                row["model"] = "lstm"
                results["runs"].append(row)

        if "cnn" in models:
            metrics_path = out_dir / f"game5m_chart_entry_cnn_seed{seed}_metrics.json"
            ckpt = model_dir / f"game5m_chart_entry_cnn_seed{seed}.pt"
            rc = _run(
                [
                    py,
                    str(project_root / "scripts/train_game5m_chart_entry_cnn.py"),
                    "--npz",
                    str(npz),
                    "--seed",
                    str(seed),
                    "--epochs",
                    str(args.epochs),
                    "--json-metrics-out",
                    str(metrics_path),
                    "--out",
                    str(ckpt),
                ]
            )
            if rc == 0 and metrics_path.is_file():
                row = json.loads(metrics_path.read_text(encoding="utf-8"))
                row["model"] = "cnn"
                results["runs"].append(row)

    for model in ("lstm", "cnn"):
        aucs = [r["auc_valid"] for r in results["runs"] if r.get("model") == model and r.get("auc_valid") is not None]
        if not aucs:
            continue
        results["summary"][model] = {
            "n_runs": len(aucs),
            "auc_valid_min": round(min(aucs), 4),
            "auc_valid_max": round(max(aucs), 4),
            "auc_valid_mean": round(sum(aucs) / len(aucs), 4),
            "auc_valid_all": aucs,
        }

    catboost_path = out_dir / "game5m_entry_bar_v2_v1_metrics.json"
    if catboost_path.is_file():
        cb = json.loads(catboost_path.read_text(encoding="utf-8"))
        results["summary"]["catboost_v2"] = {"auc_valid": cb.get("auc_valid")}

    out_path = Path(args.json_out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["summary"], ensure_ascii=False, indent=2))
    print("wrote", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
