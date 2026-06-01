"""ETA to open-path product-ready + continuous learning status for analyzer."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config_loader import get_config_value

DEFAULT_LOOKBACK_DAYS = 21


def _cfg_int(key: str, default: int) -> int:
    try:
        return int((get_config_value(key) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float((get_config_value(key) or str(default)).strip())
    except (ValueError, TypeError):
        return default


def default_refresh_log_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/last_open_path_ml_refresh.json")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "last_open_path_ml_refresh.json"


def default_history_path(project_root: Path | None = None) -> Path:
    if Path("/app/logs").exists():
        return Path("/app/logs/ml/ml_data_quality/open_path_readiness_history.jsonl")
    root = project_root or Path(__file__).resolve().parents[1]
    return root / "local" / "logs" / "ml_data_quality" / "open_path_readiness_history.jsonl"


def _eta_days(current: float, target: float, increment_in_window: float, lookback_days: int) -> Optional[int]:
    if current >= target:
        return 0
    if lookback_days <= 0 or increment_in_window <= 0:
        return None
    daily_rate = increment_in_window / float(lookback_days)
    if daily_rate <= 1e-9:
        return None
    return int(math.ceil((target - current) / daily_rate))


def _parse_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def collect_open_path_accumulation_window(
    engine: Engine,
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Dict[str, Any]:
    lb = max(7, int(lookback_days))
    since = date.today() - timedelta(days=lb)
    out: Dict[str, Any] = {"lookback_days": lb, "since": since.isoformat()}
    try:
        with engine.connect() as conn:
            pm_recent = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT trade_date)
                        FROM premarket_daily_features
                        WHERE trade_date >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
                or 0
            )
            gap_recent = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_gap_forecast_daily
                        WHERE open_gap_pct IS NOT NULL
                          AND trade_date >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
                or 0
            )
            labels_recent = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM game5m_open_path_labels
                        WHERE label_status = 'ok'
                          AND trade_date >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
                or 0
            )
            label_days_recent = int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT trade_date)
                        FROM game5m_open_path_labels
                        WHERE label_status = 'ok'
                          AND trade_date >= :since
                        """
                    ),
                    {"since": since},
                ).scalar()
                or 0
            )
        out.update(
            {
                "premarket_trading_days_in_window": pm_recent,
                "gap_open_rows_in_window": gap_recent,
                "rule_labels_in_window": labels_recent,
                "label_trading_days_in_window": label_days_recent,
            }
        )
    except Exception as e:
        out["error"] = str(e)
    return out


def _bottleneck(
    *,
    metric_id: str,
    label_ru: str,
    current: float,
    target: float,
    increment_in_window: float,
    lookback_days: int,
    blocking: bool,
) -> Dict[str, Any]:
    gap = max(0.0, float(target) - float(current))
    eta = _eta_days(float(current), float(target), float(increment_in_window), lookback_days)
    return {
        "id": metric_id,
        "label": label_ru,
        "current": current,
        "target": target,
        "gap": round(gap, 2),
        "increment_in_window": increment_in_window,
        "eta_days_calendar": eta,
        "blocking": blocking,
        "ready": gap <= 0,
    }


