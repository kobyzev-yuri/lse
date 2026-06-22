#!/usr/bin/env python3
"""
Offline policy counterfactual for entry E3 / hold H3 CatBoost (valid fold).

Entry: BUY candidates with core=BUY — if P(y_entry_good) < τ → would skip entry.
Hold: rows with y_hold_good — if P(y_hold_good) >= τ → would defer exit (proxy: label flip benefit).

  python scripts/eval_game5m_ml_policy_backtest.py \\
    --entry-csv local/datasets/game5m_entry_bar_full.csv \\
    --hold-csv local/datasets/game5m_hold_bar_dataset.csv \\
    --json-out local/datasets/game5m_ml_policy_backtest.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _time_split(n: int, ts: list[str], valid_ratio: float) -> tuple[list[int], list[int]]:
    order = sorted(range(n), key=lambda i: ts[i] or "")
    n_valid = max(1, int(n * valid_ratio))
    n_train = n - n_valid
    train_idx = order[:n_train]
    valid_idx = order[n_train:]
    return train_idx, valid_idx


def _load_catboost_bundle(path: Path) -> tuple[Any, dict]:
    from catboost import CatBoostClassifier

    meta_path = path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    model = CatBoostClassifier()
    model.load_model(str(path))
    return model, meta


def _predict_proba(model: Any, meta: dict, rows: list[list[Any]]) -> list[float]:
    from catboost import Pool

    pool = Pool(rows, cat_features=meta.get("cat_feature_indices", [0]), feature_names=meta["feature_names"])
    proba = model.predict_proba(pool)[:, 1]
    return [float(x) for x in proba]


def _eval_entry_policy(
    csv_path: Path,
    model_path: Path,
    *,
    valid_ratio: float,
    taus: list[float],
) -> dict[str, Any]:
    from services.game5m_entry_bar_dataset import get_bar_train_feature_schema, row_from_bar_dataset_dict

    raw_rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))
    rows = []
    labels = []
    ts_list = []
    kinds = []
    for raw in raw_rows:
        if str(raw.get("tb_label") or "") == "insufficient_data":
            continue
        rows.append(row_from_bar_dataset_dict(raw, mode="full"))
        labels.append(int(float(raw.get("y_entry_good") or 0)))
        ts_list.append(str(raw.get("bar_ts_et") or ""))
        kinds.append(str(raw.get("sample_kind") or ""))

    n = len(rows)
    _, valid_idx = _time_split(n, ts_list, valid_ratio)
    model, meta = _load_catboost_bundle(model_path)
    proba = _predict_proba(model, meta, rows)

    out: dict[str, Any] = {"n_valid": len(valid_idx), "taus": {}}
    for tau in taus:
        kept = 0
        good_kept = 0
        bad_blocked = 0
        good_blocked = 0
        for i in valid_idx:
            p = proba[i]
            would_enter = p >= tau
            y = labels[i]
            if would_enter:
                kept += 1
                if y == 1:
                    good_kept += 1
            else:
                if y == 0:
                    bad_blocked += 1
                else:
                    good_blocked += 1
        out["taus"][str(tau)] = {
            "would_enter_rate": round(kept / max(len(valid_idx), 1), 4),
            "precision_among_entered": round(good_kept / max(kept, 1), 4),
            "bad_blocked_rate": round(bad_blocked / max(sum(1 for i in valid_idx if labels[i] == 0), 1), 4),
            "good_blocked": good_blocked,
        }
    return out


def _eval_hold_policy(
    csv_path: Path,
    model_path: Path,
    *,
    valid_ratio: float,
    taus: list[float],
) -> dict[str, Any]:
    from services.game5m_hold_bar_dataset import row_from_hold_bar_dict, y_hold_good_from_row

    raw_rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))
    rows = []
    labels = []
    ts_list = []
    for raw in raw_rows:
        y = y_hold_good_from_row(raw)
        if y is None:
            continue
        rows.append(row_from_hold_bar_dict(raw, mode="full"))
        labels.append(int(y))
        ts_list.append(str(raw.get("bar_ts_et") or ""))

    n = len(rows)
    _, valid_idx = _time_split(n, ts_list, valid_ratio)
    model, meta = _load_catboost_bundle(model_path)
    proba = _predict_proba(model, meta, rows)

    out: dict[str, Any] = {"n_valid": len(valid_idx), "taus": {}}
    for tau in taus:
        defer = 0
        good_defer = 0
        bad_defer = 0
        for i in valid_idx:
            if proba[i] >= tau:
                defer += 1
                if labels[i] == 1:
                    good_defer += 1
                else:
                    bad_defer += 1
        out["taus"][str(tau)] = {
            "defer_rate": round(defer / max(len(valid_idx), 1), 4),
            "precision_among_deferred": round(good_defer / max(defer, 1), 4),
            "bad_defer": bad_defer,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Policy counterfactual for E3 entry / H3 hold")
    ap.add_argument("--entry-csv", default="local/datasets/game5m_entry_bar_full.csv")
    ap.add_argument("--hold-csv", default="local/datasets/game5m_hold_bar_dataset.csv")
    ap.add_argument("--entry-model", default="local/models/game5m_entry_catboost_e3.cbm")
    ap.add_argument("--hold-model", default="local/models/game5m_hold_bar_catboost_h3.cbm")
    ap.add_argument("--valid-ratio", type=float, default=0.2)
    ap.add_argument("--taus", default="0.45,0.5,0.55,0.6")
    ap.add_argument("--json-out", default="local/datasets/game5m_ml_policy_backtest.json")
    args = ap.parse_args()

    taus = [float(x.strip()) for x in args.taus.split(",") if x.strip()]
    payload: dict[str, Any] = {
        "script": "eval_game5m_ml_policy_backtest",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_ratio": float(args.valid_ratio),
        "taus": taus,
    }

    entry_model = Path(args.entry_model).expanduser()
    hold_model = Path(args.hold_model).expanduser()
    entry_csv = Path(args.entry_csv).expanduser()
    hold_csv = Path(args.hold_csv).expanduser()

    if entry_model.is_file() and entry_csv.is_file():
        payload["entry_e3"] = _eval_entry_policy(
            entry_csv, entry_model, valid_ratio=float(args.valid_ratio), taus=taus,
        )
    else:
        logger.warning("skip entry policy: model=%s csv=%s", entry_model, entry_csv)

    if hold_model.is_file() and hold_csv.is_file():
        payload["hold_h3"] = _eval_hold_policy(
            hold_csv, hold_model, valid_ratio=float(args.valid_ratio), taus=taus,
        )
    else:
        logger.warning("skip hold policy: model=%s csv=%s", hold_model, hold_csv)

    out_path = Path(args.json_out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
