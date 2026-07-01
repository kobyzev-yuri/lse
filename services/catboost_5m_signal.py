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


def _catboost_predict_proba_row(
    model: Any,
    meta: Dict[str, Any],
    colnames: List[str],
    row: List[Any],
) -> Tuple[str, Optional[float], str]:
    """Один прогон Pool.predict_proba. Возвращает (status, p_good_or_none, note)."""
    expected = meta.get("feature_names")
    cat_idx = meta.get("cat_feature_indices", [0])
    if not expected:
        return "bad_meta", None, "meta.json без feature_names."
    if list(expected) != colnames:
        logger.warning(
            "CatBoost: несовпадение признаков (переобучите модель). meta=%s текущие=%s",
            expected,
            colnames,
        )
        return "feature_mismatch", None, "Список признаков не совпадает с meta.json — переобучите модель."
    try:
        from catboost import Pool

        pool = Pool([row], cat_features=cat_idx)
        proba = model.predict_proba(pool)[0]
        p_good = float(proba[1]) if len(proba) > 1 else float(proba[0])
        return "ok", round(p_good, 4), ""
    except Exception as e:
        logger.warning("CatBoost predict: %s", e)
        return "predict_error", None, str(e)


def _catboost_runtime_guards() -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    """
    Проверки включения / пакета / файла модели.
    Возвращает (status, note, model_path_or_None, (model, meta)_or_None).
    """
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CATBOOST_ENABLED", "false") or "false").strip().lower()
    if raw not in ("1", "true", "yes"):
        return "disabled", "CatBoost выключен (GAME_5M_CATBOOST_ENABLED).", None, None

    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен (pip install catboost).", None, None

    model_path = (get_config_value("GAME_5M_CATBOOST_MODEL_PATH", "") or "").strip()
    if not model_path:
        root = Path(__file__).resolve().parents[1]
        model_path = str(root / "local" / "models" / "game5m_entry_catboost.cbm")
    if not os.path.isfile(model_path):
        return "no_model_file", f"Нет файла модели: {model_path}", model_path, None

    try:
        try:
            mtime = os.path.getmtime(model_path)
        except OSError:
            mtime = 0.0
        bundle = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("CatBoost load %s: %s", model_path, e)
        return "load_error", f"Ошибка загрузки модели: {e}", model_path, None

    return "ready", "", model_path, bundle


def predict_entry_favorability_from_saved_context(ticker: str, entry_context: Any) -> Dict[str, Any]:
    """
    CatBoost P(благоприятный исход) по сохранённому context_json на BUY.

    Используется анализатором сделок: признаки только из JSON на момент входа
    (``row_from_entry_context_dict`` — без дозаполнения корреляции из текущей матрицы).
    """
    from services.deal_params_5m import normalize_entry_context

    out: Dict[str, Any] = {
        "catboost_signal_status": "skipped",
        "catboost_signal_note": "",
        "catboost_entry_proba_good": None,
    }
    ctx = normalize_entry_context(entry_context)
    if not ctx:
        out["catboost_signal_status"] = "no_entry_context"
        out["catboost_signal_note"] = "Пустой или неразбираемый context_json на BUY."
        return out

    st_g, note_g, _mp, bundle = _catboost_runtime_guards()
    if st_g != "ready" or bundle is None:
        out["catboost_signal_status"] = st_g
        out["catboost_signal_note"] = note_g
        return out

    model, meta = bundle
    try:
        colnames, row = row_from_entry_context_dict(ctx, ticker)
        p_st, p_good, p_note = _catboost_predict_proba_row(model, meta, colnames, row)
        out["catboost_signal_status"] = p_st
        out["catboost_entry_proba_good"] = p_good
        out["catboost_signal_note"] = p_note or (
            f"P(благоприятный исход)≈{p_good:.2f} — по признакам на входе (как при обучении)."
            if p_good is not None
            else ""
        )
    except Exception as e:
        logger.warning("predict_entry_favorability_from_saved_context: %s", e)
        out["catboost_signal_status"] = "predict_error"
        out["catboost_signal_note"] = str(e)
    return out


