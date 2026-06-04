"""
Авторазметка строк event_reaction_dataset из daily quotes (MVP).

- features_before: log-returns и волатильность до якорной даты as_of (последний бар <= дня события ET).
- outcomes_after: forward log-returns на 1/5/20 торговых дней от close(as_of).
- final_label: rule-based UP/DOWN/FLAT относительно порога в log-пространстве (как portfolio ML edge).

Версия `quotes_regime_v1` добавляет снимок `market_regime_daily` на `as_of_trade_date`
(SPY/QQQ/DIA/^VIX, 1d log-returns, vix_regime). Peer-фичи — отдельно.
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

FEATURE_BUILDER_VERSION_QUOTES = "quotes_mvp_1"
FEATURE_BUILDER_VERSION_REGIME = "quotes_regime_v1"
FEATURE_BUILDER_VERSION_EARNINGS = "quotes_regime_earnings_v1"
# Back-compat alias
FEATURE_BUILDER_VERSION = FEATURE_BUILDER_VERSION_QUOTES
OUTCOME_BUILDER_VERSION = "quotes_fwd_1"

VIX_REGIME_ORD: Dict[str, float] = {
    "LOW_FEAR": 0.0,
    "NEUTRAL": 1.0,
    "HIGH_PANIC": 2.0,
    "NO_DATA": -1.0,
}

_REGIME_CLOSE_MAP = (
    ("spy_close", "mkt_spy_close"),
    ("ndx_close", "mkt_ndx_close"),
    ("dia_close", "mkt_dia_close"),
    ("vix_close", "mkt_vix_close"),
)
_REGIME_LOG_RET_KEYS = (
    ("log_ret_1d_spy", "mkt_log_ret_1d_spy"),
    ("log_ret_1d_ndx", "mkt_log_ret_1d_ndx"),
    ("log_ret_1d_dia", "mkt_log_ret_1d_dia"),
)


def active_feature_builder_version() -> str:
    raw = (get_config_value("EVENT_REACTION_FEATURE_BUILDER_VERSION", "") or "").strip()
    if raw in (FEATURE_BUILDER_VERSION_QUOTES, FEATURE_BUILDER_VERSION_REGIME, FEATURE_BUILDER_VERSION_EARNINGS):
        return raw
    return FEATURE_BUILDER_VERSION_REGIME


# Все поля ниже должны быть посчитаны из OHLC; без заглушек (см. build_features_before).
QUOTE_FEATURE_KEYS_REQUIRED = (
    "ret_1d_log",
    "ret_5d_log",
    "ret_20d_log",
    "vol_10d_log_ret_std",
    "rsi_as_of",
    "close_as_of",
)


def count_trainable_regression_rows(
    engine,
    *,
    dataset_version: str,
    feature_builder_version: str,
) -> int:
    """Rows matching train_event_reaction_catboost SQL filters for a feature_builder_version."""
    from sqlalchemy import text

    q = text(
        """
        SELECT COUNT(*)::int
        FROM event_reaction_dataset
        WHERE dataset_version = :dv
          AND features_before IS NOT NULL AND features_before <> '{}'::jsonb
          AND outcomes_after IS NOT NULL AND outcomes_after <> '{}'::jsonb
          AND outcomes_after ? 'forward_log_ret_5d'
          AND (features_before->>'feature_builder_version') = :fbv
        """
    )
    with engine.connect() as conn:
        return int(
            conn.execute(
                q,
                {"dv": dataset_version, "fbv": feature_builder_version},
            ).scalar()
            or 0
        )


def resolve_training_feature_builder_version(
    engine,
    *,
    dataset_version: str,
    preferred: str = "",
) -> str:
    """
    Pick feature_builder_version for regression train/inference.

    Nightly cron writes quotes_regime_v1 then overwrites earnings-universe rows with
    quotes_regime_earnings_v1 (superset). Prefer config, else fall back to the version
    that actually has trainable rows in DB.
    """
    pref = (preferred or "").strip() or active_feature_builder_version()
    if count_trainable_regression_rows(engine, dataset_version=dataset_version, feature_builder_version=pref) > 0:
        return pref
    for fbv in (
        FEATURE_BUILDER_VERSION_EARNINGS,
        FEATURE_BUILDER_VERSION_REGIME,
        FEATURE_BUILDER_VERSION_QUOTES,
    ):
        if fbv == pref:
            continue
        n = count_trainable_regression_rows(engine, dataset_version=dataset_version, feature_builder_version=fbv)
        if n > 0:
            logger.info(
                "event_reaction regression: preferred %s has 0 trainable rows; using %s (%s rows)",
                pref,
                fbv,
                n,
            )
            return fbv
    return pref


def event_reaction_numeric_feature_keys(feature_builder_version: str) -> Tuple[str, ...]:
    """All numeric keys for CatBoost (quote + regime)."""
    quote = QUOTE_FEATURE_KEYS_REQUIRED
    if feature_builder_version in (FEATURE_BUILDER_VERSION_REGIME, FEATURE_BUILDER_VERSION_EARNINGS):
        regime = (
            "market_regime_present",
            "mkt_spy_close",
            "mkt_ndx_close",
            "mkt_dia_close",
            "mkt_vix_close",
            "mkt_log_ret_1d_spy",
            "mkt_log_ret_1d_ndx",
            "mkt_log_ret_1d_dia",
            "mkt_vix_regime_ord",
            "mkt_spy_stress_1d",
        )
        if feature_builder_version == FEATURE_BUILDER_VERSION_EARNINGS:
            earnings = (
                "earnings_detail_present",
                "earnings_eps_estimate",
                "earnings_eps_estimate_abs",
                "earnings_report_timing_ord",
                "earnings_tone_ord",
                "earnings_scenario_hints_n",
                "peer_graph_present",
                "peer_graph_out_degree",
                "peer_graph_weight_sum",
                "peer_momentum_n",
                "peer_momentum_mean_5d_log",
                "peer_momentum_max_5d_log",
            )
            return quote + regime + earnings
        return quote + regime
    return quote


def event_reaction_required_quote_keys() -> Tuple[str, ...]:
    return QUOTE_FEATURE_KEYS_REQUIRED


def missing_quote_feature_keys(feats: Dict[str, Any]) -> Optional[str]:
    """Пустая строка если ок; иначе причина пропуска (первая недостающая колонка)."""
    for k in QUOTE_FEATURE_KEYS_REQUIRED:
        if k not in feats:
            return f"missing:{k}"
        try:
            v = float(feats[k])
        except (TypeError, ValueError):
            return f"invalid:{k}"
        if not math.isfinite(v):
            return f"non_finite:{k}"
    return None


def _rsi_as_of_from_closes(closes: np.ndarray, as_of_idx: int) -> Optional[float]:
    from services.rsi_calculator import RSI_PERIOD, compute_rsi_from_closes

    if as_of_idx < RSI_PERIOD:
        return None
    segment = closes[: as_of_idx + 1]
    closes_list = [float(c) for c in segment if np.isfinite(c) and c > 0]
    if len(closes_list) < RSI_PERIOD + 1:
        return None
    return compute_rsi_from_closes(closes_list, period=RSI_PERIOD)


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


def _parse_json_field(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            o = json.loads(raw)
            return o if isinstance(o, dict) else {}
        except Exception:
            return {}
    return {}


def load_market_regime_row(trade_date: date) -> Optional[Dict[str, Any]]:
    q = text(
        """
        SELECT trade_date, spy_close, ndx_close, dia_close, vix_close,
               regime_flags, features_json
        FROM market_regime_daily
        WHERE trade_date = :d
        """
    )
    try:
        with get_engine().connect() as conn:
            row = conn.execute(q, {"d": trade_date}).fetchone()
    except Exception as e:
        logger.debug("market_regime_daily unavailable for %s: %s", trade_date, e)
        return None
    if not row:
        return None
    return {
        "trade_date": row[0],
        "spy_close": row[1],
        "ndx_close": row[2],
        "dia_close": row[3],
        "vix_close": row[4],
        "regime_flags": _parse_json_field(row[5]),
        "features_json": _parse_json_field(row[6]),
    }


def enrich_features_with_market_regime(
    base: Dict[str, Any],
    regime: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(base)
    out["feature_builder_version"] = FEATURE_BUILDER_VERSION_REGIME
    if not regime:
        out["market_regime_present"] = 0
        out["mkt_vix_regime_ord"] = VIX_REGIME_ORD["NO_DATA"]
        out["mkt_spy_stress_1d"] = 0.0
        return out

    out["market_regime_present"] = 1
    td = regime.get("trade_date")
    out["market_regime_date"] = str(td) if td is not None else out.get("as_of_trade_date")

    for src_col, dst_key in _REGIME_CLOSE_MAP:
        val = regime.get(src_col)
        if val is not None:
            try:
                out[dst_key] = round(float(val), 6)
            except (TypeError, ValueError):
                pass

    fj = regime.get("features_json") if isinstance(regime.get("features_json"), dict) else {}
    for src_key, dst_key in _REGIME_LOG_RET_KEYS:
        val = fj.get(src_key)
        if val is not None:
            try:
                out[dst_key] = round(float(val), 8)
            except (TypeError, ValueError):
                pass

    flags = regime.get("regime_flags") if isinstance(regime.get("regime_flags"), dict) else {}
    vix_label = str(flags.get("vix_regime") or "NO_DATA").strip().upper()
    out["mkt_vix_regime_ord"] = VIX_REGIME_ORD.get(vix_label, VIX_REGIME_ORD["NO_DATA"])
    out["mkt_spy_stress_1d"] = 1.0 if flags.get("spy_stress_1d") else 0.0
    return out


_EARNINGS_TIMING_ORD: Dict[str, float] = {
    "UNKNOWN": 0.0,
    "BEFORE_OPEN": 1.0,
    "DURING_SESSION": 2.0,
    "AFTER_CLOSE": 3.0,
}

_EARNINGS_TONE_ORD: Dict[str, float] = {
    "bearish": 0.0,
    "defensive": 0.5,
    "cautious": 1.0,
    "not_clear": 1.5,
    "mixed": 2.0,
    "bullish": 3.0,
}


def _earnings_timing_from_hour(hour: Optional[int]) -> str:
    if hour is None:
        return "UNKNOWN"
    if hour < 9:
        return "BEFORE_OPEN"
    if hour < 16:
        return "DURING_SESSION"
    return "AFTER_CLOSE"


def load_earnings_detail_for_event(
    symbol: str,
    event_d: date,
    *,
    knowledge_base_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Load known earnings metadata for the symbol/event date.

    Only pre-event-safe fields are used as model features here. Post-release
    values such as reported EPS and surprise stay in guidance_summary for
    analysis/post-event models to avoid leakage in pre-event forecasts.
    """
    if knowledge_base_id:
        q_by_kb = text(
            """
            SELECT eps_estimate, eps_actual, guidance_summary
            FROM earnings_event_detail
            WHERE knowledge_base_id = :kb_id
            LIMIT 1
            """
        )
        try:
            with get_engine().connect() as conn:
                row = conn.execute(q_by_kb, {"kb_id": int(knowledge_base_id)}).fetchone()
        except Exception as e:
            logger.debug("earnings_event_detail unavailable for kb=%s: %s", knowledge_base_id, e)
            row = None
        if row:
            guidance = _parse_json_field(row[2])
            return {
                "eps_estimate": row[0],
                "eps_actual": row[1],
                "guidance_summary": guidance,
            }

    q = text(
        """
        SELECT ed.eps_estimate, ed.eps_actual, ed.guidance_summary
        FROM earnings_event_detail ed
        JOIN knowledge_base kb ON kb.id = ed.knowledge_base_id
        WHERE UPPER(TRIM(kb.ticker)) = :symbol
          AND (
            kb.ts::date = :event_d
            OR (kb.ts AT TIME ZONE 'America/New_York')::date = :event_d
          )
        ORDER BY ed.updated_at DESC
        LIMIT 1
        """
    )
    try:
        with get_engine().connect() as conn:
            row = conn.execute(q, {"symbol": str(symbol or "").strip().upper(), "event_d": event_d}).fetchone()
    except Exception as e:
        logger.debug("earnings_event_detail unavailable for %s %s: %s", symbol, event_d, e)
        return None
    if not row:
        return None
    guidance = _parse_json_field(row[2])
    return {
        "eps_estimate": row[0],
        "eps_actual": row[1],
        "guidance_summary": guidance,
    }


