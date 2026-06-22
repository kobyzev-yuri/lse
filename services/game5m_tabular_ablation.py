"""Tabular CatBoost ablation layer definitions (entry / hold)."""
from __future__ import annotations

from typing import Any

from services.game5m_entry_bar_dataset import BAR_TRAIN_NUMERIC_KEYS, BAR_TRAIN_FULL_NUMERIC_KEYS
from services.game5m_ml_context_features import (
    ENTRY_CONTEXT_NUMERIC_KEYS,
    HOLD_ENTRY_SNAPSHOT_KEYS,
    HOLD_EXIT_CONTEXT_NUMERIC_KEYS,
    HOLD_EXIT_TECH_KEYS,
    HOLD_STATE_KEYS,
)

# Entry context slices (beyond BAR_TRAIN_NUMERIC_KEYS).
ENTRY_TIME_KEYS: tuple[str, ...] = ("session_phase_enc", "dow_et", "hour_et")
ENTRY_KB_KEYS: tuple[str, ...] = ("kb_news_impact_enc", "kb_news_sentiment_mean", "kb_news_count")
ENTRY_NC_KEYS: tuple[str, ...] = tuple(
    k for k in ENTRY_CONTEXT_NUMERIC_KEYS if k not in ENTRY_TIME_KEYS and k not in ENTRY_KB_KEYS
)

# Cumulative entry ablation tracks (E0 → E3).
ENTRY_ABLATION_TRACKS: dict[str, tuple[str, ...]] = {
    "E0_T": BAR_TRAIN_NUMERIC_KEYS,
    "E1_T_time": BAR_TRAIN_NUMERIC_KEYS + ENTRY_TIME_KEYS,
    "E2_T_time_KB": BAR_TRAIN_NUMERIC_KEYS + ENTRY_TIME_KEYS + ENTRY_KB_KEYS,
    "E3_full_TNC": BAR_TRAIN_FULL_NUMERIC_KEYS,
}

# Isolated marginal (T + single layer only).
ENTRY_ABLATION_ISOLATED: dict[str, tuple[str, ...]] = {
    "E_T_only": BAR_TRAIN_NUMERIC_KEYS,
    "E_T_plus_time_only": BAR_TRAIN_NUMERIC_KEYS + ENTRY_TIME_KEYS,
    "E_T_plus_KB_only": BAR_TRAIN_NUMERIC_KEYS + ENTRY_KB_KEYS,
    "E_T_plus_NC_only": BAR_TRAIN_NUMERIC_KEYS + ENTRY_NC_KEYS,
}

# Cumulative hold ablation (H0 → H3).
HOLD_ABLATION_TRACKS: dict[str, tuple[str, ...]] = {
    "H0_state": HOLD_STATE_KEYS,
    "H1_state_entry": HOLD_STATE_KEYS + HOLD_ENTRY_SNAPSHOT_KEYS,
    "H2_state_entry_exit_tech": HOLD_STATE_KEYS + HOLD_ENTRY_SNAPSHOT_KEYS + HOLD_EXIT_TECH_KEYS,
    "H3_full": HOLD_STATE_KEYS + HOLD_ENTRY_SNAPSHOT_KEYS + HOLD_EXIT_TECH_KEYS + HOLD_EXIT_CONTEXT_NUMERIC_KEYS,
}

# Legacy recovery subset (B1 reference).
HOLD_RECOVERY_KEYS: tuple[str, ...] = (
    "ref_close",
    "entry_price",
    "pnl_pct",
    "hold_minutes",
    "minutes_after_rth_open",
    "dow",
    "hour_et",
    "entry_rsi_5m",
    "entry_vol_5m_pct",
    "entry_momentum_2h_pct",
)


def ablation_track_descriptions() -> dict[str, str]:
    return {
        "E0_T": "Technical BAR_TRAIN only",
        "E1_T_time": "T + session_phase, dow, hour",
        "E2_T_time_KB": "T + time + KB sentiment/count/impact",
        "E3_full_TNC": "Full T+N+C (gaps, macro, corr, prob, …)",
        "E_T_plus_KB_only": "T + KB only (no time)",
        "E_T_plus_NC_only": "T + gaps/macro/corr/prob only (no time/KB)",
        "H0_state": "pnl, hold_minutes, session timing",
        "H1_state_entry": "+ entry snapshot from BUY context",
        "H2_state_entry_exit_tech": "+ exit-time RSI/momentum/vol",
        "H3_full": "+ exit-bar news/calendar (N+C)",
        "H_recovery_B1": "Legacy recovery CatBoost feature set",
    }


__all__ = [
    "ENTRY_ABLATION_ISOLATED",
    "ENTRY_ABLATION_TRACKS",
    "ENTRY_KB_KEYS",
    "ENTRY_NC_KEYS",
    "ENTRY_TIME_KEYS",
    "HOLD_ABLATION_TRACKS",
    "HOLD_RECOVERY_KEYS",
    "ablation_track_descriptions",
]
