"""
Авторазметка строк event_reaction_dataset из daily quotes (MVP).

- features_before: log-returns и волатильность до якорной даты as_of (последний бар <= дня события ET).
- outcomes_after: forward log-returns на 1/5/20 торговых дней от close(as_of).
- final_label: rule-based UP/DOWN/FLAT относительно порога в log-пространстве (как portfolio ML edge).

Полные peer/regime фичи — в следующих версиях feature_builder_version.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text

from config_loader import get_config_value
from report_generator import get_engine
from services.portfolio_ml_features import portfolio_ml_threshold_log

logger = logging.getLogger(__name__)

FEATURE_BUILDER_VERSION = "quotes_mvp_1"
OUTCOME_BUILDER_VERSION = "quotes_fwd_1"

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone.utc


def event_reaction_label_threshold_log() -> float:
    """Порог |forward log-ret| для UP/DOWN vs FLAT; по умолчанию как portfolio ML edge."""
    raw = (get_config_value("EVENT_REACTION_LABEL_THRESHOLD_LOG", "") or "").strip()
    if raw:
        try:
            return float(raw.replace(",", "."))
        except (TypeError, ValueError):
            pass
    return portfolio_ml_threshold_log()


def _event_date_et(ts: Any) -> date:
    if ts is None:
        raise ValueError("event_time_et is None")
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(_ET).date()


def _empty_jsonb(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, dict):
        return len(val) == 0
    if isinstance(val, str):
        s = val.strip()
        return s in ("", "{}", "null")
    return True


def load_quotes_window(
    symbol: str,
    *,
    date_min: date,
    date_max: date,
) -> pd.DataFrame:
    sym = str(symbol or "").strip().upper()
    q = text(
        """
        SELECT date::date AS d, open, high, low, close, volume, rsi, volatility_5
        FROM quotes
        WHERE UPPER(TRIM(ticker)) = :t
          AND date::date >= :dmin
          AND date::date <= :dmax
        ORDER BY date ASC
        """
    )
    with get_engine().connect() as conn:
        df = pd.read_sql(q, conn, params={"t": sym, "dmin": date_min, "dmax": date_max})
    if df.empty:
        return df
    df["d"] = pd.to_datetime(df["d"]).dt.date
    for c in ("open", "high", "low", "close", "volume", "rsi", "volatility_5"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _find_as_of_index(dates: List[date], event_d: date) -> Optional[int]:
    idx = None
    for i, d in enumerate(dates):
        if d <= event_d:
            idx = i
        else:
            break
    return idx


def _log_ret_ratio(closes: np.ndarray, i_from: int, i_to: int) -> Optional[float]:
    if i_from < 0 or i_to >= len(closes) or i_from > i_to:
        return None
    a = closes[i_from]
    b = closes[i_to]
    if a is None or b is None or not (np.isfinite(a) and np.isfinite(b)) or a <= 0 or b <= 0:
        return None
    return float(math.log(b / a))


def build_features_before(
    df: pd.DataFrame,
    *,
    as_of_idx: int,
    event_d: date,
) -> Dict[str, Any]:
    closes = df["close"].to_numpy(dtype=float)
    dates = list(df["d"])
    i = as_of_idx
    out: Dict[str, Any] = {
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "as_of_trade_date": str(dates[i]),
        "event_date_et": str(event_d),
    }
    if i >= 1:
        lr = _log_ret_ratio(closes, i - 1, i)
        if lr is not None:
            out["ret_1d_log"] = round(lr, 6)
    if i >= 5:
        lr = _log_ret_ratio(closes, i - 5, i)
        if lr is not None:
            out["ret_5d_log"] = round(lr, 6)
    if i >= 20:
        lr = _log_ret_ratio(closes, i - 20, i)
        if lr is not None:
            out["ret_20d_log"] = round(lr, 6)
    window = closes[max(0, i - 9) : i + 1]
    if len(window) >= 5:
        lr_series = np.diff(np.log(window[np.isfinite(window) & (window > 0)]))
        if lr_series.size >= 3:
            out["vol_10d_log_ret_std"] = round(float(np.std(lr_series, ddof=0)), 6)
    rsi_row = df.iloc[i].get("rsi")
    if rsi_row is not None and np.isfinite(rsi_row):
        out["rsi_as_of"] = round(float(rsi_row), 4)
    out["close_as_of"] = round(float(closes[i]), 6)
    return out


def build_outcomes_after(
    df: pd.DataFrame,
    *,
    as_of_idx: int,
    horizons: Tuple[int, ...] = (1, 5, 20),
) -> Tuple[Dict[str, Any], Optional[float]]:
    closes = df["close"].to_numpy(dtype=float)
    i0 = as_of_idx
    out: Dict[str, Any] = {"outcome_builder_version": OUTCOME_BUILDER_VERSION}
    primary_fwd: Optional[float] = None
    for h in horizons:
        j = i0 + h
        key = f"forward_log_ret_{h}d"
        if j < len(closes):
            lr = _log_ret_ratio(closes, i0, j)
            if lr is not None:
                out[key] = round(lr, 6)
                if h == 5:
                    primary_fwd = lr
    thr = event_reaction_label_threshold_log()
    out["threshold_log_used"] = round(thr, 8)
    return out, primary_fwd


def infer_final_label(forward_5d_log: Optional[float]) -> Optional[str]:
    if forward_5d_log is None or not math.isfinite(forward_5d_log):
        return None
    thr = event_reaction_label_threshold_log()
    if forward_5d_log > thr:
        return "UP"
    if forward_5d_log < -thr:
        return "DOWN"
    return "FLAT"


def compute_row_labeling(
    symbol: str,
    event_time_et: Any,
    *,
    horizons: Tuple[int, ...] = (1, 5, 20),
    min_past_bars: int = 20,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str], str]:
    """
    Возвращает (features_before, outcomes_after, final_label, skip_reason).
    skip_reason пустой если ок.
    """
    try:
        event_d = _event_date_et(event_time_et)
    except Exception as e:
        return None, None, None, f"bad_event_ts:{e}"

    d_min = event_d - timedelta(days=400)
    d_max = event_d + timedelta(days=120)
    df = load_quotes_window(symbol, date_min=d_min, date_max=d_max)
    if df.empty:
        return None, None, None, "no_quotes"

    dates = list(df["d"])
    as_of_i = _find_as_of_index(dates, event_d)
    if as_of_i is None:
        return None, None, None, "no_as_of_before_event"

    if as_of_i < min_past_bars:
        return None, None, None, "insufficient_past_bars"

    feats = build_features_before(df, as_of_idx=as_of_i, event_d=event_d)
    if as_of_i + 5 >= len(dates):
        return feats, None, None, "insufficient_forward_for_5d"

    outcomes, fwd5 = build_outcomes_after(df, as_of_idx=as_of_i, horizons=horizons)
    label = infer_final_label(fwd5)
    return feats, outcomes, label, ""


def json_dumps_obj(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def labeling_updates_for_row(
    row: Dict[str, Any],
    *,
    do_features: bool,
    do_outcomes: bool,
    force_features: bool,
    force_outcomes: bool,
    horizons: Tuple[int, ...] = (1, 5, 20),
) -> Tuple[Dict[str, Any], str]:
    """
    Возвращает (поля для SET в UPDATE, note).
    note — пусто при полном успехе; иначе причина пропуска или предупреждение при частичном обновлении.
    """
    cur_f = row.get("features_before")
    cur_o = row.get("outcomes_after")
    want_f = do_features and (force_features or _empty_jsonb(cur_f))
    want_o = do_outcomes and (force_outcomes or _empty_jsonb(cur_o))
    if not want_f and not want_o:
        return {}, "skip_already_filled"

    sym = str(row.get("symbol") or "").strip().upper()
    if not sym:
        return {}, "skip_no_symbol"

    feats, outs, label, reason = compute_row_labeling(sym, row.get("event_time_et"), horizons=horizons)
    out: Dict[str, Any] = {}
    notes: List[str] = []

    if want_f:
        if feats:
            out["features_before"] = feats
        else:
            notes.append(f"features:{reason}")

    if want_o:
        if outs:
            out["outcomes_after"] = outs
            out["final_label"] = label
            out["label_source"] = "auto_quotes_v1"
        else:
            notes.append(f"outcomes:{reason}")

    if not out:
        return {}, ";".join(notes) if notes else "skip_empty"
    return out, ";".join(notes)