def enrich_features_with_earnings_detail(
    base: Dict[str, Any],
    detail: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(base)
    out["feature_builder_version"] = FEATURE_BUILDER_VERSION_EARNINGS
    if not detail:
        out["earnings_detail_present"] = 0.0
        out["earnings_eps_estimate"] = 0.0
        out["earnings_eps_estimate_abs"] = 0.0
        out["earnings_report_timing_ord"] = _EARNINGS_TIMING_ORD["UNKNOWN"]
        return out
    out["earnings_detail_present"] = 1.0
    try:
        est = float(detail.get("eps_estimate")) if detail.get("eps_estimate") is not None else 0.0
    except (TypeError, ValueError):
        est = 0.0
    out["earnings_eps_estimate"] = round(est, 6)
    out["earnings_eps_estimate_abs"] = round(abs(est), 6)
    guidance = detail.get("guidance_summary") if isinstance(detail.get("guidance_summary"), dict) else {}
    timing = str(guidance.get("report_timing") or "UNKNOWN").strip().upper()
    if timing not in _EARNINGS_TIMING_ORD:
        timing = _earnings_timing_from_hour(guidance.get("report_hour_et"))
    out["earnings_report_timing_ord"] = _EARNINGS_TIMING_ORD.get(timing, _EARNINGS_TIMING_ORD["UNKNOWN"])
    tone = str(guidance.get("management_tone") or "not_clear").strip().lower()
    out["earnings_tone_ord"] = _EARNINGS_TONE_ORD.get(tone, 1.5)
    hints = guidance.get("scenario_hints")
    out["earnings_scenario_hints_n"] = float(len(hints)) if isinstance(hints, list) else 0.0
    return out


def load_peer_graph_targets(source_symbol: str, *, limit: int = 12) -> list[tuple[str, float]]:
    sym = str(source_symbol or "").strip().upper()
    if not sym:
        return []
    q = text(
        """
        SELECT UPPER(TRIM(target_ticker)) AS target, weight
        FROM peer_graph_edge
        WHERE UPPER(TRIM(source_ticker)) = :source
        ORDER BY weight DESC, target_ticker
        LIMIT :limit
        """
    )
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(q, {"source": sym, "limit": int(limit)}).all()
    except Exception as e:
        logger.debug("peer_graph_edge unavailable for %s: %s", sym, e)
        return []
    out: list[tuple[str, float]] = []
    for target, weight in rows:
        try:
            w = float(weight) if weight is not None else 0.0
        except (TypeError, ValueError):
            w = 0.0
        out.append((str(target).upper(), w))
    return out


def enrich_features_with_peer_graph(base: Dict[str, Any], *, source_symbol: str) -> Dict[str, Any]:
    out = dict(base)
    edges = load_peer_graph_targets(source_symbol)
    if not edges:
        out["peer_graph_present"] = 0.0
        out["peer_graph_out_degree"] = 0.0
        out["peer_graph_weight_sum"] = 0.0
        return out
    out["peer_graph_present"] = 1.0
    out["peer_graph_out_degree"] = float(len(edges))
    out["peer_graph_weight_sum"] = round(sum(w for _, w in edges), 6)
    return out


def enrich_features_with_peer_momentum(
    base: Dict[str, Any],
    *,
    as_of_d: date,
    peer_targets: list[tuple[str, float]],
    max_peers: int = 8,
) -> Dict[str, Any]:
    out = dict(base)
    rets: list[float] = []
    d_min = as_of_d - timedelta(days=400)
    for sym, _w in peer_targets[: max(1, int(max_peers))]:
        df = load_quotes_window(sym, date_min=d_min, date_max=as_of_d)
        if df.empty:
            continue
        dates = list(df["d"])
        as_of_i = _find_as_of_index(dates, as_of_d)
        if as_of_i is None or as_of_i < 5:
            continue
        closes = df["close"].to_numpy(dtype=float)
        lr = _log_ret_ratio(closes, as_of_i - 5, as_of_i)
        if lr is not None and math.isfinite(lr):
            rets.append(float(lr))
    out["peer_momentum_n"] = float(len(rets))
    out["peer_momentum_mean_5d_log"] = round(float(np.mean(rets)), 6) if rets else 0.0
    out["peer_momentum_max_5d_log"] = round(float(max(rets)), 6) if rets else 0.0
    return out


def build_features_before(
    df: pd.DataFrame,
    *,
    as_of_idx: int,
    event_d: date,
    feature_builder_version: Optional[str] = None,
    symbol: str = "",
    knowledge_base_id: Optional[int] = None,
) -> Dict[str, Any]:
    closes = df["close"].to_numpy(dtype=float)
    dates = list(df["d"])
    i = as_of_idx
    fbv = (feature_builder_version or active_feature_builder_version()).strip()
    out: Dict[str, Any] = {
        "feature_builder_version": fbv,
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
    rsi_val = _rsi_as_of_from_closes(closes, i)
    if rsi_val is not None:
        out["rsi_as_of"] = round(float(rsi_val), 4)
    out["close_as_of"] = round(float(closes[i]), 6)

    if fbv in (FEATURE_BUILDER_VERSION_REGIME, FEATURE_BUILDER_VERSION_EARNINGS):
        as_of_d = dates[i]
        regime = load_market_regime_row(as_of_d)
        out = enrich_features_with_market_regime(out, regime)
    if fbv == FEATURE_BUILDER_VERSION_EARNINGS:
        sym_u = str(symbol or df.attrs.get("symbol") or "").strip().upper()
        detail = load_earnings_detail_for_event(
            sym_u,
            event_d,
            knowledge_base_id=knowledge_base_id,
        )
        out = enrich_features_with_earnings_detail(out, detail)
        peer_edges = load_peer_graph_targets(sym_u)
        out = enrich_features_with_peer_graph(out, source_symbol=sym_u)
        out = enrich_features_with_peer_momentum(
            out,
            as_of_d=dates[i],
            peer_targets=peer_edges,
        )
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
    knowledge_base_id: Optional[int] = None,
    feature_builder_version: Optional[str] = None,
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
    df.attrs["symbol"] = str(symbol or "").strip().upper()

    dates = list(df["d"])
    as_of_i = _find_as_of_index(dates, event_d)
    if as_of_i is None:
        return None, None, None, "no_as_of_before_event"

    if as_of_i < min_past_bars:
        return None, None, None, "insufficient_past_bars"

    fbv = (feature_builder_version or active_feature_builder_version()).strip()
    feats = build_features_before(
        df,
        as_of_idx=as_of_i,
        event_d=event_d,
        feature_builder_version=fbv,
        symbol=symbol,
        knowledge_base_id=knowledge_base_id,
    )
    miss = missing_quote_feature_keys(feats)
    if miss:
        return None, None, None, f"features:{miss}"
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

    kb_id_raw = row.get("knowledge_base_id")
    try:
        kb_id = int(kb_id_raw) if kb_id_raw is not None else None
    except (TypeError, ValueError):
        kb_id = None
    feats, outs, label, reason = compute_row_labeling(
        sym,
        row.get("event_time_et"),
        knowledge_base_id=kb_id,
        horizons=horizons,
    )
    out: Dict[str, Any] = {}
    notes: List[str] = []

    if want_f:
        if feats:
            out["features_before"] = feats
            as_of = feats.get("market_regime_date") or feats.get("as_of_trade_date")
            if as_of:
                try:
                    out["market_regime_date"] = date.fromisoformat(str(as_of)[:10])
                except ValueError:
                    pass
        else:
            notes.append(f"features:{reason}")

    if want_o:
        if outs:
            out["outcomes_after"] = outs
            preserved = str(row.get("label_source") or "").strip()
            if preserved not in ("llm_scenario_v0", "manual"):
                out["final_label"] = label
                out["label_source"] = "auto_quotes_v1"
        else:
            notes.append(f"outcomes:{reason}")

    if not out:
        return {}, ";".join(notes) if notes else "skip_empty"
    return out, ";".join(notes)
