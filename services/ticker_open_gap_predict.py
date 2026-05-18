# -*- coding: utf-8 -*-
"""
Прогноз гэпа на open RTH по тикеру (OLS: gap_ticker ~ VIX + Forex + нефть).

Факт гэпа — premarket_gap_pct (до open) или rth_open_gap_pct (после 9:30 ET).
Кэш коэффициентов на календарный день ET (refit по дневным Yahoo).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

_COEF_CACHE: Dict[str, Tuple[str, Dict[str, float]]] = {}


def _cfg_bool(key: str, default: bool = True) -> bool:
    from config_loader import get_config_value

    raw = (get_config_value(key, "true" if default else "false") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _cfg_int(key: str, default: int) -> int:
    from config_loader import get_config_value

    try:
        return int((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _et_today_str() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except ImportError:
        return date.today().isoformat()


def _fit_ticker_ols_coefs(ticker: str, *, history_days: int = 400) -> Optional[Dict[str, float]]:
    """Коэффициенты OLS: gap_open(ticker) ~ const + gaps макро-индикаторов."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    cache_key = f"{t}|{history_days}"
    today = _et_today_str()
    if cache_key in _COEF_CACHE and _COEF_CACHE[cache_key][0] == today:
        return _COEF_CACHE[cache_key][1]

    try:
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from scripts.analyze_macro_gap_indicators import _daily_ohlc_panel
    except ImportError:
        logger.debug("ticker_open_gap_predict: analyze_macro_gap_indicators недоступен")
        return None

    from services.macro_premarket_risk import (
        get_macro_forex_tickers,
        get_macro_oil_ticker,
        get_macro_vix_ticker,
    )

    vix_t = get_macro_vix_ticker()
    forex = get_macro_forex_tickers()
    oil_t = get_macro_oil_ticker()
    tickers = [t, vix_t] + list(forex) + [oil_t]
    panels = []
    for sym in tickers:
        p = _daily_ohlc_panel(sym, history_days)
        if p is not None and not p.empty:
            panels.append(p)
    if not panels:
        return None
    import pandas as pd

    df = pd.concat(panels, axis=1, sort=True)
    y_col = f"{t}|gap_open"
    if y_col not in df.columns:
        return None
    x_map: List[Tuple[str, str]] = [(vix_t, "beta_vix")]
    for f in forex:
        key = "beta_gbp" if "GBP" in f else "beta_eur" if "EUR" in f else f"beta_{f.replace('=X', '').lower()}"
        x_map.append((f, key))
    x_map.append((oil_t, "beta_cl"))
    x_cols = [f"{sym}|gap_open" for sym, _ in x_map if f"{sym}|gap_open" in df.columns]
    if not x_cols:
        return None
    sub = df.dropna(subset=[y_col] + x_cols, how="any")
    min_n = _cfg_int("GAME_5M_TICKER_OPEN_GAP_PREDICT_MIN_DAYS", 25)
    if len(sub) < min_n:
        return None
    y = sub[y_col].to_numpy(dtype=float)
    X = np.column_stack([np.ones(len(y)), *(sub[c].to_numpy(dtype=float) for c in x_cols)])
    beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    out: Dict[str, float] = {"const": round(float(beta[0]), 4)}
    for i, c in enumerate(x_cols, start=1):
        sym = c.split("|")[0]
        for s, key in x_map:
            if s == sym and i < len(beta):
                out[key] = round(float(beta[i]), 4)
                break
    _COEF_CACHE[cache_key] = (today, out)
    return out


