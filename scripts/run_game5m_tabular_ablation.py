#!/usr/bin/env python3
"""
Tabular CatBoost ablation for GAME_5M entry / hold classifiers.

Entry (cumulative): E0_T → E1_T_time → E2_T_time_KB → E3_full_TNC
Hold (cumulative):    H0_state → H1_entry → H2_exit_tech → H3_full

  python scripts/run_game5m_tabular_ablation.py \\
    --entry-csv local/datasets/game5m_entry_bar_full.csv \\
    --hold-csv local/datasets/game5m_hold_bar_dataset.csv \\
    --json-out local/datasets/game5m_tabular_ablation.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return default


def _time_split_indices(ts_list: list[str], *, valid_ratio: float) -> tuple[list[int], list[int]]:
    n = len(ts_list)
    order = sorted(range(n), key=lambda i: ts_list[i] or "")
    n_valid = max(1, int(n * float(valid_ratio)))
    n_train = n - n_valid
    train_idx = order[:n_train]
    valid_idx = order[n_train:]
    return train_idx, valid_idx


def _train_catboost_auc(
    rows: list[list[Any]],
    labels: list[int],
    ts_list: list[str],
    feature_names: list[str],
    *,
    valid_ratio: float = 0.2,
    seed: int = 42,
    cat_features: list[int] | None = None,
) -> dict[str, Any]:
    from catboost import CatBoostClassifier, Pool
    from sklearn.metrics import roc_auc_score

    n = len(rows)
    if n < 50 or len(labels) != n or len(ts_list) != n:
        return {"status": "insufficient_rows", "n_total": n}

    train_order, valid_order = _time_split_indices(ts_list, valid_ratio=valid_ratio)
    train_rows = [rows[i] for i in train_order]
    valid_rows = [rows[i] for i in valid_order]
    train_y = [labels[i] for i in train_order]
    valid_y = [labels[i] for i in valid_order]

    cat_idx = cat_features if cat_features is not None else [0]
    train_pool = Pool(train_rows, label=train_y, cat_features=cat_idx, feature_names=feature_names)
    valid_pool = Pool(valid_rows, label=valid_y, cat_features=cat_idx, feature_names=feature_names)

    pos = sum(train_y)
    neg = len(train_y) - pos
    model = CatBoostClassifier(
        iterations=400,
        depth=6,
        learning_rate=0.05,
        loss_function="Logloss",
        eval_metric="AUC",
        random_seed=int(seed),
        verbose=False,
        scale_pos_weight=neg / max(pos, 1),
    )
    model.fit(train_pool, eval_set=valid_pool, use_best_model=True)
    proba = model.predict_proba(valid_pool)[:, 1]
    try:
        auc = float(roc_auc_score(valid_y, proba)) if len(set(valid_y)) > 1 else float("nan")
    except Exception:
        auc = float("nan")

    return {
        "status": "ok",
        "n_total": n,
        "n_train": len(train_rows),
        "n_valid": len(valid_rows),
        "y_pos_rate": round(sum(labels) / n, 4) if n else 0.0,
        "auc_valid": round(auc, 4) if auc == auc else None,
        "n_features": len(feature_names) - 1,
    }


def _build_entry_rows(raw_rows: list[dict[str, Any]], keys: tuple[str, ...]) -> tuple[list[list[Any]], list[int], list[str]]:
    rows: list[list[Any]] = []
    labels: list[int] = []
    ts_list: list[str] = []
    for raw in raw_rows:
        if str(raw.get("tb_label") or "") == "insufficient_data":
            continue
        try:
            y = int(float(raw.get("y_entry_good") or 0))
        except (TypeError, ValueError):
            continue
        ticker = str(raw.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        rows.append([ticker] + [_safe_float(raw.get(k)) for k in keys])
        labels.append(y)
        ts_list.append(str(raw.get("bar_ts_et") or ""))
    return rows, labels, ts_list


def _build_hold_rows(
    raw_rows: list[dict[str, Any]],
    keys: tuple[str, ...],
    *,
    recovery: bool = False,
) -> tuple[list[list[Any]], list[int], list[str]]:
    from services.game5m_hold_bar_dataset import y_hold_good_from_row

    rows: list[list[Any]] = []
    labels: list[int] = []
    ts_list: list[str] = []
    for raw in raw_rows:
        y = y_hold_good_from_row(raw)
        if y is None:
            continue
        ticker = str(raw.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        if recovery:
            ed = raw.get("entry_decision")
            ed_s = (str(ed).strip()[:64] if ed is not None else "") or "—"
            rows.append([ticker] + [_safe_float(raw.get(k)) for k in keys] + [ed_s])
        else:
            rows.append([ticker] + [_safe_float(raw.get(k)) for k in keys])
        labels.append(int(y))
        ts_list.append(str(raw.get("bar_ts_et") or ""))
    return rows, labels, ts_list


def _run_entry_tracks(
    raw_rows: list[dict[str, Any]],
    tracks: dict[str, tuple[str, ...]],
    *,
    valid_ratio: float,
    seed: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for track_id, keys in tracks.items():
        rows, labels, ts_list = _build_entry_rows(raw_rows, keys)
        feat_names = ["ticker"] + list(keys)
        logger.info("entry %s: n=%d feat=%d", track_id, len(rows), len(keys))
        out[track_id] = _train_catboost_auc(
            rows, labels, ts_list, feat_names, valid_ratio=valid_ratio, seed=seed,
        )
    return out


def _run_hold_tracks(
    raw_rows: list[dict[str, Any]],
    tracks: dict[str, tuple[str, ...]],
    *,
    valid_ratio: float,
    seed: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for track_id, keys in tracks.items():
        recovery = track_id == "H_recovery_B1"
        rows, labels, ts_list = _build_hold_rows(raw_rows, keys, recovery=recovery)
        feat_names = ["ticker"] + list(keys)
        if recovery:
            feat_names = feat_names + ["entry_decision"]
        logger.info("hold %s: n=%d feat=%d", track_id, len(rows), len(keys))
        cat_features = [0, len(feat_names) - 1] if recovery else [0]
        out[track_id] = _train_catboost_auc(
            rows,
            labels,
            ts_list,
            feat_names,
            valid_ratio=valid_ratio,
            seed=seed,
            cat_features=cat_features,
        )
    return out


def _auc_deltas(tracks_out: dict[str, Any], ordered_ids: list[str]) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for i in range(1, len(ordered_ids)):
        prev, cur = ordered_ids[i - 1], ordered_ids[i]
        a0 = (tracks_out.get(prev) or {}).get("auc_valid")
        a1 = (tracks_out.get(cur) or {}).get("auc_valid")
        if a0 is not None and a1 is not None:
            deltas[f"{prev}_to_{cur}"] = round(float(a1) - float(a0), 4)
    return deltas


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser(description="Tabular CatBoost ablation (entry + hold)")
    ap.add_argument("--entry-csv", default="local/datasets/game5m_entry_bar_full.csv")
    ap.add_argument("--hold-csv", default="local/datasets/game5m_hold_bar_dataset.csv")
    ap.add_argument("--json-out", default="local/datasets/game5m_tabular_ablation.json")
    ap.add_argument("--valid-ratio", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--entry-only", action="store_true")
    ap.add_argument("--hold-only", action="store_true")
    ap.add_argument("--isolated", action="store_true", help="Also run entry isolated layer tracks")
    args = ap.parse_args()

    try:
        import catboost  # noqa: F401
    except ImportError:
        logger.error("pip install -r requirements-catboost.txt")
        return 1

    from services.game5m_tabular_ablation import (
        ENTRY_ABLATION_ISOLATED,
        ENTRY_ABLATION_TRACKS,
        HOLD_ABLATION_TRACKS,
        HOLD_RECOVERY_KEYS,
        ablation_track_descriptions,
    )

    payload: dict[str, Any] = {
        "script": "run_game5m_tabular_ablation",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "valid_ratio": float(args.valid_ratio),
        "seed": int(args.seed),
        "track_descriptions": ablation_track_descriptions(),
    }

    entry_order = list(ENTRY_ABLATION_TRACKS.keys())
    hold_order = list(HOLD_ABLATION_TRACKS.keys()) + ["H_recovery_B1"]

    if not args.hold_only:
        entry_path = Path(args.entry_csv).expanduser()
        if not entry_path.is_file():
            logger.error("entry CSV not found: %s", entry_path)
            return 1
        tracks = dict(ENTRY_ABLATION_TRACKS)
        if args.isolated:
            tracks.update(ENTRY_ABLATION_ISOLATED)
        payload["entry_csv"] = str(entry_path)
        payload["entry"] = _run_entry_tracks(
            _load_csv(entry_path), tracks, valid_ratio=float(args.valid_ratio), seed=int(args.seed),
        )
        payload["entry_auc_deltas_pp"] = _auc_deltas(payload["entry"], entry_order)

    if not args.entry_only:
        hold_path = Path(args.hold_csv).expanduser()
        if not hold_path.is_file():
            logger.error("hold CSV not found: %s", hold_path)
            return 1
        hold_tracks = dict(HOLD_ABLATION_TRACKS)
        hold_tracks["H_recovery_B1"] = HOLD_RECOVERY_KEYS
        payload["hold_csv"] = str(hold_path)
        payload["hold"] = _run_hold_tracks(
            _load_csv(hold_path), hold_tracks, valid_ratio=float(args.valid_ratio), seed=int(args.seed),
        )
        payload["hold_auc_deltas_pp"] = _auc_deltas(payload["hold"], hold_order)

    out_path = Path(args.json_out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("wrote %s", out_path)

    for section, order in (("entry", entry_order), ("hold", hold_order)):
        data = payload.get(section)
        if not data:
            continue
        logger.info("=== %s ===", section.upper())
        for tid in order:
            meta = data.get(tid) or {}
            if meta.get("status") == "ok":
                logger.info("  %s: AUC=%s", tid, meta.get("auc_valid"))
        if args.isolated and section == "entry":
            for tid in ENTRY_ABLATION_ISOLATED:
                meta = data.get(tid) or {}
                if meta.get("status") == "ok":
                    logger.info("  %s: AUC=%s (isolated)", tid, meta.get("auc_valid"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
