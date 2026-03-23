"""
Дополнительное мнение CatBoost для игры 5m (не меняет правила BUY/HOLD/SELL).

- Инференс только если установлен catboost, есть файл модели и meta.json с порядком признаков.
- При отсутствии пакета/модели/ошибке — поля статуса в payload, без падения пайплайна.

Обучение: scripts/train_game5m_catboost.py → local/models/ (не в git).
"""

from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.cluster_recommend import (
    CORRELATION_CB_FEATURE_KEYS,
    extract_correlation_features_for_5m_entry,
    load_game5m_llm_correlation,
)

logger = logging.getLogger(__name__)

# Числовые признаки (порядок должен совпадать с meta.json после колонки ticker)
NUMERIC_FEATURE_KEYS: Tuple[str, ...] = (
    "session_phase_enc",
    "rsi_5m",
    "momentum_2h_pct",
    "momentum_rth_today_pct",
    "premarket_intraday_momentum_pct",
    "volatility_5m_pct",
    "pullback_from_high_pct",
    "bars_count",
    "momentum_rth_today_bars",
    "prob_up",
    "prob_down",
    "estimated_downside_pct_day",
    "atr_5m_pct",
    "volume_vs_avg_pct",
    "price_to_low5d_ratio",
) + CORRELATION_CB_FEATURE_KEYS


def _phase_to_int(phase: str) -> int:
    p = (phase or "").strip().upper()
    order = (
        "PRE_MARKET",
        "NEAR_OPEN",
        "REGULAR",
        "NEAR_CLOSE",
        "AFTER_HOURS",
        "WEEKEND",
        "HOLIDAY",
    )
    try:
        return float(order.index(p) if p in order else 2)
    except ValueError:
        return 2.0


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return default


def ensure_correlation_features_for_catboost(d5: Dict[str, Any], ticker: str) -> None:
    """
    Дозаполняет cb_corr_* из той же матрицы, что у LLM, если в payload их ещё нет
    (например вызов get_decision_5m без кластерного контекста).
    """
    missing = [k for k in CORRELATION_CB_FEATURE_KEYS if k not in d5]
    if not missing:
        return
    try:
        matrix, _, _ = load_game5m_llm_correlation(days=30)
        from services.ticker_groups import get_tickers_game_5m

        feats = extract_correlation_features_for_5m_entry(
            ticker, matrix, list(get_tickers_game_5m() or []),
        )
        for k in CORRELATION_CB_FEATURE_KEYS:
            if k not in d5:
                d5[k] = feats.get(k, 0.0)
    except Exception as e:
        logger.debug("ensure_correlation_features_for_catboost: %s", e)
        for k in missing:
            d5.setdefault(k, 0.0)


def get_catboost_feature_schema() -> Tuple[List[str], List[int]]:
    """Имена колонок (как в Pool) и индексы категориальных — для meta.json и обучения."""
    colnames = ["ticker"] + list(NUMERIC_FEATURE_KEYS)
    return colnames, [0]


def build_catboost_feature_row(
    ticker: str,
    d5: Dict[str, Any],
    *,
    ensure_correlation: bool = True,
) -> Tuple[List[str], List[Any]]:
    """
    Строка признаков: первая колонка — ticker (категория CatBoost), далее числа в порядке NUMERIC_FEATURE_KEYS.

    ensure_correlation: при False не вызываем load_game5m_llm_correlation (нужно при обучении по
    сохранённому context_json — иначе подмешивается «текущая» матрица, а не на момент входа).
    """
    if ensure_correlation:
        ensure_correlation_features_for_catboost(d5, ticker)
    ms = d5.get("market_session") or {}
    phase = (ms.get("session_phase") or d5.get("session_phase") or "") or ""
    high_5d = _safe_float(d5.get("high_5d"), 0.0)
    low_5d = _safe_float(d5.get("low_5d"), 0.0)
    price = _safe_float(d5.get("price"), 0.0)
    if high_5d > low_5d and price > 0:
        price_to_low5d_ratio = (price - low_5d) / (high_5d - low_5d)
    else:
        price_to_low5d_ratio = 0.5

    numeric_vals: Dict[str, float] = {
        "session_phase_enc": _phase_to_int(phase),
        "rsi_5m": _safe_float(d5.get("rsi_5m")),
        "momentum_2h_pct": _safe_float(d5.get("momentum_2h_pct")),
        "momentum_rth_today_pct": _safe_float(d5.get("momentum_rth_today_pct")),
        "premarket_intraday_momentum_pct": _safe_float(d5.get("premarket_intraday_momentum_pct")),
        "volatility_5m_pct": _safe_float(d5.get("volatility_5m_pct")),
        "pullback_from_high_pct": _safe_float(d5.get("pullback_from_high_pct")),
        "bars_count": _safe_float(d5.get("bars_count")),
        "momentum_rth_today_bars": _safe_float(d5.get("momentum_rth_today_bars")),
        "prob_up": _safe_float(d5.get("prob_up")),
        "prob_down": _safe_float(d5.get("prob_down")),
        "estimated_downside_pct_day": _safe_float(d5.get("estimated_downside_pct_day")),
        "atr_5m_pct": _safe_float(d5.get("atr_5m_pct")),
        "volume_vs_avg_pct": _safe_float(d5.get("volume_vs_avg_pct")),
        "price_to_low5d_ratio": float(price_to_low5d_ratio),
    }
    for ck in CORRELATION_CB_FEATURE_KEYS:
        numeric_vals[ck] = _safe_float(d5.get(ck))

    colnames = ["ticker"] + list(NUMERIC_FEATURE_KEYS)
    row: List[Any] = [str(ticker or "").strip().upper() or "UNKNOWN"]
    for k in NUMERIC_FEATURE_KEYS:
        row.append(numeric_vals[k])
    return colnames, row