def estimate_open_path_product_eta(
    *,
    snapshot: Dict[str, Any],
    gates: Dict[str, Any],
    earnings_snapshot: Optional[Dict[str, Any]] = None,
    accumulation: Optional[Dict[str, Any]] = None,
    shadow_aggregate: Optional[Dict[str, Any]] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Dict[str, Any]:
    """Estimate calendar days until overall_open_path_classifier_ready (data bottlenecks)."""
    lb = max(7, int(lookback_days))
    acc = accumulation or {}
    od = snapshot.get("open_path_data") if isinstance(snapshot.get("open_path_data"), dict) else {}
    op_ds = snapshot.get("open_path_classifier_dataset") if isinstance(snapshot.get("open_path_classifier_dataset"), dict) else {}
    ei_lf = (earnings_snapshot or {}).get("labels_and_features") if isinstance((earnings_snapshot or {}).get("labels_and_features"), dict) else {}
    sh_agg = shadow_aggregate if isinstance(shadow_aggregate, dict) else {}

    min_pm = _cfg_int("OPEN_PATH_MVP_MIN_PREMARKET_TRADING_DAYS", 60)
    min_gap = _cfg_int("OPEN_PATH_MVP_MIN_GAP_FORECAST_OPEN_ROWS", 120)
    min_e_labels = _cfg_int("ML_READINESS_EARNINGS_AUTOPREP_MIN_LLM_LABELS", 40)
    min_e_shadow = _cfg_int("ML_READINESS_EARNINGS_AUTOPREP_MIN_SHADOW_MATURED", 50)
    min_labels = _cfg_int("ML_READINESS_OPEN_PATH_MIN_TRAIN_ROWS", 200)
    min_shadow = _cfg_int("ML_READINESS_OPEN_PATH_SHADOW_MIN_MATURED", 80)

    pm_days = float(od.get("premarket_feature_trading_days") or 0)
    gap_rows = float(od.get("gap_forecast_open_rows") or 0)
    rule_labels = float(op_ds.get("n_rule_labels") or 0)
    e_labels = float(ei_lf.get("llm_scenario_labels") or 0)
    e_shadow = float(sh_agg.get("n_matured") or 0)
    op_shadow = float((gates.get("open_path_trading_shadow") or {}).get("n_matured") or 0)
    if op_shadow <= 0:
        op_shadow = float(op_ds.get("n_trainable_rows") or 0)

    bottlenecks: List[Dict[str, Any]] = [
        _bottleneck(
            metric_id="premarket_trading_days",
            label_ru="Premarket trading days",
            current=pm_days,
            target=min_pm,
            increment_in_window=float(acc.get("premarket_trading_days_in_window") or 0),
            lookback_days=lb,
            blocking=not bool((gates.get("open_path_mvp_prerequisites") or {}).get("ready")) and pm_days < min_pm,
        ),
        _bottleneck(
            metric_id="gap_open_rows",
            label_ru="Gap open rows",
            current=gap_rows,
            target=min_gap,
            increment_in_window=float(acc.get("gap_open_rows_in_window") or 0),
            lookback_days=lb,
            blocking=gap_rows < min_gap,
        ),
        _bottleneck(
            metric_id="earnings_llm_labels",
            label_ru="Earnings LLM labels (autoprep)",
            current=e_labels,
            target=min_e_labels,
            increment_in_window=max(0.0, e_labels * 0.05),
            lookback_days=lb,
            blocking=not bool((gates.get("open_path_mvp_prerequisites") or {}).get("ready")) and e_labels < min_e_labels,
        ),
        _bottleneck(
            metric_id="earnings_shadow_matured",
            label_ru="Earnings shadow matured (autoprep)",
            current=e_shadow,
            target=min_e_shadow,
            increment_in_window=max(0.0, e_shadow * 0.08),
            lookback_days=lb,
            blocking=not bool((gates.get("open_path_mvp_prerequisites") or {}).get("ready")) and e_shadow < min_e_shadow,
        ),
        _bottleneck(
            metric_id="open_path_rule_labels",
            label_ru="Open-path rule labels",
            current=rule_labels,
            target=min_labels,
            increment_in_window=float(acc.get("rule_labels_in_window") or 0),
            lookback_days=lb,
            blocking=rule_labels < min_labels,
        ),
        _bottleneck(
            metric_id="open_path_shadow_rows",
            label_ru="Open-path shadow rows",
            current=op_shadow,
            target=min_shadow,
            increment_in_window=float(acc.get("rule_labels_in_window") or 0),
            lookback_days=lb,
            blocking=op_shadow < min_shadow,
        ),
    ]

    blocking = [b for b in bottlenecks if b["blocking"] and not b["ready"]]
    etas = [b["eta_days_calendar"] for b in blocking if b.get("eta_days_calendar") is not None]
    max_eta = max(etas) if etas else None

    product_ready = bool(gates.get("overall_open_path_classifier_ready"))
    quality_blockers: list[str] = []
    if not product_ready and not blocking:
        clf = gates.get("open_path_classifier") if isinstance(gates.get("open_path_classifier"), dict) else {}
        sh = gates.get("open_path_trading_shadow") if isinstance(gates.get("open_path_trading_shadow"), dict) else {}
        if not clf.get("ready"):
            quality_blockers.extend(list(clf.get("reasons") or [])[:3])
        if not sh.get("ready"):
            quality_blockers.extend(list(sh.get("reasons") or [])[:3])

    est_date: Optional[str] = None
    if max_eta is not None and max_eta > 0:
        est_date = (date.today() + timedelta(days=max_eta)).isoformat()

    phase = "product_ready" if product_ready else ("quality_tuning" if not blocking else "accumulating_data")
    note_ru = {
        "product_ready": "Product gate закрыт; включено непрерывное дообучение на свежих сессиях.",
        "quality_tuning": "Данных достаточно; ждём метрик train/shadow (качество, не объём).",
        "accumulating_data": "Накопление premarket/gap/labels по cron; ETA — оценка по темпу за lookback.",
    }.get(phase, "")

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "product_ready": product_ready,
        "lookback_days": lb,
        "eta_days_calendar_max": max_eta,
        "eta_date_utc_approx": est_date,
        "note_ru": note_ru,
        "bottlenecks": bottlenecks,
        "blocking_bottlenecks": [b["id"] for b in blocking],
        "quality_blockers": quality_blockers,
        "confidence": "high" if etas else ("low" if blocking else "medium"),
    }


