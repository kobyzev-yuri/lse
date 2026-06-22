#!/usr/bin/env python3
"""
Train LSTM + tabular fusion for GAME_5M chart entry (Phase 2).

Chart OHLCV window → LSTM embedding; tabular E2/E3 vector → small MLP; concat → head.
No broadcast of context on chart timesteps (avoids Phase 1.5 LSTM regression).

  python scripts/build_game5m_chart_entry_dataset.py \\
    --bar-csv local/datasets/game5m_entry_bar_full.csv \\
    --no-context --fusion-tab e2 \\
    --out local/datasets/game5m_chart_entry_fusion_e2.npz

  python scripts/train_game5m_chart_entry_fusion.py \\
    --npz local/datasets/game5m_chart_entry_fusion_e2.npz \\
    --json-metrics-out local/datasets/game5m_chart_entry_fusion_e2_metrics.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _load_npz(path: Path) -> dict:
    data = np.load(path, allow_pickle=True)
    return {k: data[k] for k in data.files}


def _normalize_tab(X_tab: np.ndarray, train_m: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = X_tab[train_m].mean(axis=0)
    std = X_tab[train_m].std(axis=0)
    std = np.where(std < 1e-6, 1.0, std)
    return (X_tab - mean) / std, mean, std


def main() -> int:
    ap = argparse.ArgumentParser(description="Train LSTM+tabular fusion on chart entry NPZ")
    ap.add_argument("--npz", required=True)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--tab-hidden", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--json-metrics-out", type=str, default="")
    ap.add_argument("--init-lstm", type=str, default="", help="Checkpoint from train_game5m_chart_entry_lstm.py")
    ap.add_argument(
        "--freeze-lstm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Freeze LSTM after --init-lstm (default: True when init-lstm set)",
    )
    ap.add_argument("--fusion-mode", choices=("residual", "concat"), default="residual")
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        import torch
        from torch.utils.data import DataLoader, TensorDataset
    except ImportError:
        logger.error("torch not installed; pip install torch (CUDA wheel for GPU)")
        return 1

    from sklearn.metrics import roc_auc_score

    from services.game5m_chart_entry_fusion import build_entry_lstm_fusion_model, load_pretrained_lstm_into_fusion

    path = Path(args.npz).expanduser()
    if not path.is_file():
        logger.error("NPZ not found: %s", path)
        return 1

    raw = _load_npz(path)
    X = np.asarray(raw["X"], dtype=np.float32)
    X_tab = np.asarray(raw["X_tab"], dtype=np.float32)
    y = np.asarray(raw["y"], dtype=np.int64)
    splits = np.asarray(raw["split"])
    if X.ndim != 3 or X_tab.ndim != 2:
        logger.error("expected X (N,T,F) and X_tab (N,C); got %s %s", X.shape, X_tab.shape)
        return 1
    if len(X) != len(X_tab):
        logger.error("X / X_tab length mismatch %d vs %d", len(X), len(X_tab))
        return 1

    train_m = splits == "train"
    valid_m = splits == "valid"
    if train_m.sum() < 10 or valid_m.sum() < 5:
        logger.error("insufficient split sizes train=%s valid=%s", train_m.sum(), valid_m.sum())
        return 1

    X_tab, tab_mean, tab_std = _normalize_tab(X_tab, train_m)

    torch.manual_seed(int(args.seed))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    fusion_tab = str(raw.get("fusion_tab", "e2"))
    logger.info(
        "device=%s X=%s X_tab=%s fusion_tab=%s train=%d valid=%d",
        device,
        X.shape,
        X_tab.shape,
        fusion_tab,
        train_m.sum(),
        valid_m.sum(),
    )

    if args.dry_run:
        return 0

    model = build_entry_lstm_fusion_model(
        n_chart_features=int(X.shape[2]),
        n_tab_features=int(X_tab.shape[1]),
        hidden=int(args.hidden),
        tab_hidden=int(args.tab_hidden),
        dropout=float(args.dropout),
        fusion_mode=str(args.fusion_mode),  # type: ignore[arg-type]
    ).to(device)

    init_lstm = (args.init_lstm or "").strip()
    freeze_lstm = args.freeze_lstm if args.freeze_lstm is not None else bool(init_lstm)
    if init_lstm:
        load_pretrained_lstm_into_fusion(model, init_lstm, freeze=freeze_lstm)
        logger.info("init LSTM from %s freeze=%s", init_lstm, freeze_lstm)

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("trainable_params=%d", trainable)
    opt = torch.optim.Adam(model.parameters(), lr=float(args.lr))
    pos = float(y[train_m].sum())
    neg = float(train_m.sum() - pos)
    pos_weight = torch.tensor([neg / max(pos, 1.0)], device=device)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    train_ds = TensorDataset(
        torch.from_numpy(X[train_m]),
        torch.from_numpy(X_tab[train_m]),
        torch.from_numpy(y[train_m].astype(np.float32)),
    )
    train_dl = DataLoader(train_ds, batch_size=int(args.batch_size), shuffle=True)

    X_valid = torch.from_numpy(X[valid_m]).to(device)
    X_tab_valid = torch.from_numpy(X_tab[valid_m]).to(device)
    y_valid = y[valid_m]

    best_auc = float("nan")
    best_state = None
    for epoch in range(1, int(args.epochs) + 1):
        model.train()
        total_loss = 0.0
        n_batches = 0
        for xb, tb, yb in train_dl:
            xb = xb.to(device)
            tb = tb.to(device)
            yb = yb.to(device)
            opt.zero_grad()
            logits = model(xb, tb)
            loss = loss_fn(logits, yb)
            loss.backward()
            opt.step()
            total_loss += float(loss.item())
            n_batches += 1

        model.eval()
        with torch.no_grad():
            logits = model(X_valid, X_tab_valid)
            proba = torch.sigmoid(logits).cpu().numpy()
        try:
            auc = roc_auc_score(y_valid, proba) if len(set(y_valid.tolist())) > 1 else float("nan")
        except Exception:
            auc = float("nan")
        if auc == auc and (best_state is None or auc > best_auc):
            best_auc = auc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
        logger.info("epoch %d loss=%.4f auc_valid=%s", epoch, total_loss / max(n_batches, 1), f"{auc:.4f}" if auc == auc else "n/a")

    if best_state is not None:
        model.load_state_dict(best_state)

    out_ckpt = (args.out or "").strip()
    if not out_ckpt:
        out_ckpt = str(project_root / "local" / "models" / f"game5m_chart_entry_fusion_{fusion_tab}.pt")
    ckpt_path = Path(out_ckpt).expanduser()
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "n_chart_features": int(X.shape[2]),
            "n_tab_features": int(X_tab.shape[1]),
            "hidden": int(args.hidden),
            "tab_hidden": int(args.tab_hidden),
            "window_bars": int(raw.get("window_bars", X.shape[1])),
            "chart_feature_names": list(raw.get("feature_names", [])),
            "tab_feature_names": list(raw.get("tab_feature_names", [])),
            "fusion_tab": fusion_tab,
            "tab_mean": tab_mean.astype(np.float32),
            "tab_std": tab_std.astype(np.float32),
        },
        ckpt_path,
    )
    logger.info("saved checkpoint → %s", ckpt_path)

    metrics = {
        "script": "train_game5m_chart_entry_fusion",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "npz": str(path),
        "fusion_tab": fusion_tab,
        "fusion_mode": str(args.fusion_mode),
        "n_tab_features": int(X_tab.shape[1]),
        "device": str(device),
        "n_train": int(train_m.sum()),
        "n_valid": int(valid_m.sum()),
        "auc_valid": round(best_auc, 4) if best_auc == best_auc else None,
        "epochs": int(args.epochs),
        "hidden": int(args.hidden),
        "tab_hidden": int(args.tab_hidden),
        "dropout": float(args.dropout),
        "init_lstm": init_lstm or None,
        "freeze_lstm": freeze_lstm if init_lstm else None,
        "checkpoint": str(ckpt_path),
        "baseline_note": "Compare to lstm_chart_ohlcv ~0.616 and catboost E2 ~0.589 / E3 ~0.610",
    }
    metrics_path = (args.json_metrics_out or "").strip()
    if metrics_path:
        mp = Path(metrics_path).expanduser()
        mp.parent.mkdir(parents=True, exist_ok=True)
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        logger.info("metrics → %s", mp)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
