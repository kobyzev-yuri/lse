# -*- coding: utf-8 -*-
"""Open-gap forecasts for premarket UI: baseline (PM→open), ML ridge, effective pick."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_METRICS_CACHE: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)


def _cfg_float(key: str, default: float) -> float:
    try:
        from config_loader import get_config_value

        return float((get_config_value(key, str(default)) or str(default)).strip())
    except Exception:
        return default


def _forecast_policy() -> str:
    try:
        from config_loader import get_config_value

        return (
            get_config_value("GAME_5M_OPEN_GAP_FORECAST_POLICY", "auto") or "auto"
        ).strip().lower()
    except Exception:
        return "auto"


def _metrics_json_path() -> Path:
    app = Path("/app/logs/ml/ml_data_quality/last_gap_forecast_metrics.json")
    if app.is_file():
        return app
    local = Path(__file__).resolve().parents[1] / "local" / "logs" / "ml_data_quality" / "last_gap_forecast_metrics.json"
    return local


def load_gap_forecast_metrics(*, max_age_sec: float = 86400.0) -> Optional[Dict[str, Any]]:
    """Cached pooled metrics from last gap_forecast refresh (MAE baseline vs ML)."""
    import time

    global _METRICS_CACHE
    now = time.time()
    if _METRICS_CACHE[1] is not None and (now - _METRICS_CACHE[0]) < max_age_sec:
        return _METRICS_CACHE[1]
    path = _metrics_json_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pooled = data.get("pooled") if isinstance(data, dict) else None
        if isinstance(pooled, dict):
            _METRICS_CACHE = (now, pooled)
            return pooled
    except Exception as e:
        logger.debug("gap forecast metrics: %s", e)
    return None


def _mae_from_metrics_block(block: Any) -> Optional[float]:
    if not isinstance(block, dict):
        return None
    v = block.get("mean_abs_error_pred_pp")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ml_beats_baseline_on_metrics(metrics: Optional[Dict[str, Any]] = None) -> Optional[bool]:
    """True if ticker ML MAE < naive premarket baseline MAE in last analyze run."""
    m = metrics if metrics is not None else load_gap_forecast_metrics()
    if not m:
        return None
    ml_mae = _mae_from_metrics_block(m.get("ticker_v2") or m.get("game_tickers_pooled"))
    base_mae = _mae_from_metrics_block(m.get("premarket_baseline"))
    if ml_mae is None or base_mae is None:
        return None
    return ml_mae < base_mae


def pick_effective_open_gap_pct(
    *,
    baseline_open_gap_pct: Optional[float],
    ml_open_gap_pct: Optional[float],
    policy: Optional[str] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], str, str]:
    """
    Returns (effective_pct, effective_source, policy_used).
    policy: auto | baseline | ml | blend
    """
    pol = (policy or _forecast_policy()).strip().lower()
    if baseline_open_gap_pct is None and ml_open_gap_pct is None:
        return None, "unavailable", pol
    if baseline_open_gap_pct is None:
        return round(float(ml_open_gap_pct), 3), "ml_only", pol  # type: ignore[arg-type]
    if ml_open_gap_pct is None:
        return round(float(baseline_open_gap_pct), 3), "premarket_baseline", pol

    b = float(baseline_open_gap_pct)
    m = float(ml_open_gap_pct)

    if pol == "baseline":
        return round(b, 3), "premarket_baseline", pol
    if pol == "ml":
        return round(m, 3), "ml_open_gap", pol
    if pol == "blend":
        w = max(0.0, min(1.0, _cfg_float("GAME_5M_OPEN_GAP_FORECAST_BLEND_ML_WEIGHT", 0.35)))
        return round((1.0 - w) * b + w * m, 3), "blend_pm_ml", pol

    # auto: prefer baseline unless ML clearly beats it on rolling metrics
    beats = ml_beats_baseline_on_metrics(metrics)
    if beats is True:
        return round(m, 3), "ml_open_gap", pol
    return round(b, 3), "premarket_baseline", pol


def build_open_gap_forecast_fields(
    ticker: str,
    *,
    premarket_gap_pct: Optional[float],
    macro_risk: Optional[Dict[str, Any]] = None,
    pred_sector_gap_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Live open-gap forecasts for web premarket table (before RTH open)."""
    baseline = None
    if premarket_gap_pct is not None:
        try:
            baseline = round(float(premarket_gap_pct), 3)
        except (TypeError, ValueError):
            baseline = None

    ml_open = None
    ml_source = None
    ml_version = None
    try:
        from services.ticker_open_gap_predict import predict_ticker_open_gap_detail

        det = predict_ticker_open_gap_detail(
            ticker,
            macro_risk=macro_risk,
            premarket_gap_pct=premarket_gap_pct,
        )
        ml_open = det.get("predicted_pct")
        ml_source = det.get("source")
        ml_version = det.get("model_version")
    except Exception as e:
        logger.debug("open gap forecast %s: %s", ticker, e)

    if pred_sector_gap_pct is None and isinstance(macro_risk, dict):
        try:
            ps = macro_risk.get("macro_predicted_sector_gap_pct")
            pred_sector_gap_pct = float(ps) if ps is not None else None
        except (TypeError, ValueError):
            pred_sector_gap_pct = None

    metrics = load_gap_forecast_metrics()
    effective, eff_src, pol = pick_effective_open_gap_pct(
        baseline_open_gap_pct=baseline,
        ml_open_gap_pct=float(ml_open) if ml_open is not None else None,
        metrics=metrics,
    )

    ml_mae = _mae_from_metrics_block((metrics or {}).get("ticker_v2"))
    base_mae = _mae_from_metrics_block((metrics or {}).get("premarket_baseline"))

    out: Dict[str, Any] = {
        "baseline_open_gap_pct": baseline,
        "ml_open_gap_pct": ml_open,
        "pred_ticker_gap_pct": ml_open,
        "pred_ticker_source": ml_source,
        "pred_ticker_model_version": ml_version,
        "effective_open_gap_pct": effective,
        "effective_open_gap_source": eff_src,
        "open_gap_forecast_policy": pol,
        "forecast_recalc_live": True,
    }
    if ml_mae is not None:
        out["gap_forecast_ml_mae_pp"] = ml_mae
    if base_mae is not None:
        out["gap_forecast_baseline_mae_pp"] = base_mae
    if ml_mae is not None and base_mae is not None:
        out["gap_forecast_ml_vs_baseline_mae_delta_pp"] = round(ml_mae - base_mae, 4)
    if pred_sector_gap_pct is not None:
        out["pred_sector_gap_pct"] = round(float(pred_sector_gap_pct), 3)
    return out


