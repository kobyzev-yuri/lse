# -*- coding: utf-8 -*-
"""
Прогноз гэпа на open RTH по тикеру.

v2 (GAME_5M_TICKER_OPEN_GAP_MODEL_VERSION): OLS gap(ticker) ~ sector + VIX + Forex + oil;
опциональный blend с премаркет-гэпом тикера. Кэш коэффициентов на календарный день ET.

Факт: premarket_gap_pct (до open) или rth_open_gap_pct (после 9:30 ET).
Телеметрия / арбитр: game5m_gap_forecast_daily + game5m_gap_forecast_arbiter.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

TICKER_GAP_MODEL_VERSION = "v2_sector_premarket_blend"

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


def _cfg_float(key: str, default: float) -> float:
    from config_loader import get_config_value

    try:
        return float((get_config_value(key, str(default)) or str(default)).strip())
    except (TypeError, ValueError):
        return default


def _configured_model_version() -> str:
    from config_loader import get_config_value

    return (
        get_config_value("GAME_5M_TICKER_OPEN_GAP_MODEL_VERSION", TICKER_GAP_MODEL_VERSION)
        or TICKER_GAP_MODEL_VERSION
    ).strip()


def _et_today_str() -> str:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except ImportError:
        return date.today().isoformat()


def _sector_proxy_ticker() -> str:
    from config_loader import get_config_value

    return (get_config_value("GAME_5M_MACRO_SECTOR_PROXY", "SMH") or "SMH").strip().upper()


def _fit_ticker_ols_coefs(ticker: str, *, history_days: int = 400) -> Optional[Dict[str, float]]:
    """OLS: gap_open(ticker) ~ const + gap(sector) + gaps макро-индикаторов."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    if t == _sector_proxy_ticker():
        return None
    cache_key = f"{t}|v2|{history_days}"
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

    sector = _sector_proxy_ticker()
    vix_t = get_macro_vix_ticker()
    forex = get_macro_forex_tickers()
    oil_t = get_macro_oil_ticker()
    tickers = [t, sector, vix_t] + list(forex) + [oil_t]
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
    x_map: List[Tuple[str, str]] = [(sector, "beta_sector")]
    x_map.append((vix_t, "beta_vix"))
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
    beta_flat = np.asarray(beta, dtype=float).ravel()
    if beta_flat.size < 1:
        return None
    out: Dict[str, float] = {"const": round(float(beta_flat[0]), 4), "model_version": 2.0}
    for i, c in enumerate(x_cols, start=1):
        sym = c.split("|")[0]
        for s, key in x_map:
            if s == sym and i < beta_flat.size:
                out[key] = round(float(beta_flat[i]), 4)
                break
    _COEF_CACHE[cache_key] = (today, out)
    return out


