"""
Наивный мультидневной прогноз лог-доходности по дневным свечам (Yahoo 1d) + контекст 5m в точке прогноза.

- Обучение: ridge-регрессия по строкам «на конец дня i» → целевая суммарная лог-доходность
  log(C[i+h]/C[i]) для h ∈ {1,2,3} торговых дней (индекс по ряду close без выходных).
- Признаки (только прошлое относительно i): дневные лаги LR[i], LR[i-1], LR[i-2], среднее LR за 5 дней,
  волатильность (std) дневных LR за 10 дней, накопленная 5-дневная лог-доходность log(C[i]/C[i-5]).
- В точке live-прогноза к вектору признаков добавляются (если переданы) volatility_5m_pct и momentum_2h_pct
  в долях; при обучении эти два столбца заполняются нулями (5m в прошлом по дням здесь не восстанавливаем),
  то есть модель учит «дневную часть», а 5m слегка сдвигает итоговый score только для текущего дня.
- Опционально (БД + GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB): четыре признака из premarket_daily_features
  (gap, return, range, gap_vs_vol в долях) на дату сессии closes.index[i]; см. scripts/ingest_premarket_daily_features.py.

Не заменяет календарь/события; по умолчанию выключено (GAME_5M_MULTIDAY_LR_REG_ENABLED).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

HORIZONS_DEFAULT: Tuple[int, int, int] = (1, 2, 3)


def fetch_daily_close_series(ticker: str, period_days: int = 400) -> Optional[pd.Series]:
    """Дневные close, индекс UTC-naive date ascending."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance не установлен — multiday log-ret forecast пропущен")
        return None
    period_days = max(120, min(int(period_days), 2000))
    try:
        hist = yf.Ticker(ticker).history(period=f"{period_days}d", interval="1d", auto_adjust=False)
    except Exception as e:
        logger.debug("yfinance daily для %s: %s", ticker, e)
        return None
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    s = hist["Close"].astype(float).replace(0, np.nan).dropna()
    if len(s) < 20:
        return None
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s.index = idx.normalize()
    return s.sort_index()


def _ridge_weights(X: np.ndarray, y: np.ndarray, l2: float) -> np.ndarray:
    """w = (X'X + λI)^{-1} X'y, X уже с колонкой констант."""
    n, d = X.shape
    if n < d or n < 2:
        raise ValueError("ridge: недостаточно строк")
    lam = max(float(l2), 1e-8)
    a = X.T @ X + lam * np.eye(d, dtype=float)
    b = X.T @ y
    return np.linalg.solve(a, b)


def _build_feature_row(
    c: np.ndarray,
    lr: np.ndarray,
    i: int,
    *,
    vol_window: int = 10,
    mean_window: int = 5,
) -> Optional[np.ndarray]:
    """
    Признаки на конец дня i (0-based индекс close).
    lr[j] = log(c[j]/c[j-1]) для j>=1, длина n-1 на позициях conceptually 1..n-1 — передаём полный lr длиной n с nan в 0.
    Здесь lr_aligned: lr_aligned[k] = log(c[k]/c[k-1]) if k>0 else nan, массив длины n.
    """
    n = len(c)
    if i < vol_window - 1 or i < mean_window - 1 or i < 5:
        return None
    # lr[k] for k>=1
    if i < 1:
        return None
    f_lag1 = float(lr[i])
    f_lag2 = float(lr[i - 1])
    f_lag3 = float(lr[i - 2])
    sl = i - (mean_window - 1)
    f_mean5 = float(np.nanmean(lr[sl : i + 1]))
    slv = i - (vol_window - 1)
    seg = lr[slv : i + 1]
    if np.nanstd(seg) is np.nan:
        return None
    f_vol10 = float(np.nanstd(seg, ddof=1) if len(seg) > 1 else 0.0)
    if c[i - 5] <= 0 or c[i] <= 0:
        return None
    f_cum5 = float(math.log(c[i] / c[i - 5]))
    base = np.array([1.0, f_lag1, f_lag2, f_lag3, f_mean5, f_vol10, f_cum5], dtype=float)
    if not np.all(np.isfinite(base)):
        return None
    return base


