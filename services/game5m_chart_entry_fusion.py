"""LSTM + tabular fusion for GAME_5M chart entry (Phase 2 — no broadcast ctx)."""
from __future__ import annotations

import math
from typing import Any, Literal, Mapping

from services.game5m_tabular_ablation import ENTRY_ABLATION_TRACKS

FusionTabMode = Literal["e2", "e3"]
FusionMode = Literal["residual", "concat"]

FUSION_TAB_MODES: tuple[FusionTabMode, ...] = ("e2", "e3")
FUSION_MODES: tuple[FusionMode, ...] = ("residual", "concat")


def fusion_tab_keys(mode: FusionTabMode) -> tuple[str, ...]:
    """Tabular keys fused at LSTM embedding (not broadcast on chart window)."""
    if mode == "e2":
        return ENTRY_ABLATION_TRACKS["E2_T_time_KB"]
    if mode == "e3":
        return ENTRY_ABLATION_TRACKS["E3_full_TNC"]
    raise ValueError(f"unknown fusion tab mode: {mode}")


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


def tabular_vector_from_row(row: Mapping[str, Any], keys: tuple[str, ...]) -> list[float]:
    return [_safe_float(row.get(k)) for k in keys]


def build_entry_lstm_fusion_model(
    *,
    n_chart_features: int,
    n_tab_features: int,
    hidden: int = 64,
    tab_hidden: int = 32,
    dropout: float = 0.1,
    fusion_mode: FusionMode = "residual",
):
    """Return fusion nn.Module (requires torch). residual = frozen chart logit + tab correction."""
    import torch
    import torch.nn as nn

    class EntryLSTMResidualFusion(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.fusion_mode = fusion_mode
            self.lstm = nn.LSTM(int(n_chart_features), int(hidden), batch_first=True, num_layers=1)
            self.chart_head = nn.Linear(int(hidden), 1)
            self.tab_mlp = nn.Sequential(
                nn.Linear(int(n_tab_features), int(tab_hidden)),
                nn.ReLU(inplace=True),
                nn.Dropout(p=float(dropout)),
            )
            self.tab_head = nn.Linear(int(tab_hidden), 1)
            if fusion_mode == "concat":
                self.fuse_head = nn.Sequential(
                    nn.Linear(int(hidden) + int(tab_hidden), int(hidden)),
                    nn.ReLU(inplace=True),
                    nn.Dropout(p=float(dropout)),
                    nn.Linear(int(hidden), 1),
                )

        def forward(self, x_chart: torch.Tensor, x_tab: torch.Tensor) -> torch.Tensor:
            out, _ = self.lstm(x_chart)
            emb = out[:, -1, :]
            tab = self.tab_mlp(x_tab)
            if self.fusion_mode == "residual":
                return (self.chart_head(emb) + self.tab_head(tab)).squeeze(-1)
            return self.fuse_head(torch.cat([emb, tab], dim=1)).squeeze(-1)

    return EntryLSTMResidualFusion()


def load_pretrained_lstm_into_fusion(model: Any, checkpoint_path: str, *, freeze: bool = True) -> None:
    """Copy lstm.* (+ chart_head.* for residual) from train_game5m_chart_entry_lstm checkpoint."""
    import torch

    ckpt = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    state = ckpt.get("state_dict") or ckpt
    lstm_state = {k[len("lstm."):]: v for k, v in state.items() if k.startswith("lstm.")}
    if not lstm_state:
        raise ValueError(f"no lstm weights in {checkpoint_path}")
    model.lstm.load_state_dict(lstm_state)
    head_state = {k[len("head."):]: v for k, v in state.items() if k.startswith("head.")}
    if head_state and hasattr(model, "chart_head"):
        try:
            model.chart_head.load_state_dict(head_state)
        except Exception:
            pass
    if freeze:
        for p in model.lstm.parameters():
            p.requires_grad = False
        if hasattr(model, "chart_head"):
            for p in model.chart_head.parameters():
                p.requires_grad = False


__all__ = [
    "FUSION_MODES",
    "FUSION_TAB_MODES",
    "FusionMode",
    "FusionTabMode",
    "build_entry_lstm_fusion_model",
    "fusion_tab_keys",
    "load_pretrained_lstm_into_fusion",
    "tabular_vector_from_row",
]