def _macro_gaps_for_predict(macro_risk: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Текущие gap_pct макро-индикаторов (из evaluate_macro_premarket_risk или live fetch)."""
    gaps: Dict[str, float] = {}
    if isinstance(macro_risk, dict) and macro_risk.get("indicators"):
        for k, v in (macro_risk.get("indicators") or {}).items():
            if isinstance(v, dict) and v.get("gap_pct") is not None:
                try:
                    gaps[str(k)] = float(v["gap_pct"])
                except (TypeError, ValueError):
                    pass
        if gaps:
            return gaps
    try:
        from services.macro_premarket_risk import (
            get_macro_forex_tickers,
            get_macro_oil_ticker,
            get_macro_vix_ticker,
            get_indicator_gap_detail,
        )

        for sym in [get_macro_vix_ticker()] + get_macro_forex_tickers() + [get_macro_oil_ticker()]:
            det = get_indicator_gap_detail(sym)
            g = det.get("gap_pct")
            if g is not None:
                gaps[sym] = float(g)
    except Exception as e:
        logger.debug("ticker_open_gap_predict: macro gaps: %s", e)
    return gaps


def predict_ticker_open_gap_pct(
    ticker: str,
    *,
    macro_risk: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], str]:
    """
  Returns (predicted_pct, source).
  source: ticker_ols | sector_proxy | unavailable
    """
    if not _cfg_bool("GAME_5M_TICKER_OPEN_GAP_PREDICT_ENABLED", True):
        return None, "disabled"
    t = (ticker or "").strip().upper()
    coefs = _fit_ticker_ols_coefs(t)
    gaps = _macro_gaps_for_predict(macro_risk)
    if coefs and gaps:
        try:
            from services.macro_premarket_risk import (
                get_macro_forex_tickers,
                get_macro_oil_ticker,
                get_macro_vix_ticker,
            )

            pred = float(coefs.get("const") or 0.0)
            vix_t = get_macro_vix_ticker()
            if vix_t in gaps and "beta_vix" in coefs:
                pred += float(coefs["beta_vix"]) * gaps[vix_t]
            for f in get_macro_forex_tickers():
                g = gaps.get(f)
                if g is None:
                    continue
                key = "beta_gbp" if "GBP" in f else "beta_eur" if "EUR" in f else None
                if key and key in coefs:
                    pred += float(coefs[key]) * float(g)
            oil_t = get_macro_oil_ticker()
            if oil_t in gaps and "beta_cl" in coefs:
                pred += float(coefs["beta_cl"]) * gaps[oil_t]
            return round(pred, 3), "ticker_ols"
        except Exception as e:
            logger.debug("ticker_open_gap_predict %s: %s", t, e)
    if isinstance(macro_risk, dict) and macro_risk.get("macro_predicted_sector_gap_pct") is not None:
        try:
            return round(float(macro_risk["macro_predicted_sector_gap_pct"]), 3), "sector_proxy"
        except (TypeError, ValueError):
            pass
    return None, "unavailable"


def resolve_ticker_open_gap_fact(
    payload: Dict[str, Any],
) -> Tuple[Optional[float], Optional[str]]:
    """Фактический гэп тикера: open RTH приоритетнее премаркета."""
    if payload.get("rth_open_gap_pct") is not None:
        try:
            return round(float(payload["rth_open_gap_pct"]), 2), "open_9_30_et"
        except (TypeError, ValueError):
            pass
    if payload.get("premarket_gap_pct") is not None:
        try:
            return round(float(payload["premarket_gap_pct"]), 2), "premarket"
        except (TypeError, ValueError):
            pass
    return None, None


def attach_ticker_open_gap_fields(
    out: Dict[str, Any],
    *,
    ticker: str,
    macro_risk: Optional[Dict[str, Any]] = None,
) -> None:
    """Заполняет ticker_open_gap_predicted_pct / ticker_open_gap_fact_pct на карточке."""
    pred, pred_src = predict_ticker_open_gap_pct(ticker, macro_risk=macro_risk)
    fact, fact_basis = resolve_ticker_open_gap_fact(out)
    if pred is not None:
        out["ticker_open_gap_predicted_pct"] = pred
    out["ticker_open_gap_predicted_source"] = pred_src
    if fact is not None:
        out["ticker_open_gap_fact_pct"] = fact
        out["ticker_open_gap_fact_basis"] = fact_basis
