#!/usr/bin/env python3
"""
LSTM baseline for GAME_5M chart entry research (phase 1.3, local GPU).

  python scripts/train_game5m_chart_entry_lstm.py \\
    --npz local/datasets/game5m_chart_entry_v1.npz \\
    --json-metrics-out local/datasets/game5m_chart_entry_lstm_metrics.json

Requires torch (CUDA if available). Not deployed to prod cron.
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Train LSTM on chart entry NPZ")
    ap.add_argument("--npz", required=True, help="Chart dataset NPZ from build_game5m_chart_entry_dataset.py")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--hidden", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="", help="Checkpoint .pt path")
    ap.add_argument("--json-metrics-out", type=str, default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        import torch
        import torch.nn as nn
        from torch.utils.data import DataLoader, TensorDataset
    except ImportError:
        logger.error("torch not installed; pip install torch (CUDA wheel for GPU)")
        return 1

    from sklearn.metrics import roc_auc_score

    path = Path(args.npz).expanduser()
    if not path.is_file():
        logger.error("NPZ not found: %s", path)
        return 1

    raw = _load_npz(path)
    X = np.asarray(raw["X"], dtype=np.float32)
    y = np.asarray(raw["y"], dtype=np.int64)
    splits = np.asarray(raw["split"])
    if X.ndim != 3:
        logger.error("expected X (N,T,F), got %s", X.shape)
        return 1

    train_m = splits == "train"
    valid_m = splits == "valid"
    if train_m.sum() < 10 or valid_m.sum() < 5:
        logger.error("insufficient split sizes train=%s valid=%s", train_m.sum(), valid_m.sum())
        return 1

    torch.manual_seed(int(args.seed))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("device=%s X=%s train=%d valid=%d", device, X.shape, train_m.sum(), valid_m.sum())

    if args.dry_run:
        return 0

    class EntryLSTM(nn.Module):
        def __init__(self, n_features: int, hidden: int) -> None:
            super().__init__()
            self.lstm = nn.LSTM(n_features, hidden, batch_first=True, num_layers=1)
            self.head = nn.Linear(hidden, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out, _ = self.lstm(x)
            last = out[:, -1, :]
            return self.head(last).squeeze(-1)

    n_features = X.shape[2]
    model = EntryLSTM(n_features, int(args.hidden)).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=float(args.lr))
    pos = float(y[train_m].sum())
    neg = float(train_m.sum() - pos)
    pos_weight = torch.tensor([neg / max(pos, 1.0)], device=device)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    train_ds = TensorDataset(torch.from_numpy(X[train_m]), torch.from_numpy(y[train_m].astype(np.float32)))
    train_dl = DataLoader(train_ds, batch_size=int(args.batch_size), shuffle=True)

    X_valid = torch.from_numpy(X[valid_m]).to(device)
    y_valid = y[valid_m]

    best_auc = float("nan")
    best_state = None
    for epoch in range(1, int(args.epochs) + 1):
        model.train()
        total_loss = 0.0
        n_batches = 0
        for xb, yb in train_dl:
            xb = xb.to(device)
            yb = yb.to(device)
            opt.zero_grad()
            logits = model(xb)
            loss = loss_fn(logits, yb)
            loss.backward()
            opt.step()
            total_loss += float(loss.item())
            n_batches += 1

        model.eval()
        with torch.no_grad():
            logits = model(X_valid)
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
        out_ckpt = str(project_root / "local" / "models" / "game5m_chart_entry_lstm.pt")
    ckpt_path = Path(out_ckpt).expanduser()
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "n_features": n_features,
            "hidden": int(args.hidden),
            "window_bars": int(raw.get("window_bars", X.shape[1])),
            "feature_names": list(raw.get("feature_names", [])),
        },
        ckpt_path,
    )
    logger.info("saved checkpoint → %s", ckpt_path)

    metrics = {
        "script": "train_game5m_chart_entry_lstm",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "npz": str(path),
        "device": str(device),
        "n_train": int(train_m.sum()),
        "n_valid": int(valid_m.sum()),
        "auc_valid": round(best_auc, 4) if best_auc == best_auc else None,
        "epochs": int(args.epochs),
        "hidden": int(args.hidden),
        "checkpoint": str(ckpt_path),
        "baseline_note": "Compare to bar v2 CatBoost AUC ~0.5495 on same time valid fold",
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
