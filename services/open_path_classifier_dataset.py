"""Open-path scenario classifier dataset: features, labels, readiness coverage."""
from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.open_path_labels import LABEL_SOURCE, RULE_VERSION

FEATURE_BUILDER_VERSION = "open_path_premarket_v0"
MODEL_VERSION = "open_path_scenario_v0"

NUMERIC_FEATURE_KEYS: tuple[str, ...] = (
    "pm_gap_pct",
    "pm_return_pct",
    "pm_range_pct",
    "pm_gap_vs_vol",
    "pm_volume_log",
    "pm_minutes_until_open",
    "pm_vwap_gap_pct",
    "gf_premarket_gap_pct",
    "gf_pred_sector_gap_pct",
    "gf_pred_ticker_gap_pct",
    "macro_bias_up",
    "macro_risk_avoid",
    "multiday_h1_pct",
)


def open_path_numeric_feature_keys() -> tuple[str, ...]:
    return NUMERIC_FEATURE_KEYS


def collect_open_path_data_counts(engine: Engine) -> dict[str, Any]:
    """Premarket/gap row counts for readiness gates and ETA."""
    out: dict[str, Any] = {}
    try:
        with engine.connect() as conn:
            pm_days = conn.execute(
                text("SELECT COUNT(DISTINCT trade_date) FROM premarket_daily_features")
            ).scalar()
            gap_open_rows = conn.execute(
                text("SELECT COUNT(*) FROM game5m_gap_forecast_daily WHERE open_gap_pct IS NOT NULL")
            ).scalar()
            gap_pm_rows = conn.execute(
                text("SELECT COUNT(*) FROM game5m_gap_forecast_daily WHERE premarket_gap_pct IS NOT NULL")
            ).scalar()
        out = {
            "premarket_feature_trading_days": int(pm_days or 0),
            "gap_forecast_open_rows": int(gap_open_rows or 0),
            "gap_forecast_premarket_rows": int(gap_pm_rows or 0),
        }
    except Exception as e:
        out = {"error": str(e)}
    return out


