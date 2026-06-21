"""Chart-window tensors for GAME_5M entry ML (research, triple-barrier y from bar CSV)."""
from __future__ import annotations

import hashlib
import math
from typing import Any, Literal

import numpy as np
import pandas as pd

CHART_ENTRY_ML_SCHEMA_VERSION = "1"
DEFAULT_WINDOW_BARS = 48
DEFAULT_VALID_RATIO = 0.2

# Per-bar features at decision time (inclusive window ending at anchor close).
CHART_FEATURE_NAMES: tuple[str, ...] = (
    "open_pct_anchor",
    "high_pct_anchor",
    "low_pct_anchor",
    "close_pct_anchor",
    "log_volume",
)

CHART_ENTRY_ML_SCHEMA: dict[str, Any] = {
    "version": CHART_ENTRY_ML_SCHEMA_VERSION,
    "unit": "(ticker, bar_ts) OHLCV window + y_entry_good from bar CSV",
    "window_bars": DEFAULT_WINDOW_BARS,
    "window_end": "inclusive at decision bar close (predict at close)",
    "features": list(CHART_FEATURE_NAMES),
    "label_source": "y_entry_good / tb_label from build_game5m_entry_bar_dataset.py",
    "normalization": "OHLC % vs anchor close; log1p(volume) z-scored within window",
    "split": "time-ordered last valid_ratio → valid (matches bar v2 CatBoost)",
    "docs": "docs/GAME_5M_CHART_PATTERN_ML_RESEARCH_PLAN.md",
}

SplitName = Literal["train", "valid"]


def make_sample_id(ticker: str, bar_ts_et: str) -> str:
    raw = f"{(ticker or '').strip().upper()}|{(bar_ts_et or '').strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_bar_ts_et(bar_ts_et: str) -> pd.Timestamp:
    ts = pd.Timestamp(bar_ts_et)
    if ts.tzinfo is None:
        ts = ts.tz_localize("America/New_York", ambiguous=True)
    else:
        ts = ts.tz_convert("America/New_York")
    return ts


def find_decision_bar_index(df: pd.DataFrame, bar_ts_et: str) -> int | None:
    """Index of decision bar (bar open == bar_ts_et) in normalized OHLC dataframe."""
    if df is None or df.empty or "datetime" not in df.columns:
        return None
    target = parse_bar_ts_et(bar_ts_et)
    for i in range(len(df)):
        ts = pd.Timestamp(df.iloc[i]["datetime"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("America/New_York", ambiguous=True)
        else:
            ts = ts.tz_convert("America/New_York")
        if ts == target:
            return i
    return None


def _safe_float(v: Any) -> float:
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return float("nan")


def window_tensor_from_df(
    df: pd.DataFrame,
    decision_idx: int,
    *,
    window_bars: int = DEFAULT_WINDOW_BARS,
) -> np.ndarray | None:
    """
    Build (window_bars, n_features) float32 tensor.
    Window is [decision_idx - window_bars + 1 .. decision_idx] inclusive.
    Returns None if insufficient history or bad anchor close.
    """
    if df is None or df.empty or decision_idx < 0 or decision_idx >= len(df):
        return None
    w = max(1, int(window_bars))
    start = decision_idx - w + 1
    if start < 0:
        return None

    anchor_close = _safe_float(df.iloc[decision_idx].get("Close"))
    if not math.isfinite(anchor_close) or anchor_close <= 0:
        return None

    rows: list[list[float]] = []
    log_vols: list[float] = []
    for i in range(start, decision_idx + 1):
        row = df.iloc[i]
        o = _safe_float(row.get("Open"))
        h = _safe_float(row.get("High"))
        lo = _safe_float(row.get("Low"))
        c = _safe_float(row.get("Close"))
        vol = _safe_float(row.get("Volume"))
        if not all(math.isfinite(x) for x in (o, h, lo, c)):
            return None
        log_v = math.log1p(max(0.0, vol)) if math.isfinite(vol) else 0.0
        log_vols.append(log_v)
        rows.append(
            [
                (o / anchor_close - 1.0) * 100.0,
                (h / anchor_close - 1.0) * 100.0,
                (lo / anchor_close - 1.0) * 100.0,
                (c / anchor_close - 1.0) * 100.0,
                log_v,
            ]
        )

    arr = np.asarray(rows, dtype=np.float32)
    if arr.shape[0] != w:
        return None

    # z-score log_volume within window only (no cross-sample stats)
    lv = arr[:, 4]
    mu = float(lv.mean())
    std = float(lv.std())
    if std > 1e-8:
        arr[:, 4] = (lv - mu) / std
    else:
        arr[:, 4] = 0.0

    return arr


def window_includes_future_bars(df: pd.DataFrame, decision_idx: int, window: np.ndarray, *, window_bars: int) -> bool:
    """True if any tensor row uses data from bars after decision_idx (leak)."""
    w = max(1, int(window_bars))
    start = decision_idx - w + 1
    if start < 0:
        return True
    # Last row must correspond to decision_idx close
    anchor = _safe_float(df.iloc[decision_idx].get("Close"))
    last_close = _safe_float(df.iloc[decision_idx].get("Close"))
    if not math.isfinite(anchor) or anchor <= 0:
        return True
    expected_last = (last_close / anchor - 1.0) * 100.0
    if abs(float(window[-1, 3]) - expected_last) > 1e-3:
        return True
    return False


def assign_time_splits(bar_ts_list: list[str], *, valid_ratio: float = DEFAULT_VALID_RATIO) -> list[SplitName]:
    """Same policy as train_game5m_catboost bar mode: sort by bar_ts, last fraction → valid."""
    n = len(bar_ts_list)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: bar_ts_list[i] or "")
    n_valid = max(1, int(n * float(valid_ratio)))
    n_train = n - n_valid
    if n_train < 10 and n > 10:
        n_train = max(10, n // 2)
        n_valid = n - n_train
    split_by_orig: list[SplitName] = ["train"] * n
    for j in order[n_train:]:
        split_by_orig[j] = "valid"
    return split_by_orig


def chart_dataset_summary(
    *,
    n_rows: int,
    n_skipped: int,
    y_pos: int,
    n_train: int,
    n_valid: int,
    window_bars: int,
    bar_csv: str,
    source: str,
) -> dict[str, Any]:
    return {
        "schema_version": CHART_ENTRY_ML_SCHEMA_VERSION,
        "n_rows": n_rows,
        "n_skipped": n_skipped,
        "y_entry_good_rate": round(y_pos / n_rows, 4) if n_rows else 0.0,
        "n_train": n_train,
        "n_valid": n_valid,
        "valid_ratio": DEFAULT_VALID_RATIO,
        "window_bars": window_bars,
        "n_features": len(CHART_FEATURE_NAMES),
        "feature_names": list(CHART_FEATURE_NAMES),
        "bar_csv": bar_csv,
        "ohlc_source": source,
    }


__all__ = [
    "CHART_ENTRY_ML_SCHEMA",
    "CHART_ENTRY_ML_SCHEMA_VERSION",
    "CHART_FEATURE_NAMES",
    "DEFAULT_VALID_RATIO",
    "DEFAULT_WINDOW_BARS",
    "SplitName",
    "assign_time_splits",
    "chart_dataset_summary",
    "find_decision_bar_index",
    "make_sample_id",
    "parse_bar_ts_et",
    "window_includes_future_bars",
    "window_tensor_from_df",
]