def _aligned_lr(c: np.ndarray) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan, dtype=float)
    for k in range(1, n):
        if c[k] > 0 and c[k - 1] > 0:
            out[k] = math.log(c[k] / c[k - 1])
    return out


def _forecast_multiday_from_daily_series(
    ticker: str,
    s: pd.Series,
    daily_source_label: str,
    *,
    volatility_5m_pct: Optional[float],
    momentum_2h_pct: Optional[float],
    horizons: Sequence[int],
    ridge_lambda: float,
    min_train_rows: int,
    use_intraday_features: bool,
    db_engine: Any,
) -> Optional[Dict[str, Any]]:
    """Полный ridge-прогноз по уже выбранному ряду дневных close (quotes или Yahoo)."""
    c = s.values.astype(float)
    n = len(c)
    lr = _aligned_lr(c)

    def _env_true(key: str, default: str = "false") -> bool:
        try:
            from config_loader import get_config_value as _gcv

            return (_gcv(key, default) or default).strip().lower() in ("1", "true", "yes")
        except Exception:
            return default.strip().lower() in ("1", "true", "yes")

    use_pm = use_news = use_macro = use_sym = False
    pm_df = news_df = macro_df = sym_df = None
    if db_engine is not None:
        use_pm = _env_true("GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB", "true")
        use_news = _env_true("GAME_5M_MULTIDAY_LR_USE_NEWS_DB", "false")
        use_macro = _env_true("GAME_5M_MULTIDAY_LR_USE_MACRO_CALENDAR_DB", "false")
        use_sym = _env_true("GAME_5M_MULTIDAY_LR_USE_SYMBOL_CALENDAR_DB", "false")
        try:
            from services.multiday_lr_pipeline import _load_optional_daily_feature_frames

            pm_df, news_df, macro_df, sym_df = _load_optional_daily_feature_frames(
                db_engine,
                ticker,
                s,
                use_premarket_db=use_pm,
                use_news_db=use_news,
                use_macro_calendar_db=use_macro,
                use_symbol_calendar_db=use_sym,
            )
        except Exception as e:
            logger.debug("optional daily features load %s: %s", ticker, e)

    try:
        from services.multiday_lr_pipeline import build_training_stack, _concat_optional_daily_features

        X_train, targets, _min_i, _max_i, n_pm = build_training_stack(
            s.index,
            c,
            horizons,
            pm_df=pm_df,
            use_premarket=use_pm,
            news_df=news_df,
            use_news=use_news,
            macro_cal_df=macro_df,
            use_macro_calendar=use_macro,
            sym_cal_df=sym_df,
            use_symbol_calendar=use_sym,
        )
    except Exception as e:
        logger.debug("build_training_stack %s: %s", ticker, e)
        X_train, targets, n_pm = None, {}, 0

    if X_train is None or X_train.shape[0] < min_train_rows:
        return None

    last_i = n - 1
    row_pred_base = _build_feature_row(c, lr, last_i, vol_window=10, mean_window=5)
    if row_pred_base is None:
        return None

    v5 = (
        float(volatility_5m_pct) / 100.0
        if use_intraday_features and volatility_5m_pct is not None and math.isfinite(float(volatility_5m_pct))
        else 0.0
    )
    m2 = (
        float(momentum_2h_pct) / 100.0
        if use_intraday_features and momentum_2h_pct is not None and math.isfinite(float(momentum_2h_pct))
        else 0.0
    )
    x_pred = _concat_optional_daily_features(
        row_pred_base,
        s.index[last_i],
        pm_df=pm_df,
        news_df=news_df,
        macro_df=macro_df,
        sym_df=sym_df,
        use_premarket=use_pm,
        use_news=use_news,
        use_macro_calendar=use_macro,
        use_symbol_calendar=use_sym,
        intra2=np.array([v5, m2], dtype=float),
    )

    out_horizons: Dict[str, Any] = {}
    for h in horizons:
        hh = int(h)
        raw_y = targets.get(hh)
        if raw_y is None:
            y = np.array([], dtype=float)
        else:
            y = np.asarray(raw_y, dtype=float)
        if len(y) != X_train.shape[0]:
            continue
        try:
            w = _ridge_weights(X_train, y, ridge_lambda)
        except Exception as e:
            logger.debug("ridge h=%s %s: %s", hh, ticker, e)
            continue
        pred_log = float(x_pred @ w)
        pred_pct = (math.exp(pred_log) - 1.0) * 100.0 if math.isfinite(pred_log) else float("nan")
        resid = y - (X_train @ w)
        rmse = float(math.sqrt(float(np.mean(resid ** 2)))) if len(resid) else None
        out_horizons[str(hh)] = {
            "horizon_trading_days": hh,
            "predicted_log_ret": round(pred_log, 6) if math.isfinite(pred_log) else None,
            "predicted_pct_vs_spot": round(pred_pct, 3) if math.isfinite(pred_pct) else None,
            "train_rmse_log": round(rmse, 6) if rmse is not None and math.isfinite(rmse) else None,
            "n_train": int(X_train.shape[0]),
        }

    if len(out_horizons) != len(tuple(horizons)):
        return None

    last_date = s.index[-1]
    try:
        last_date_s = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10]
    except Exception:
        last_date_s = str(last_date)

    preds_pct: List[float] = []
    for h in horizons:
        cell = out_horizons.get(str(int(h)))
        if not isinstance(cell, dict):
            continue
        p = cell.get("predicted_pct_vs_spot")
        if p is None:
            continue
        try:
            preds_pct.append(float(p))
        except (TypeError, ValueError):
            continue
    bias = "neutral"
    if preds_pct:
        pos = sum(1 for p in preds_pct if p > 0.15)
        neg = sum(1 for p in preds_pct if p < -0.15)
        if pos >= 2 and neg == 0:
            bias = "up"
        elif neg >= 2 and pos == 0:
            bias = "down"

    from services.multiday_lr_pipeline import _feature_names_for_artifact

    fnames = _feature_names_for_artifact(
        use_premarket=use_pm,
        use_news=use_news,
        use_macro_calendar=use_macro,
        use_symbol_calendar=use_sym,
    )
    method = "ridge_daily_lags"
    if use_sym or use_macro or use_news:
        method += "_v3_enriched"
    elif use_pm:
        method += "_pm_db"
    method += "_plus_intraday_tail"

    return {
        "ticker": ticker,
        "daily_close_source": daily_source_label,
        "method": method,
        "daily_last_date": last_date_s,
        "ridge_lambda": float(ridge_lambda),
        "feature_names": fnames,
        "horizons": out_horizons,
        "bias_summary": bias,
        "intraday_used": bool(use_intraday_features and (volatility_5m_pct is not None or momentum_2h_pct is not None)),
        "premarket_db_used": bool(use_pm),
        "news_db_used": bool(use_news),
        "macro_calendar_db_used": bool(use_macro),
        "symbol_calendar_db_used": bool(use_sym),
        "n_features": int(len(x_pred)),
    }


