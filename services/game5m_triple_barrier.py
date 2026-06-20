"""Path-dependent triple-barrier labels on 5m OHLC (GAME_5M entry / hold research)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from config_loader import get_config_value

BarrierLabel = Literal["upper", "lower", "time", "insufficient_data"]

ENTRY_BAR_ML_SCHEMA_VERSION = "1"

ENTRY_BAR_ML_SCHEMA: dict[str, Any] = {
    "version": ENTRY_BAR_ML_SCHEMA_VERSION,
    "unit": "(ticker, bar_ts) candidate entry",
    "label_columns": {
        "tb_label": "upper | lower | time | insufficient_data",
        "y_entry_good": "1 iff tb_label == upper (path-dependent long entry target)",
        "tb_upper_pct": "effective upper hurdle incl. cost drag",
        "tb_lower_pct": "effective lower hurdle incl. cost drag",
        "tb_bars_forward": "bars scanned until first touch or time barrier",
        "tb_mfe_pct": "max favorable excursion % from anchor close over window",
        "tb_mae_pct": "max adverse excursion % from anchor close over window",
    },
    "feature_source": "compute_5m_features + decision snapshot fields (builder phase 1)",
    "docs": "docs/GAME_5M_PREDICTOR_DATASET_PLAN.md",
}


@dataclass(frozen=True)
class TripleBarrierConfig:
    upper_pct: float = 1.0
    lower_pct: float = 1.0
    max_bars: int = 24
    max_minutes: int = 120
    cost_bps: float = 20.0
    pessimistic_same_bar: bool = True

    def effective_upper_pct(self) -> float:
        return float(self.upper_pct) + float(self.cost_bps) / 100.0

    def effective_lower_pct(self) -> float:
        return float(self.lower_pct) + float(self.cost_bps) / 100.0


@dataclass(frozen=True)
class TripleBarrierResult:
    label: BarrierLabel
    y_entry_good: bool
    anchor_close: float | None
    upper_price: float | None
    lower_price: float | None
    bars_forward: int
    minutes_forward: float | None
    mfe_pct: float | None
    mae_pct: float | None
    first_touch_ts: str | None = None


def _float_cfg(key: str, default: float) -> float:
    raw = (get_config_value(key) or "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int_cfg(key: str, default: int) -> int:
    raw = (get_config_value(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def triple_barrier_config_from_env() -> TripleBarrierConfig:
    return TripleBarrierConfig(
        upper_pct=_float_cfg("GAME_5M_TB_UPPER_PCT", 1.0),
        lower_pct=_float_cfg("GAME_5M_TB_LOWER_PCT", 1.0),
        max_bars=max(1, _int_cfg("GAME_5M_TB_MAX_BARS", 24)),
        max_minutes=max(1, _int_cfg("GAME_5M_TB_MAX_MINUTES", 120)),
        cost_bps=_float_cfg("GAME_5M_TB_COST_BPS", 20.0),
    )


def _pct_move(base: float, price: float) -> float:
    return (float(price) / float(base) - 1.0) * 100.0


def _normalize_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame()
    out = bars.copy()
    if "datetime" not in out.columns:
        raise ValueError("bars must include datetime column")
    d = pd.to_datetime(out["datetime"])
    if getattr(d.dt, "tz", None) is None:
        d = d.dt.tz_localize("America/New_York", ambiguous=True)
    else:
        d = d.dt.tz_convert("America/New_York")
    out["datetime"] = d
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values("datetime").reset_index(drop=True)


def _same_bar_first_touch_long(
    *,
    open_px: float,
    high_px: float,
    low_px: float,
    upper_price: float,
    lower_price: float,
    pessimistic: bool,
) -> BarrierLabel | None:
    hit_up = high_px >= upper_price
    hit_dn = low_px <= lower_price
    if hit_up and not hit_dn:
        return "upper"
    if hit_dn and not hit_up:
        return "lower"
    if not hit_up and not hit_dn:
        return None
    if pessimistic:
        return "lower"
    dist_up = abs(open_px - upper_price)
    dist_dn = abs(open_px - lower_price)
    return "upper" if dist_up <= dist_dn else "lower"


def forward_excursion_pct(
    bars: pd.DataFrame,
    start_idx: int,
    *,
    max_bars: int | None = None,
    max_minutes: int | None = None,
) -> tuple[float | None, float | None]:
    """MFE/MAE % from anchor close over forward window (for recovery-style labels)."""
    df = _normalize_bars(bars)
    if start_idx < 0 or start_idx >= len(df):
        return None, None
    anchor = df.iloc[start_idx].get("Close")
    if anchor is None or float(anchor) <= 0:
        return None, None
    anchor_f = float(anchor)
    start_ts = df.iloc[start_idx]["datetime"]
    end_idx = len(df) - 1
    if max_bars is not None:
        end_idx = min(end_idx, start_idx + int(max_bars))
    fwd = df.iloc[start_idx + 1 : end_idx + 1]
    if max_minutes is not None and not fwd.empty:
        limit_ts = start_ts + pd.Timedelta(minutes=int(max_minutes))
        fwd = fwd.loc[fwd["datetime"] <= limit_ts]
    if fwd.empty:
        return None, None
    high_max = float(pd.to_numeric(fwd["High"], errors="coerce").max())
    low_min = float(pd.to_numeric(fwd["Low"], errors="coerce").min())
    return _pct_move(anchor_f, high_max), _pct_move(anchor_f, low_min)


def forward_mfe_mae_pct_window(
    bars: pd.DataFrame,
    *,
    ref_close: float,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> tuple[float | None, float | None]:
    """MFE/MAE % from ref_close over (start_ts, end_ts] — shared with recovery JSONL labels."""
    if ref_close <= 0:
        return None, None
    df = _normalize_bars(bars)
    if df.empty:
        return None, None
    try:
        st = pd.Timestamp(start_ts)
        et = pd.Timestamp(end_ts)
        if st.tzinfo is None:
            st = st.tz_localize("America/New_York", ambiguous=True)
        else:
            st = st.tz_convert("America/New_York")
        if et.tzinfo is None:
            et = et.tz_localize("America/New_York", ambiguous=True)
        else:
            et = et.tz_convert("America/New_York")
        fwd = df.loc[(df["datetime"] > st) & (df["datetime"] <= et)]
        if fwd is None or fwd.empty:
            return None, None
        hi = float(pd.to_numeric(fwd["High"], errors="coerce").max())
        lo = float(pd.to_numeric(fwd["Low"], errors="coerce").min())
        return _pct_move(ref_close, hi), _pct_move(ref_close, lo)
    except Exception:
        return None, None


def recovery_y_label(
    mfe_pct: float | None,
    mae_pct: float | None,
    *,
    eps_up_pct: float,
    max_adverse_pct: float,
) -> int | None:
    """Binary recovery label (same rule as RECOVERY_ML_SCHEMA export)."""
    if mfe_pct is None or mae_pct is None:
        return None
    return 1 if (mfe_pct >= eps_up_pct and mae_pct >= max_adverse_pct) else 0


def triple_barrier_forward(
    bars: pd.DataFrame,
    start_idx: int,
    *,
    config: TripleBarrierConfig | None = None,
) -> TripleBarrierResult:
    """
    First-touch triple barrier on forward 5m bars after anchor bar ``start_idx``.

    Uses High/Low per bar for long side. Same-bar double touch: pessimistic lower
  unless ``pessimistic_same_bar=False`` (closer to open wins).
    """
    cfg = config or triple_barrier_config_from_env()
    df = _normalize_bars(bars)
    empty = TripleBarrierResult(
        label="insufficient_data",
        y_entry_good=False,
        anchor_close=None,
        upper_price=None,
        lower_price=None,
        bars_forward=0,
        minutes_forward=None,
        mfe_pct=None,
        mae_pct=None,
    )
    if start_idx < 0 or start_idx >= len(df):
        return empty
    anchor = df.iloc[start_idx].get("Close")
    if anchor is None or float(anchor) <= 0:
        return empty
    anchor_f = float(anchor)
    start_ts = df.iloc[start_idx]["datetime"]
    upper_price = anchor_f * (1.0 + cfg.effective_upper_pct() / 100.0)
    lower_price = anchor_f * (1.0 - cfg.effective_lower_pct() / 100.0)
    limit_ts = start_ts + pd.Timedelta(minutes=int(cfg.max_minutes))
    end_idx = min(len(df) - 1, start_idx + int(cfg.max_bars))

    mfe_pct: float | None = None
    mae_pct: float | None = None
    bars_forward = 0
    first_touch_ts: str | None = None

    for i in range(start_idx + 1, end_idx + 1):
        row = df.iloc[i]
        bar_ts = row["datetime"]
        if bar_ts > limit_ts:
            break
        bars_forward += 1
        high_px = float(row["High"])
        low_px = float(row["Low"])
        open_px = float(row["Open"])
        high_max = high_px if mfe_pct is None else max(
            anchor_f * (1.0 + mfe_pct / 100.0), high_px
        )
        low_min = low_px if mae_pct is None else min(
            anchor_f * (1.0 + mae_pct / 100.0), low_px
        )
        mfe_pct = _pct_move(anchor_f, high_max)
        mae_pct = _pct_move(anchor_f, low_min)

        touch = _same_bar_first_touch_long(
            open_px=open_px,
            high_px=high_px,
            low_px=low_px,
            upper_price=upper_price,
            lower_price=lower_price,
            pessimistic=cfg.pessimistic_same_bar,
        )
        if touch is not None:
            first_touch_ts = bar_ts.isoformat()
            return TripleBarrierResult(
                label=touch,
                y_entry_good=touch == "upper",
                anchor_close=anchor_f,
                upper_price=upper_price,
                lower_price=lower_price,
                bars_forward=bars_forward,
                minutes_forward=float((bar_ts - start_ts).total_seconds() / 60.0),
                mfe_pct=mfe_pct,
                mae_pct=mae_pct,
                first_touch_ts=first_touch_ts,
            )

    return TripleBarrierResult(
        label="time",
        y_entry_good=False,
        anchor_close=anchor_f,
        upper_price=upper_price,
        lower_price=lower_price,
        bars_forward=bars_forward,
        minutes_forward=float((df.iloc[min(end_idx, start_idx + bars_forward)]["datetime"] - start_ts).total_seconds() / 60.0)
        if bars_forward > 0
        else 0.0,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        first_touch_ts=first_touch_ts,
    )