def catboost_entry_dataset_version() -> str:
    """trade = closed-trade v1 model; bar = entry_bar_v2 triple-barrier model."""
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CATBOOST_DATASET_VERSION", "trade") or "trade").strip().lower()
    if raw in ("bar", "v2", "bar_v2", "entry_bar_v2"):
        return "bar"
    return "trade"


def _default_bar_v2_model_path() -> str:
    from config_loader import get_config_value

    model_path = (get_config_value("GAME_5M_CATBOOST_V2_MODEL_PATH", "") or "").strip()
    if model_path:
        return model_path
    if Path("/app/logs").exists():
        return "/app/logs/ml/models/game5m_entry_catboost_v2.cbm"
    return str(Path(__file__).resolve().parents[1] / "local" / "models" / "game5m_entry_catboost_v2.cbm")


def _bar_v2_log_enabled() -> bool:
    from config_loader import get_config_value

    raw = (get_config_value("GAME_5M_CATBOOST_BAR_V2_LOG_ENABLED", "true") or "true").strip().lower()
    return raw in ("1", "true", "yes")


def _price_to_low5d_ratio(d5: Dict[str, Any]) -> float:
    high_5d = _safe_float(d5.get("high_5d"), 0.0)
    low_5d = _safe_float(d5.get("low_5d"), 0.0)
    price = _safe_float(d5.get("price"), 0.0)
    if high_5d > low_5d and price > 0:
        return (price - low_5d) / (high_5d - low_5d)
    return 0.5


def build_catboost_bar_v2_feature_row(
    ticker: str,
    d5: Dict[str, Any],
    *,
    mode: str = "tech",
) -> Tuple[List[str], List[Any]]:
    """Feature row for bar-level entry v2 (tech + optional T+N+C context)."""
    from services.game5m_entry_bar_dataset import (
        FeatureMode,
        get_bar_train_feature_schema,
        row_from_bar_dataset_dict,
    )

    sym = str(ticker or "").strip().upper()
    feat_mode: FeatureMode = "full" if mode == "full" else "tech"
    row_dict: Dict[str, Any] = {
        "ticker": sym,
        "rsi_5m": d5.get("rsi_5m"),
        "momentum_2h_pct": d5.get("momentum_2h_pct"),
        "momentum_rth_today_pct": d5.get("momentum_rth_today_pct"),
        "volatility_5m_pct": d5.get("volatility_5m_pct"),
        "pullback_from_high_pct": d5.get("pullback_from_high_pct"),
        "bars_count": d5.get("bars_count"),
        "momentum_rth_today_bars": d5.get("momentum_rth_today_bars"),
        "price_to_low5d_ratio": _price_to_low5d_ratio(d5),
        "prob_up": d5.get("prob_up"),
        "prob_down": d5.get("prob_down"),
        "macro_risk_level": d5.get("macro_risk_level"),
        "ndx_gap_pct": d5.get("ndx_gap_pct"),
        "spy_gap_pct": d5.get("spy_gap_pct"),
        "premarket_gap_pct": d5.get("premarket_gap_pct"),
        "llm_sentiment": d5.get("llm_sentiment"),
    }
    if feat_mode == "full":
        ms = d5.get("market_session") or {}
        bar_ts = (
            d5.get("decision_5m_bar_open_et")
            or d5.get("bar_ts_et")
            or ms.get("now_et")
            or ""
        )
        if bar_ts:
            try:
                from services.game5m_ml_context_features import build_entry_context_features

                ctx = build_entry_context_features(
                    ticker=sym,
                    bar_ts_et=str(bar_ts),
                    features=row_dict,
                    entry_context=d5,
                )
                row_dict.update(ctx)
            except Exception as e:
                logger.debug("bar v2 context enrich %s: %s", sym, e)
    colnames, _ = get_bar_train_feature_schema(feat_mode)
    return colnames, row_from_bar_dataset_dict(row_dict, sym, mode=feat_mode)