def _macro_gaps_for_predict(macro_risk: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Текущие gap_pct макро-индикаторов и сектора (live / evaluate_macro)."""
    gaps: Dict[str, float] = {}
    if isinstance(macro_risk, dict) and macro_risk.get("indicators"):
        for k, v in (macro_risk.get("indicators") or {}).items():
            if isinstance(v, dict) and v.get("gap_pct") is not None:
                try:
                    gaps[str(k)] = float(v["gap_pct"])
                except (TypeError, ValueError):
                    pass
    sector = _sector_proxy_ticker()
    if sector not in gaps and isinstance(macro_risk, dict):
        for det in macro_risk.get("game_5m_gaps") or []:
            if not isinstance(det, dict):
                continue
            if str(det.get("ticker") or "").upper() == sector and det.get("gap_pct") is not None:
                try:
                    gaps[sector] = float(det["gap_pct"])
                except (TypeError, ValueError):
                    pass
                break
    if gaps:
        return gaps
    try:
        from services.macro_premarket_risk import (
            get_indicator_gap_detail,
            get_macro_forex_tickers,
            get_macro_oil_ticker,
            get_macro_vix_ticker,
        )

        for sym in [get_macro_vix_ticker()] + get_macro_forex_tickers() + [get_macro_oil_ticker()]:
            det = get_indicator_gap_detail(sym)
            g = det.get("gap_pct")
            if g is not None:
                gaps[sym] = float(g)
        det_sec = get_indicator_gap_detail(sector)
        if det_sec.get("gap_pct") is not None:
            gaps[sector] = float(det_sec["gap_pct"])
    except Exception as e:
        logger.debug("ticker_open_gap_predict: macro gaps: %s", e)
    return gaps


def _ols_predict_from_coefs(
    coefs: Dict[str, float],
    gaps: Dict[str, float],
) -> Optional[float]:
    from services.macro_premarket_risk import (
        get_macro_forex_tickers,
        get_macro_oil_ticker,
        get_macro_vix_ticker,
    )

    sector = _sector_proxy_ticker()
    pred = float(coefs.get("const") or 0.0)
    if "beta_sector" in coefs and sector in gaps:
        pred += float(coefs["beta_sector"]) * gaps[sector]
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
    return pred


def _blend_premarket(
    pred: float,
    premarket_gap_pct: Optional[float],
) -> Tuple[float, bool]:
    """Blend OLS с наблюдаемым премаркет-гэпом тикера (если |pm| выше порога)."""
    if premarket_gap_pct is None:
        return pred, False
    try:
        pm = float(premarket_gap_pct)
    except (TypeError, ValueError):
        return pred, False
    min_abs = _cfg_float("GAME_5M_TICKER_OPEN_GAP_PREMARKET_BLEND_MIN_ABS", 1.0)
    if abs(pm) < min_abs:
        return pred, False
    w = _cfg_float("GAME_5M_TICKER_OPEN_GAP_PREMARKET_BLEND_WEIGHT", 0.45)
    w = max(0.0, min(1.0, w))
    return round((1.0 - w) * pred + w * pm, 3), True


def predict_ticker_open_gap_pct(
    ticker: str,
    *,
    macro_risk: Optional[Dict[str, Any]] = None,
    premarket_gap_pct: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    detail = predict_ticker_open_gap_detail(
        ticker,
        macro_risk=macro_risk,
        premarket_gap_pct=premarket_gap_pct,
    )
    return detail.get("predicted_pct"), str(detail.get("source") or "unavailable")


def predict_ticker_open_gap_detail(
    ticker: str,
    *,
    macro_risk: Optional[Dict[str, Any]] = None,
    premarket_gap_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Returns normalized prediction details.
    source: ticker_ols_v2 | ticker_ols_v2_premarket_blend | sector_proxy | unavailable
    """
    if not _cfg_bool("GAME_5M_TICKER_OPEN_GAP_PREDICT_ENABLED", True):
        return {"predicted_pct": None, "source": "disabled"}
    t = (ticker or "").strip().upper()
    gaps = _macro_gaps_for_predict(macro_risk)
    pred_sector = None
    if isinstance(macro_risk, dict) and macro_risk.get("macro_predicted_sector_gap_pct") is not None:
        try:
            pred_sector = float(macro_risk["macro_predicted_sector_gap_pct"])
        except (TypeError, ValueError):
            pred_sector = None
    pooled_enabled = _cfg_bool("GAME_5M_PREMARKET_GAP_POOLED_ENABLED", True)
    if pooled_enabled:
        try:
            from services.premarket_gap_model import load_pooled_gap_artifact, predict_from_artifact

            pooled_pred, pooled_meta = predict_from_artifact(
                load_pooled_gap_artifact() or {},
                symbol=t,
                premarket_gap_pct=premarket_gap_pct,
                pred_sector_gap_pct=pred_sector,
            )
            if pooled_pred is not None:
                return {
                    "predicted_pct": pooled_pred,
                    "source": "pooled_ridge_v1",
                    "model_version": pooled_meta.get("model_version"),
                    "confidence": pooled_meta.get("confidence"),
                    "uncertainty_p80_pp": pooled_meta.get("uncertainty_p80_pp"),
                    "n_train": pooled_meta.get("n_train"),
                }
        except Exception as e:
            logger.debug("pooled premarket gap %s: %s", t, e)
    coefs = _fit_ticker_ols_coefs(t)
    if coefs and gaps:
        try:
            raw = _ols_predict_from_coefs(coefs, gaps)
            if raw is not None:
                blended, did_blend = _blend_premarket(float(raw), premarket_gap_pct)
                if did_blend:
                    return {"predicted_pct": blended, "source": "ticker_ols_v2_premarket_blend"}
                return {"predicted_pct": round(float(raw), 3), "source": "ticker_ols_v2"}
        except Exception as e:
            logger.debug("ticker_open_gap_predict %s: %s", t, e)
    if pred_sector is not None:
        try:
            return {"predicted_pct": round(float(pred_sector), 3), "source": "sector_proxy"}
        except (TypeError, ValueError):
            pass
    return {"predicted_pct": None, "source": "unavailable"}


def get_ticker_gap_model_version() -> str:
    return _configured_model_version()


def resolve_ticker_open_gap_fact(
    payload: Dict[str, Any],
    *,
    frozen: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """Фактический гэп тикера: open RTH приоритетнее премаркета."""
    if frozen and frozen.get("open_gap_pct") is not None:
        try:
            return round(float(frozen["open_gap_pct"]), 2), "open_db"
        except (TypeError, ValueError):
            pass
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


def _use_frozen_gap_snapshot(session_phase: Optional[str], frozen: Optional[Dict[str, Any]]) -> bool:
    """После open не пересчитывать OLS без премаркета — брать утренний снапшот из БД."""
    if not frozen or frozen.get("pred_ticker_gap_pct") is None:
        return False
    phase = (session_phase or "").strip().upper()
    if phase == "PRE_MARKET" and frozen.get("open_gap_pct") is None:
        return False
    return True


def _pred_detail_from_frozen(frozen: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "predicted_pct": round(float(frozen["pred_ticker_gap_pct"]), 3),
        "source": str(frozen.get("pred_ticker_source") or "frozen_db"),
        "model_version": frozen.get("pred_ticker_model_version"),
        "frozen_snapshot": True,
    }


def attach_ticker_open_gap_fields(
    out: Dict[str, Any],
    *,
    ticker: str,
    macro_risk: Optional[Dict[str, Any]] = None,
) -> None:
    """Заполняет ticker_open_gap_* на карточке / в get_decision_5m."""
    frozen: Optional[Dict[str, Any]] = None
    try:
        from services.game5m_gap_forecast import load_frozen_gap_snapshot

        frozen = load_frozen_gap_snapshot(ticker)
    except Exception as e:
        logger.debug("frozen_gap_snapshot %s: %s", ticker, e)

    pm = out.get("premarket_gap_pct")
    if pm is None and frozen and frozen.get("premarket_gap_pct") is not None:
        try:
            pm = round(float(frozen["premarket_gap_pct"]), 2)
            out["premarket_gap_pct"] = pm
        except (TypeError, ValueError):
            pm = None

    session_phase = str(out.get("session_phase") or "")
    if _use_frozen_gap_snapshot(session_phase, frozen):
        pred_detail = _pred_detail_from_frozen(frozen)  # type: ignore[arg-type]
    else:
        pred_detail = predict_ticker_open_gap_detail(
            ticker,
            macro_risk=macro_risk,
            premarket_gap_pct=float(pm) if pm is not None else None,
        )

    pred = pred_detail.get("predicted_pct")
    pred_src = pred_detail.get("source")
    if pred is not None:
        out["ticker_open_gap_ml_advisory_pct"] = pred
        out["ticker_open_gap_ml_advisory_source"] = pred_src
    if pm is not None:
        out["ticker_open_gap_observable_baseline_pct"] = round(float(pm), 3)

    fact, fact_basis = resolve_ticker_open_gap_fact(out, frozen=frozen)
    if pred is not None:
        out["ticker_open_gap_predicted_pct"] = pred
    out["ticker_open_gap_predicted_source"] = pred_src
    out["ticker_open_gap_model_version"] = pred_detail.get("model_version") or get_ticker_gap_model_version()
    if pred_detail.get("confidence") is not None:
        out["ticker_open_gap_confidence"] = pred_detail.get("confidence")
    if pred_detail.get("uncertainty_p80_pp") is not None:
        out["ticker_open_gap_uncertainty_p80_pp"] = pred_detail.get("uncertainty_p80_pp")
    if pred_detail.get("n_train") is not None:
        out["ticker_open_gap_model_n_train"] = pred_detail.get("n_train")
    if fact is not None:
        out["ticker_open_gap_fact_pct"] = fact
        out["ticker_open_gap_fact_basis"] = fact_basis