def append_readiness_history(
    *,
    project_root: Path | None,
    metrics: Dict[str, Any],
) -> None:
    path = default_history_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(metrics, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def build_continuous_learning_status(
    *,
    project_root: Path | None,
    product_ready: bool,
    gates: Dict[str, Any],
    train_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    refresh_log = _json_load(default_refresh_log_path(project_root)) or {}
    tm = train_metrics or {}
    mets = tm.get("metrics") if isinstance(tm.get("metrics"), dict) else tm
    trained_at = tm.get("trained_at")
    mode = "continuous_prod" if product_ready else "accumulating"

    continuous_enabled = _cfg_int("OPEN_PATH_ML_CONTINUOUS_TRAIN", 1 if product_ready else 0) == 1
    if product_ready and not continuous_enabled:
        mode = "product_paused"

    return {
        "mode": mode,
        "continuous_train_enabled": continuous_enabled,
        "policy_ru": (
            "После close: label → train каждые 6h + nightly; full+shadow воскресенье. "
            "Модель переобучается на всей доступной истории (walk-forward valid)."
        ),
        "cron": {
            "labels_et": "45 23 * * 1-5 MSK (after US close)",
            "incremental_refresh": "35 */6 * * *",
            "nightly_train": "46 23 * * 1-5 MSK",
            "full_shadow": "48 23 * * 0 MSK",
        },
        "last_refresh": {
            "finished_at_utc": refresh_log.get("finished_at_utc"),
            "apply_data": refresh_log.get("apply_data"),
            "train_ran": refresh_log.get("train_ran"),
            "full": refresh_log.get("full"),
            "labeled_ok": refresh_log.get("labeled_ok"),
            "n_trainable_after": refresh_log.get("n_trainable_after"),
        },
        "last_train": {
            "trained_at_utc": trained_at,
            "n_train": mets.get("n_train"),
            "valid_accuracy": mets.get("valid_accuracy"),
            "model_path": tm.get("out_model_path"),
        },
        "gates_snapshot": {
            "product_ready": product_ready,
            "model_ready": bool(gates.get("overall_open_path_classifier_model_ready")),
            "dataset_ready": bool((gates.get("open_path_classifier_dataset") or {}).get("ready")),
            "shadow_ready": bool((gates.get("open_path_trading_shadow") or {}).get("ready")),
        },
        "advisory_only_until": None if product_ready else "overall_open_path_classifier_ready",
    }


def _json_load(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_refresh_log(
    *,
    project_root: Path | None,
    payload: Dict[str, Any],
) -> Path:
    path = default_refresh_log_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), **payload}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path