def compute_log_return_multiday_forecast(
    ticker: str,
    *,
    volatility_5m_pct: Optional[float] = None,
    momentum_2h_pct: Optional[float] = None,
    period_days: int = 400,
    horizons: Sequence[int] = HORIZONS_DEFAULT,
    ridge_lambda: float = 1.0,
    min_train_rows: int = 80,
    use_intraday_features: bool = True,
    db_engine: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    Возвращает словарь с прогнозами log-ret и % от spot, либо None при нехватке данных/ошибке.
    db_engine: при наличии и GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB — подмешиваются premarket_daily_features.
    Источник дневных close: env ``GAME_5M_MULTIDAY_LR_DAILY_CLOSE_SOURCE`` = auto | quotes | yahoo (по умолчанию
    **auto**: при ``db_engine`` сначала ``public.quotes``, при нехватке строк — Yahoo; как walk-forward анализатора).
    """
    try:
        from config_loader import get_config_value as _gcv_src

        src_pref = (_gcv_src("GAME_5M_MULTIDAY_LR_DAILY_CLOSE_SOURCE", "auto") or "auto").strip().lower()
    except Exception:
        src_pref = "auto"
    if src_pref not in ("auto", "yahoo", "quotes"):
        src_pref = "auto"

    resolved_src = "none"
    s: Optional[pd.Series] = None
    if src_pref == "yahoo":
        s = fetch_daily_close_series(ticker, period_days=period_days)
        resolved_src = "yahoo" if s is not None else "none"
    else:
        from services.multiday_lr_pipeline import resolve_daily_close_series

        s, resolved_src = resolve_daily_close_series(
            ticker, period_days=period_days, engine=db_engine, source=src_pref
        )
        if (s is None or len(s) < min_train_rows) and src_pref == "auto":
            s_yf = fetch_daily_close_series(ticker, period_days=period_days)
            if s_yf is not None and len(s_yf) >= min_train_rows:
                s, resolved_src = s_yf, "yahoo"
            elif s_yf is not None and (s is None or len(s_yf) > len(s)):
                s, resolved_src = s_yf, "yahoo"

    if s is None or len(s) < min_train_rows:
        logger.debug(
            "multiday_lr_forecast_none ticker=%s reason=short_daily_series n=%s min=%s",
            ticker,
            len(s) if s is not None else None,
            min_train_rows,
        )
        return None

    r = _forecast_multiday_from_daily_series(
        ticker,
        s,
        resolved_src,
        volatility_5m_pct=volatility_5m_pct,
        momentum_2h_pct=momentum_2h_pct,
        horizons=horizons,
        ridge_lambda=ridge_lambda,
        min_train_rows=min_train_rows,
        use_intraday_features=use_intraday_features,
        db_engine=db_engine,
    )
    if r is None and src_pref != "quotes" and resolved_src == "quotes":
        s_yf = fetch_daily_close_series(ticker, period_days=period_days)
        if s_yf is not None and len(s_yf) >= min_train_rows:
            r = _forecast_multiday_from_daily_series(
                ticker,
                s_yf,
                "yahoo",
                volatility_5m_pct=volatility_5m_pct,
                momentum_2h_pct=momentum_2h_pct,
                horizons=horizons,
                ridge_lambda=ridge_lambda,
                min_train_rows=min_train_rows,
                use_intraday_features=use_intraday_features,
                db_engine=db_engine,
            )
            if r is not None:
                r["daily_close_source"] = "yahoo_fallback_after_quotes"
    if r is None:
        logger.warning(
            "multiday_lr_forecast_none ticker=%s after_passes n_daily=%s resolved=%s",
            ticker,
            len(s),
            resolved_src,
        )
    return r


def format_multiday_forecast_one_line(fc: Optional[Dict[str, Any]]) -> str:
    if not fc or not fc.get("horizons"):
        return ""
    parts: List[str] = []
    for key in ("1", "2", "3"):
        h = (fc.get("horizons") or {}).get(key)
        if not isinstance(h, dict):
            continue
        p = h.get("predicted_pct_vs_spot")
        if p is None:
            continue
        try:
            parts.append(f"{key}д:{float(p):+.2f}%")
        except (TypeError, ValueError):
            continue
    if not parts:
        return ""
    bias = (fc.get("bias_summary") or "").strip()
    tail = f" ({bias})" if bias and bias != "neutral" else ""
    return "Мультидневный ridge (дн., 1–3 торг. дня + 5m хвост): " + " ".join(parts) + tail