def _catboost_bar_v2_runtime_guards(*, require_log_flag: bool = True) -> Tuple[str, str, Optional[str], Optional[Tuple[Any, Dict[str, Any]]]]:
    """Load bar v2 model. require_log_flag=False when fusion path uses GAME_5M_CATBOOST_ENABLED."""
    if require_log_flag and not _bar_v2_log_enabled():
        return "disabled", "Bar v2 log_only выключен (GAME_5M_CATBOOST_BAR_V2_LOG_ENABLED).", None, None

    try:
        import catboost  # noqa: F401
    except ImportError:
        return "no_package", "Пакет catboost не установлен (pip install catboost).", None, None

    model_path = _default_bar_v2_model_path()
    if not os.path.isfile(model_path):
        return "no_model_file", f"Нет файла bar v2 модели: {model_path}", model_path, None

    try:
        try:
            mtime = os.path.getmtime(model_path)
        except OSError:
            mtime = 0.0
        bundle = _load_model_bundle(model_path, mtime)
    except Exception as e:
        logger.warning("CatBoost bar v2 load %s: %s", model_path, e)
        return "load_error", f"Ошибка загрузки bar v2 модели: {e}", model_path, None

    return "ready", "", model_path, bundle


def _predict_catboost_bar_v2(
    ticker: str,
    d5: Dict[str, Any],
    *,
    require_log_flag: bool = True,
) -> Tuple[str, Optional[float], str, str]:
    """Returns (status, p_good, note, model_path)."""
    st_g, note_g, model_path, bundle = _catboost_bar_v2_runtime_guards(require_log_flag=require_log_flag)
    if st_g != "ready" or bundle is None:
        return st_g, None, note_g, model_path or ""

    model, meta = bundle
    from services.game5m_entry_bar_dataset import resolve_bar_v2_feature_mode

    feat_mode = resolve_bar_v2_feature_mode(meta)
    colnames, row = build_catboost_bar_v2_feature_row(ticker, d5, mode=feat_mode)
    p_st, p_good, p_note = _catboost_predict_proba_row(model, meta, colnames, row)
    return p_st, p_good, p_note, model_path or ""


def _set_bar_v2_predict_fields(
    out: Dict[str, Any],
    *,
    p_st: str,
    p_good: Optional[float],
    p_note: str,
    fusion_active: bool,
) -> None:
    out["catboost_bar_v2_signal_status"] = p_st
    out["catboost_entry_proba_good_v2"] = p_good
    out["catboost_dataset_version"] = "bar"
    if p_st != "ok":
        out["catboost_bar_v2_signal_note"] = p_note
        return
    if fusion_active:
        out["catboost_bar_v2_signal_note"] = (
            f"CatBoost bar v2 (fusion): P(upper barrier first)≈{p_good:.2f}."
        )
    else:
        out["catboost_bar_v2_signal_note"] = (
            f"CatBoost bar v2 (shadow): P(upper barrier first)≈{p_good:.2f} — log_only."
        )


def attach_catboost_bar_v2_signal(out: Dict[str, Any], ticker: str) -> None:
    """
    Bar-level entry CatBoost v2 telemetry. When fusion already ran via attach_catboost_signal
    (GAME_5M_CATBOOST_DATASET_VERSION=bar), skips duplicate predict.
    """
    out.setdefault("catboost_bar_v2_signal_status", "skipped")
    out.setdefault("catboost_bar_v2_signal_note", "")
    out.setdefault("catboost_entry_proba_good_v2", None)

    if out.get("catboost_bar_v2_signal_status") == "ok" and out.get("catboost_entry_proba_good_v2") is not None:
        return

    try:
        p_st, p_good, p_note, _mp = _predict_catboost_bar_v2(ticker, out, require_log_flag=True)
        _set_bar_v2_predict_fields(out, p_st=p_st, p_good=p_good, p_note=p_note, fusion_active=False)
    except Exception as e:
        logger.warning("CatBoost bar v2 predict: %s", e)
        out["catboost_bar_v2_signal_status"] = "predict_error"
        out["catboost_bar_v2_signal_note"] = str(e)