def format_open_gap_forecast_compact(
    fc: Dict[str, Any],
    *,
    open_gap_pct: Optional[float] = None,
) -> str:
    """Краткая строка: baseline / ML / effective (+ факт open и ML error после 9:30)."""
    parts: List[str] = []
    base = fc.get("baseline_open_gap_pct")
    ml = fc.get("ml_open_gap_pct")
    eff = fc.get("effective_open_gap_pct")
    if base is not None:
        parts.append(f"base→open {float(base):+.2f}%")
    if ml is not None:
        parts.append(f"ML {float(ml):+.2f}%")
    if eff is not None:
        eff_tag = "eff"
        if (fc.get("effective_open_gap_source") or "") == "premarket_baseline":
            eff_tag = "eff=base"
        parts.append(f"{eff_tag} {float(eff):+.2f}%")
    if open_gap_pct is not None:
        parts.append(f"open {float(open_gap_pct):+.2f}%")
        if ml is not None:
            try:
                parts.append(f"ML err {float(open_gap_pct) - float(ml):+.2f}п.п.")
            except (TypeError, ValueError):
                pass
    return ", ".join(parts)


def format_open_gap_forecast_telegram_line(
    ticker: str,
    *,
    premarket_gap_pct: Optional[float],
    premarket_last: Optional[float] = None,
    macro_risk: Optional[Dict[str, Any]] = None,
    open_gap_pct: Optional[float] = None,
    pred_sector_gap_pct: Optional[float] = None,
) -> Optional[str]:
    """Строка для Telegram/cron: PM + прогнозы open (baseline, ML, effective)."""
    t = (ticker or "?").strip().upper()
    if premarket_gap_pct is None and premarket_last is None:
        return None
    try:
        fc = build_open_gap_forecast_fields(
            t,
            premarket_gap_pct=premarket_gap_pct,
            macro_risk=macro_risk,
            pred_sector_gap_pct=pred_sector_gap_pct,
        )
    except Exception as e:
        logger.debug("telegram open gap line %s: %s", t, e)
        return None
    prefix = f"• {t}"
    if premarket_last is not None:
        prefix += f" {float(premarket_last):.2f}"
    chunks: List[str] = []
    if premarket_gap_pct is not None:
        chunks.append(f"PM {float(premarket_gap_pct):+.2f}%")
    fc_txt = format_open_gap_forecast_compact(fc, open_gap_pct=open_gap_pct)
    if fc_txt:
        chunks.append(fc_txt)
    if not chunks:
        return None
    return f"{prefix}: " + ", ".join(chunks)