@lru_cache(maxsize=16)
def _load_model_bundle(model_path: str, model_mtime: float) -> Tuple[Any, Dict[str, Any]]:
    """Загружает CatBoost + meta.json рядом с .cbm. mtime в ключе кэша — после перезаписи файла подхватится новая модель."""
    from catboost import CatBoostClassifier

    p = Path(model_path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    meta_path = p.with_suffix(".meta.json")
    if not meta_path.is_file():
        raise FileNotFoundError(str(meta_path))
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    model = CatBoostClassifier()
    model.load_model(str(p))
    return model, meta


def attach_catboost_signal(out: Dict[str, Any], ticker: str) -> None:
    """
    Добавляет в out поля catboost_*; не бросает исключений наружу.
    """
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower()
    if raw not in ("1", "true", "yes"):
        out["catboost_signal_status"] = "disabled"
        out["catboost_signal_note"] = "CatBoost выключен (GAME_5M_CATBOOST_ENABLED)."
        return

    try:
        import catboost  # noqa: F401
    except ImportError:
        out["catboost_signal_status"] = "no_package"
        out["catboost_signal_note"] = "Пакет catboost не установлен (pip install catboost)."
        return

    model_path = (get_config_value("GAME_5M_CATBOOST_MODEL_PATH", "") or "").strip()
    if not model_path:
        root = Path(__file__).resolve().parents[1]
        model_path = str(root / "local" / "models" / "game5m_entry_catboost.cbm")
    if not os.path.isfile(model_path):
        out["catboost_signal_status"] = "no_model_file"
        out["catboost_signal_note"] = f"Нет файла модели: {model_path}"
        return

    try:
        try:
            mtime = os.path.getmtime(model_path)
        except OSError:
            mtime = 0.0
        model, meta = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("CatBoost load %s: %s", model_path, e)
        out["catboost_signal_status"] = "load_error"
        out["catboost_signal_note"] = f"Ошибка загрузки модели: {e}"
        return

    expected = meta.get("feature_names")
    cat_idx = meta.get("cat_feature_indices", [0])
    if not expected:
        out["catboost_signal_status"] = "bad_meta"
        out["catboost_signal_note"] = "meta.json без feature_names."
        return

    try:
        colnames, row = build_catboost_feature_row(ticker, out)
        if list(expected) != colnames:
            logger.warning(
                "CatBoost: несовпадение признаков (переобучите модель). meta=%s текущие=%s",
                expected,
                colnames,
            )
            out["catboost_signal_status"] = "feature_mismatch"
            out["catboost_signal_note"] = "Список признаков не совпадает с meta.json — переобучите модель."
            return
        from catboost import Pool

        pool = Pool([row], cat_features=cat_idx)
        proba = model.predict_proba(pool)[0]
        # класс 1 = «хороший» исход (как при обучении)
        p_good = float(proba[1]) if len(proba) > 1 else float(proba[0])
        out["catboost_entry_proba_good"] = round(p_good, 4)
        out["catboost_signal_status"] = "ok"
        out["catboost_signal_note"] = (
            f"CatBoost: оценка P(благоприятный исход по истории)≈{p_good:.2f} — "
            f"только справочно, правила входа не меняются."
        )
        raw_append = (get_config_value("GAME_5M_CATBOOST_APPEND_REASONING", "false") or "false").strip().lower()
        if raw_append in ("1", "true", "yes") and out.get("reasoning"):
            out["reasoning"] = (
                str(out["reasoning"]).rstrip()
                + f" [CatBoost P≈{p_good:.2f}]"
            )
    except Exception as e:
        logger.warning("CatBoost predict: %s", e)
        out["catboost_signal_status"] = "predict_error"
        out["catboost_signal_note"] = str(e)


def finalize_technical_decision_with_catboost(out: Dict[str, Any]) -> None:
    """
    Вызывать в конце get_decision_5m после attach_catboost_signal.

    - technical_decision_core — решение чистых правил (поле decision).
    - technical_decision_effective — то, на что опираются вход и LLM-слой (по GAME_5M_CATBOOST_FUSION).

    Выход из позиции (тейк/стоп/SELL) в кроне должен использовать core, не effective.
    """
    from config_loader import get_config_value

    core = out.get("decision")
    out["technical_decision_core"] = core

    fusion = (get_config_value("GAME_5M_CATBOOST_FUSION", "none") or "none").strip().lower()
    out["catboost_fusion_mode"] = fusion

    effective = core
    note: Optional[str] = None

    if fusion == "hold_if_buy_below_p":
        try:
            p_min = float((get_config_value("GAME_5M_CATBOOST_HOLD_BELOW_P", "0.45") or "0.45").strip())
        except (ValueError, TypeError):
            p_min = 0.45
        st = out.get("catboost_signal_status")
        p = out.get("catboost_entry_proba_good")
        if (
            st == "ok"
            and p is not None
            and core in ("BUY", "STRONG_BUY")
            and float(p) < p_min
        ):
            effective = "HOLD"
            note = f"P={float(p):.2f} < {p_min} (CatBoost) → HOLD"
    elif fusion not in ("none", ""):
        note = f"Неизвестный GAME_5M_CATBOOST_FUSION={fusion!r} — итог = базовое решение"

    out["technical_decision_effective"] = effective
    out["catboost_fusion_note"] = note


def row_from_entry_context_dict(ctx: Dict[str, Any], ticker: str) -> Tuple[List[str], List[Any]]:
    """Для обучения: ctx — распарсенный context_json входа; корреляция только из JSON, без дозаполнения."""
    return build_catboost_feature_row(ticker, ctx, ensure_correlation=False)