def attach_catboost_signal(out: Dict[str, Any], ticker: str) -> None:
    """
    Добавляет в out поля catboost_*; не бросает исключений наружу.

    GAME_5M_CATBOOST_DATASET_VERSION=bar → entry_bar_v2 model (triple-barrier y_entry_good).
    """
    from config_loader import get_config_value

    if catboost_entry_dataset_version() == "bar":
        out.setdefault("catboost_signal_status", "skipped")
        out.setdefault("catboost_signal_note", "")
        out.setdefault("catboost_entry_proba_good", None)
        try:
            p_st, p_good, p_note, _mp = _predict_catboost_bar_v2(ticker, out, require_log_flag=False)
            out["catboost_signal_status"] = p_st
            out["catboost_entry_proba_good"] = p_good
            _set_bar_v2_predict_fields(out, p_st=p_st, p_good=p_good, p_note=p_note, fusion_active=True)
            if p_st != "ok":
                out["catboost_signal_note"] = p_note
                return
            out["catboost_signal_note"] = (
                f"CatBoost bar v2: P(upper barrier first)≈{p_good:.2f} — "
                f"метка triple-barrier (y_entry_good)."
            )
            raw_append = (get_config_value("GAME_5M_CATBOOST_APPEND_REASONING", "false") or "false").strip().lower()
            if raw_append in ("1", "true", "yes") and out.get("reasoning"):
                out["reasoning"] = (
                    str(out["reasoning"]).rstrip()
                    + f" [CatBoost bar v2 P≈{p_good:.2f}]"
                )
        except Exception as e:
            logger.warning("CatBoost bar v2 fusion predict: %s", e)
            out["catboost_signal_status"] = "predict_error"
            out["catboost_signal_note"] = str(e)
        return

    st_g, note_g, _mp, bundle = _catboost_runtime_guards()
    if st_g != "ready" or bundle is None:
        out["catboost_signal_status"] = st_g
        out["catboost_signal_note"] = note_g
        return

    model, meta = bundle
    try:
        colnames, row = build_catboost_feature_row(ticker, out)
        p_st, p_good, p_note = _catboost_predict_proba_row(model, meta, colnames, row)
        out["catboost_signal_status"] = p_st
        out["catboost_entry_proba_good"] = p_good
        if p_st != "ok":
            out["catboost_signal_note"] = p_note
            return
        out["catboost_dataset_version"] = "trade"
        out["catboost_signal_note"] = (
            f"CatBoost trade v1: P(net_pnl>0)≈{p_good:.2f} — "
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
    ds_ver = catboost_entry_dataset_version()
    out["catboost_dataset_version"] = out.get("catboost_dataset_version") or ds_ver

    effective = core
    note: Optional[str] = None
    model_label = "bar v2" if ds_ver == "bar" else "trade v1"

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
            note = f"P={float(p):.2f} < {p_min} (CatBoost {model_label}) → HOLD"
    elif fusion not in ("none", ""):
        note = f"Неизвестный GAME_5M_CATBOOST_FUSION={fusion!r} — итог = базовое решение"

    out["technical_decision_effective"] = effective
    out["catboost_fusion_note"] = note


def row_from_entry_context_dict(ctx: Dict[str, Any], ticker: str) -> Tuple[List[str], List[Any]]:
    """Для обучения: ctx — распарсенный context_json входа; корреляция только из JSON, без дозаполнения."""
    return build_catboost_feature_row(ticker, ctx, ensure_correlation=False)