def _cfg_float(key: str, default: float) -> float:
    try:
        from config_loader import get_config_value

        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _f(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _json_obj(raw: Any) -> dict[str, Any]:
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


def build_features_snapshot(
    *,
    pm_row: Optional[dict[str, Any]],
    gf_row: dict[str, Any],
    multiday_h1_pct: Optional[float] = None,
) -> dict[str, Any]:
    """Pre-open feature vector stored in features_before JSONB."""
    pm = pm_row or {}
    prev_close = _f(pm.get("prev_close"), 0.0)
    pm_last = _f(pm.get("premarket_last"), 0.0)
    pm_vwap = _f(pm.get("premarket_vwap"), 0.0)
    vwap_gap = 0.0
    if prev_close > 0 and pm_vwap > 0:
        vwap_gap = (pm_vwap / prev_close - 1.0) * 100.0
    elif prev_close > 0 and pm_last > 0:
        vwap_gap = (pm_last / prev_close - 1.0) * 100.0

    macro_bias = str(gf_row.get("macro_equity_gap_bias") or "").strip().upper()
    macro_risk = str(gf_row.get("macro_risk_level") or "").strip().upper()
    vol = pm.get("premarket_volume")
    try:
        vol_log = math.log1p(max(0.0, float(vol))) if vol is not None else 0.0
    except (TypeError, ValueError):
        vol_log = 0.0

    rec = {
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "pm_gap_pct": _f(pm.get("premarket_gap_pct")),
        "pm_return_pct": _f(pm.get("premarket_return_pct")),
        "pm_range_pct": _f(pm.get("premarket_range_pct")),
        "pm_gap_vs_vol": _f(pm.get("gap_vs_daily_volatility")),
        "pm_volume_log": round(vol_log, 6),
        "pm_minutes_until_open": _f(pm.get("minutes_until_open")),
        "pm_vwap_gap_pct": round(vwap_gap, 4),
        "gf_premarket_gap_pct": _f(gf_row.get("premarket_gap_pct")),
        "gf_pred_sector_gap_pct": _f(gf_row.get("pred_sector_gap_pct")),
        "gf_pred_ticker_gap_pct": _f(gf_row.get("pred_ticker_gap_pct")),
        "macro_bias_up": 1.0 if macro_bias == "UP" else 0.0,
        "macro_risk_avoid": 1.0 if macro_risk == "AVOID" else 0.0,
        "multiday_h1_pct": _f(multiday_h1_pct),
    }
    return rec


def features_record_from_json(
    features_before: Any,
    *,
    symbol: str,
) -> Optional[dict[str, float]]:
    fb = _json_obj(features_before)
    if not fb or str(fb.get("feature_builder_version") or "") != FEATURE_BUILDER_VERSION:
        return None
    rec: dict[str, float] = {"symbol": symbol.strip().upper()}
    for k in NUMERIC_FEATURE_KEYS:
        rec[k] = _f(fb.get(k))
    return rec


def fetch_rth_close_price(
    engine: Engine,
    *,
    symbol: str,
    trade_date,
) -> tuple[Optional[float], str]:
    """RTH session close: quotes daily close, fallback last market_bars_5m bar."""
    sym = symbol.strip().upper()
    with engine.connect() as conn:
        q_close = conn.execute(
            text(
                """
                SELECT close::double precision
                FROM public.quotes
                WHERE ticker = :sym AND date::date = :td AND close IS NOT NULL AND close > 0
                LIMIT 1
                """
            ),
            {"sym": sym, "td": trade_date},
        ).scalar()
        if q_close is not None:
            return float(q_close), "quotes"

        bar_close = conn.execute(
            text(
                """
                SELECT close::double precision
                FROM public.market_bars_5m
                WHERE symbol = :sym
                  AND (bar_start_utc AT TIME ZONE 'America/New_York')::date = :td
                  AND close IS NOT NULL AND close > 0
                ORDER BY bar_start_utc DESC
                LIMIT 1
                """
            ),
            {"sym": sym, "td": trade_date},
        ).scalar()
        if bar_close is not None:
            return float(bar_close), "market_bars_5m"
    return None, "missing"


def load_open_path_training_frame(
    engine: Engine,
    *,
    since: str = "2026-01-01",
    label_source: str = LABEL_SOURCE,
    feature_builder_version: str = FEATURE_BUILDER_VERSION,
) -> pd.DataFrame:
    q = text(
        """
        SELECT trade_date, symbol, scenario_label, features_before
        FROM game5m_open_path_labels
        WHERE label_status = 'ok'
          AND label_source = :ls
          AND features_before IS NOT NULL
          AND features_before <> '{}'::jsonb
          AND (features_before->>'feature_builder_version') = :fbv
          AND trade_date >= CAST(:since AS date)
        ORDER BY trade_date, symbol
        """
    )
    with engine.connect() as conn:
        df = pd.read_sql(q, conn, params={"ls": label_source, "fbv": feature_builder_version, "since": since[:10]})
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        sym = str(r["symbol"]).strip().upper()
        rec = features_record_from_json(r.get("features_before"), symbol=sym)
        if rec is None:
            continue
        rows.append(
            {
                "trade_date": r["trade_date"],
                "symbol": sym,
                "target_scenario": str(r["scenario_label"]),
                **rec,
            }
        )
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def collect_open_path_classifier_coverage(
    engine: Engine,
    *,
    since: str = "2026-01-01",
    min_class_samples: int = 2,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "label_source": LABEL_SOURCE,
        "feature_builder_version": FEATURE_BUILDER_VERSION,
        "rule_version": RULE_VERSION,
    }
    try:
        with engine.connect() as conn:
            n_labels = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_open_path_labels
                        WHERE label_status = 'ok' AND label_source = :ls
                          AND trade_date >= CAST(:since AS date)
                        """
                    ),
                    {"ls": LABEL_SOURCE, "since": since[:10]},
                ).scalar()
                or 0
            )
            n_missing_close = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_open_path_labels
                        WHERE label_status = 'missing_close'
                          AND trade_date >= CAST(:since AS date)
                        """
                    ),
                    {"since": since[:10]},
                ).scalar()
                or 0
            )
            n_gap_open = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_gap_forecast_daily
                        WHERE open_gap_pct IS NOT NULL
                          AND rth_open_price IS NOT NULL
                          AND trade_date >= CAST(:since AS date)
                        """
                    ),
                    {"since": since[:10]},
                ).scalar()
                or 0
            )
            n_unlabeled = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_gap_forecast_daily g
                        LEFT JOIN game5m_open_path_labels l
                          ON l.trade_date = g.trade_date
                         AND l.symbol = g.symbol
                         AND l.exchange = 'US'
                        WHERE g.open_gap_pct IS NOT NULL
                          AND g.rth_open_price IS NOT NULL
                          AND g.trade_date >= CAST(:since AS date)
                          AND l.trade_date IS NULL
                        """
                    ),
                    {"since": since[:10]},
                ).scalar()
                or 0
            )
            label_no_features = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_open_path_labels
                        WHERE label_status = 'ok'
                          AND label_source = :ls
                          AND trade_date >= CAST(:since AS date)
                          AND (
                            features_before IS NULL
                            OR features_before = '{}'::jsonb
                            OR (features_before->>'feature_builder_version') IS DISTINCT FROM :fbv
                          )
                        """
                    ),
                    {"ls": LABEL_SOURCE, "since": since[:10], "fbv": FEATURE_BUILDER_VERSION},
                ).scalar()
                or 0
            )
    except Exception as e:
        out["error"] = str(e)
        return out

    train_frame = load_open_path_training_frame(engine, since=since)
    n_trainable = int(len(train_frame))
    labels_by_class: dict[str, int] = {}
    labels_by_symbol: dict[str, int] = {}
    if not train_frame.empty:
        labels_by_class = dict(Counter(train_frame["target_scenario"].astype(str)))
        labels_by_symbol = dict(Counter(train_frame["symbol"].astype(str)))

    sparse = sorted(c for c, n in labels_by_class.items() if n < max(1, int(min_class_samples)))
    out.update(
        {
            "n_rule_labels": n_labels,
            "n_trainable_rows": n_trainable,
            "n_classes_distinct": len(labels_by_class),
            "labels_by_class": labels_by_class,
            "labels_by_symbol_top": dict(sorted(labels_by_symbol.items(), key=lambda x: -x[1])[:12]),
            "sparse_classes_below_min_samples": sparse,
            "n_gap_open_unlabeled": n_unlabeled,
            "n_gap_open_rows": n_gap_open,
            "n_missing_close": n_missing_close,
            "labels_without_features": label_no_features,
        }
    )
    return out


def summarize_open_path_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n_rows": 0}
    by_class = Counter(str(r.get("scenario_label") or "") for r in rows)
    return {
        "n_rows": len(rows),
        "n_symbols": len({str(r.get("symbol") or "").upper() for r in rows}),
        "n_trade_dates": len({r.get("trade_date") for r in rows}),
        "labels_by_class": dict(by_class),
    }
