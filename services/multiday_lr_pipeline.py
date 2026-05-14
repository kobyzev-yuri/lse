"""
Пайплайн мультидневного ridge по лог-доходностям: данные из БД (quotes, market_bars_5m,
premarket_daily_features), артефакты JSON, readiness для анализатора.

Премаркет (таблица premarket_daily_features, ingest_premarket_daily_features.py):
  на каждую trade_date сессии — gap / return / range / gap_vs_daily_vol (в долях, /100),
  подмешиваются в строку признаков «на конец дня i» по дате closes.index[i].
  Артефакт v2: 7 (дневные) + 4 (премаркет) + 2 (5m хвост live) веса; v1: 7+2 без премаркета.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.log_return_multiday_forecast import (
    HORIZONS_DEFAULT,
    _aligned_lr,
    _build_feature_row,
    _ridge_weights,
    fetch_daily_close_series,
)

logger = logging.getLogger(__name__)

ARTIFACT_VERSION_V1 = 1
ARTIFACT_VERSION_V2 = 2
PREMARKET_N = 4


def multiday_lr_model_dir() -> Path:
    try:
        from config_loader import get_config_value

        raw = (get_config_value("GAME_5M_MULTIDAY_LR_MODEL_DIR", "local/multiday_lr_models") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        raw = "local/multiday_lr_models"
    p = Path(raw)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def artifact_path(ticker: str, model_dir: Optional[Path] = None) -> Path:
    d = model_dir or multiday_lr_model_dir()
    return d / f"{str(ticker).strip().upper()}.json"


def fetch_daily_close_series_from_quotes(
    engine: Engine,
    ticker: str,
    *,
    min_date: Optional[str] = None,
) -> Optional[pd.Series]:
    """Дневные close из public.quotes; индекс date UTC-naive ascending."""
    t = str(ticker).strip().upper()
    sql = """
        SELECT date::date AS d, close::double precision AS close
        FROM public.quotes
        WHERE ticker = :ticker AND close IS NOT NULL AND close > 0
    """
    params: Dict[str, Any] = {"ticker": t}
    if min_date:
        sql += " AND date >= :min_date"
        params["min_date"] = min_date
    sql += " ORDER BY date ASC"
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(sql), conn, params=params)
    except Exception as e:
        logger.warning("quotes daily для %s: %s", t, e)
        return None
    if df is None or df.empty or "d" not in df.columns:
        return None
    df["d"] = pd.to_datetime(df["d"]).dt.normalize()
    s = pd.Series(df["close"].astype(float).values, index=df["d"])
    s = s[~s.index.duplicated(keep="last")]
    if len(s) < 30:
        return None
    return s.sort_index()


def count_5m_bars_recent(engine: Engine, symbol: str, *, days: int = 14) -> Optional[int]:
    sym = str(symbol).strip().upper()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM public.market_bars_5m
                    WHERE symbol = :sym AND bar_start_utc >= (NOW() AT TIME ZONE 'utc') - (:days::text || ' days')::interval
                    """
                ),
                {"sym": sym, "days": str(max(1, min(int(days), 90)))},
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except Exception as e:
        logger.debug("count_5m_bars_recent %s: %s", sym, e)
        return None


def resolve_daily_close_series(
    ticker: str,
    *,
    period_days: int,
    engine: Optional[Engine],
    source: str,
) -> Tuple[Optional[pd.Series], str]:
    """
    source: quotes | yahoo | auto (quotes если engine и есть строки, иначе yahoo).
    Возвращает (series, resolved_source).
    """
    src = (source or "auto").strip().lower()
    if src == "auto":
        if engine is not None:
            sq = fetch_daily_close_series_from_quotes(engine, ticker)
            if sq is not None and len(sq) >= 40:
                return sq, "quotes"
        yf = fetch_daily_close_series(ticker, period_days=period_days)
        return yf, "yahoo" if yf is not None else "none"
    if src == "quotes" and engine is not None:
        sq = fetch_daily_close_series_from_quotes(engine, ticker)
        return sq, "quotes" if sq is not None else "none"
    yf = fetch_daily_close_series(ticker, period_days=period_days)
    return yf, "yahoo" if yf is not None else "none"


def fetch_premarket_features_dataframe(
    engine: Engine,
    symbol: str,
    *,
    min_date: Optional[str] = None,
    exchange: str = "US",
    snapshot_label: str = "latest",
) -> Optional[pd.DataFrame]:
    """
    Строки premarket_daily_features: индекс trade_date (naive), колонки в долях (/100).
    """
    sym = str(symbol).strip().upper()
    sql = """
        SELECT trade_date::date AS d,
               premarket_gap_pct,
               premarket_return_pct,
               premarket_range_pct,
               gap_vs_daily_volatility
        FROM public.premarket_daily_features
        WHERE symbol = :sym AND exchange = :exch AND snapshot_label = :slab
    """
    params: Dict[str, Any] = {"sym": sym, "exch": exchange, "slab": snapshot_label}
    if min_date:
        sql += " AND trade_date >= :min_date"
        params["min_date"] = min_date
    sql += " ORDER BY trade_date ASC"
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(sql), conn, params=params)
    except Exception as e:
        logger.debug("premarket_daily_features %s: %s", sym, e)
        return None
    if df is None or df.empty:
        return None
    df["d"] = pd.to_datetime(df["d"]).dt.normalize()
    df = df.drop_duplicates(subset=["d"], keep="last").set_index("d")
    for col in (
        "premarket_gap_pct",
        "premarket_return_pct",
        "premarket_range_pct",
        "gap_vs_daily_volatility",
    ):
        if col not in df.columns:
            df[col] = 0.0
    out = pd.DataFrame(index=df.index)
    for col, key in zip(
        ("premarket_gap_pct", "premarket_return_pct", "premarket_range_pct", "gap_vs_daily_volatility"),
        ("pm_gap", "pm_ret", "pm_range", "pm_gap_vs_vol"),
    ):
        out[key] = pd.to_numeric(df[col], errors="coerce").fillna(0.0) / 100.0
    return out


def _premarket_vec_for_date(pm: Optional[pd.DataFrame], trade_d: Any) -> np.ndarray:
    z = np.zeros(PREMARKET_N, dtype=float)
    if pm is None or pm.empty:
        return z
    try:
        dt = pd.Timestamp(trade_d).normalize()
    except Exception:
        return z
    try:
        hit = pm.loc[dt]
    except KeyError:
        return z
    except TypeError:
        return z
    try:
        if isinstance(hit, pd.DataFrame):
            hit = hit.iloc[-1]
        if not isinstance(hit, pd.Series):
            hit = pd.Series(hit)
        return np.array(
            [
                float(hit.get("pm_gap", 0) or 0),
                float(hit.get("pm_ret", 0) or 0),
                float(hit.get("pm_range", 0) or 0),
                float(hit.get("pm_gap_vs_vol", 0) or 0),
            ],
            dtype=float,
        )
    except Exception:
        return z


def build_training_stack(
    dates: pd.DatetimeIndex,
    c: np.ndarray,
    horizons: Sequence[int],
    *,
    pm_df: Optional[pd.DataFrame] = None,
    use_premarket: bool = False,
) -> Tuple[Optional[np.ndarray], Dict[int, np.ndarray], int, int, int]:
    """
    Матрица X и y по горизонтам.
    use_premarket True: 7 + 4 (premarket_daily_features по trade_date, иначе нули) + 2 (5m в train — нули).
    use_premarket False: 7 + 2 (как раньше).
    Возвращает (X или None, ydict, min_i, max_i, n_pm_cols).
    """
    lr = _aligned_lr(c)
    n = len(c)
    max_h = max(int(h) for h in horizons)
    min_i = 10
    max_i = n - 1 - max_h
    if max_i < min_i:
        return None, {}, min_i, max_i, 0
    n_pm = PREMARKET_N if use_premarket else 0
    rows: List[np.ndarray] = []
    targets: Dict[int, List[float]] = {int(h): [] for h in horizons}
    for i in range(min_i, max_i + 1):
        row = _build_feature_row(c, lr, i, vol_window=10, mean_window=5)
        if row is None:
            continue
        ok = True
        rt: Dict[int, float] = {}
        for h in horizons:
            hh = int(h)
            j = i + hh
            if j >= n or c[j] <= 0 or c[i] <= 0:
                ok = False
                break
            rt[hh] = float(math.log(c[j] / c[i]))
        if not ok:
            continue
        td = dates[i]
        pm_vec = _premarket_vec_for_date(pm_df, td) if n_pm else np.zeros(0, dtype=float)
        intra2 = np.zeros(2, dtype=float)
        if n_pm:
            full = np.concatenate([row, pm_vec, intra2])
        else:
            full = np.concatenate([row, intra2])
        rows.append(full)
        for h in horizons:
            targets[int(h)].append(rt[int(h)])
    if not rows:
        return None, {}, min_i, max_i, n_pm
    X = np.vstack(rows).astype(float)
    ydict = {int(h): np.array(targets[int(h)], dtype=float) for h in horizons}
    return X, ydict, min_i, max_i, n_pm


def holdout_rmse(X: np.ndarray, y: np.ndarray, l2: float, holdout_frac: float = 0.12) -> float:
    """Последняя доля строк — валидация; RMSE в log-пространстве."""
    n = X.shape[0]
    if n < 25:
        return float("inf")
    hn = max(5, int(round(n * float(holdout_frac))))
    hn = min(hn, n - 10)
    X_tr, y_tr = X[:-hn], y[:-hn]
    X_va, y_va = X[-hn:], y[-hn:]
    w = _ridge_weights(X_tr, y_tr, l2)
    pred = X_va @ w
    return float(math.sqrt(float(np.mean((y_va - pred) ** 2))))


def select_lambda_mean_cv(
    X: np.ndarray,
    y_by_h: Dict[int, np.ndarray],
    lambdas: Sequence[float],
    holdout_frac: float = 0.12,
) -> Tuple[float, Dict[str, Any]]:
    """Средний holdout RMSE по горизонтам — минимизируем по λ."""
    best_lam = float(lambdas[0])
    best_score = float("inf")
    grid_out: List[Dict[str, Any]] = []
    for lam in lambdas:
        scores = []
        for h, y in y_by_h.items():
            if len(y) != X.shape[0]:
                continue
            scores.append(holdout_rmse(X, y, float(lam), holdout_frac=holdout_frac))
        if not scores:
            continue
        m = float(sum(scores) / len(scores))
        grid_out.append({"lambda": float(lam), "mean_holdout_rmse_log": round(m, 6)})
        if m < best_score:
            best_score = m
            best_lam = float(lam)
    return best_lam, {"lambda_grid_cv": grid_out, "selected_lambda": best_lam}


def _feature_names_for_artifact(use_premarket: bool) -> List[str]:
    base = [
        "intercept",
        "lr_lag1",
        "lr_lag2",
        "lr_lag3",
        "lr_mean5d",
        "lr_std10d",
        "log_ret_5d",
    ]
    if use_premarket:
        base.extend(["pm_gap_frac", "pm_ret_frac", "pm_range_frac", "pm_gap_vs_vol_frac"])
    base.extend(["vol_5m_frac", "mom_2h_frac"])
    return base


def fit_artifact_for_ticker(
    ticker: str,
    closes: pd.Series,
    *,
    engine: Optional[Engine] = None,
    use_premarket_db: bool = False,
    horizons: Sequence[int] = HORIZONS_DEFAULT,
    lambda_candidates: Sequence[float] = (0.25, 0.5, 1.0, 2.0, 4.0),
    holdout_frac: float = 0.12,
    training_source: str = "quotes",
    min_train_rows: int = 80,
) -> Optional[Dict[str, Any]]:
    c = closes.astype(float).values
    pm_df: Optional[pd.DataFrame] = None
    use_pm = bool(use_premarket_db and engine is not None)
    if use_pm:
        try:
            d0 = closes.index[0]
            min_d = d0.strftime("%Y-%m-%d") if hasattr(d0, "strftime") else str(d0)[:10]
        except Exception:
            min_d = None
        pm_df = fetch_premarket_features_dataframe(engine, ticker, min_date=min_d)
    X, y_by_h, _, _, n_pm = build_training_stack(
        closes.index, c, horizons, pm_df=pm_df, use_premarket=use_pm
    )
    if X is None or X.shape[0] < min_train_rows:
        return None
    lam, grid_info = select_lambda_mean_cv(X, y_by_h, lambda_candidates, holdout_frac=holdout_frac)
    horizons_out: Dict[str, Any] = {}
    for h in horizons:
        hh = int(h)
        y = y_by_h.get(hh)
        if y is None or len(y) != X.shape[0]:
            continue
        w = _ridge_weights(X, y, lam)
        resid = y - (X @ w)
        rmse_in = float(math.sqrt(float(np.mean(resid ** 2))))
        horizons_out[str(hh)] = {
            "weights": [round(float(x), 8) for x in w.tolist()],
            "train_rmse_in_sample_log": round(rmse_in, 6),
            "n_train": int(X.shape[0]),
        }
    if len(horizons_out) != len(tuple(horizons)):
        return None
    idx0 = closes.index[0]
    idx1 = closes.index[-1]
    try:
        d0 = idx0.strftime("%Y-%m-%d") if hasattr(idx0, "strftime") else str(idx0)[:10]
        d1 = idx1.strftime("%Y-%m-%d") if hasattr(idx1, "strftime") else str(idx1)[:10]
    except Exception:
        d0, d1 = str(idx0), str(idx1)
    art_ver = ARTIFACT_VERSION_V2 if use_pm else ARTIFACT_VERSION_V1
    return {
        "artifact_version": art_ver,
        "ticker": str(ticker).strip().upper(),
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "training": {
            "source": training_source,
            "n_rows": int(len(closes)),
            "first_date": d0,
            "last_date": d1,
            "ridge_lambda": lam,
            "use_premarket_db": use_pm,
            "premarket_rows_loaded": int(len(pm_df)) if pm_df is not None else 0,
            **grid_info,
            "holdout_frac": float(holdout_frac),
            "min_train_rows": int(min_train_rows),
        },
        "horizons": horizons_out,
        "feature_names": _feature_names_for_artifact(use_pm),
    }


def save_artifact(ticker: str, artifact: Dict[str, Any], model_dir: Optional[Path] = None) -> Path:
    d = model_dir or multiday_lr_model_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = artifact_path(ticker, d)
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_artifact(ticker: str, model_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = artifact_path(ticker, model_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("artifact %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        return None
    ver = int(data.get("artifact_version") or 0)
    if ver not in (ARTIFACT_VERSION_V1, ARTIFACT_VERSION_V2):
        return None
    return data


def predict_from_artifact(
    artifact: Dict[str, Any],
    closes: pd.Series,
    *,
    volatility_5m_pct: Optional[float] = None,
    momentum_2h_pct: Optional[float] = None,
    use_intraday_features: bool = True,
    db_engine: Optional[Engine] = None,
    ticker: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    c = closes.astype(float).values
    n = len(c)
    lr = _aligned_lr(c)
    last_i = n - 1
    row_base = _build_feature_row(c, lr, last_i, vol_window=10, mean_window=5)
    if row_base is None:
        return None
    ver = int(artifact.get("artifact_version") or ARTIFACT_VERSION_V1)
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
    pm_live = np.zeros(PREMARKET_N, dtype=float)
    if ver == ARTIFACT_VERSION_V2 and db_engine is not None and ticker:
        try:
            td = closes.index[last_i]
            pm_df = fetch_premarket_features_dataframe(
                db_engine,
                ticker,
                min_date=str(pd.Timestamp(td).normalize())[:10],
            )
            pm_live = _premarket_vec_for_date(pm_df, td)
        except Exception as e:
            logger.debug("premarket live row %s: %s", ticker, e)
    if ver == ARTIFACT_VERSION_V2:
        x_pred = np.concatenate([row_base, pm_live, np.array([v5, m2], dtype=float)])
    else:
        x_pred = np.concatenate([row_base, np.array([v5, m2], dtype=float)])
    horizons_art = artifact.get("horizons") or {}
    out_horizons: Dict[str, Any] = {}
    for key, block in horizons_art.items():
        if not isinstance(block, dict):
            continue
        w_list = block.get("weights")
        if not isinstance(w_list, list) or len(w_list) != len(x_pred):
            continue
        w = np.array([float(x) for x in w_list], dtype=float)
        pred_log = float(x_pred @ w)
        pred_pct = (math.exp(pred_log) - 1.0) * 100.0 if math.isfinite(pred_log) else float("nan")
        out_horizons[str(key)] = {
            "horizon_trading_days": int(key),
            "predicted_log_ret": round(pred_log, 6) if math.isfinite(pred_log) else None,
            "predicted_pct_vs_spot": round(pred_pct, 3) if math.isfinite(pred_pct) else None,
            "train_rmse_in_sample_log": block.get("train_rmse_in_sample_log"),
            "n_train": block.get("n_train"),
        }
    if not out_horizons:
        return None
    idx1 = closes.index[-1]
    try:
        d1 = idx1.strftime("%Y-%m-%d") if hasattr(idx1, "strftime") else str(idx1)[:10]
    except Exception:
        d1 = str(idx1)
    preds_pct: List[float] = []
    for k in ("1", "2", "3"):
        cell = out_horizons.get(k)
        if isinstance(cell, dict) and cell.get("predicted_pct_vs_spot") is not None:
            try:
                preds_pct.append(float(cell["predicted_pct_vs_spot"]))
            except (TypeError, ValueError):
                pass
    bias = "neutral"
    if preds_pct:
        pos = sum(1 for p in preds_pct if p > 0.15)
        neg = sum(1 for p in preds_pct if p < -0.15)
        if pos >= 2 and neg == 0:
            bias = "up"
        elif neg >= 2 and pos == 0:
            bias = "down"
    tr = artifact.get("training") or {}
    method = "artifact_ridge_v2_pm" if ver == ARTIFACT_VERSION_V2 else "artifact_ridge_v1"
    return {
        "ticker": artifact.get("ticker"),
        "method": method,
        "daily_last_date": d1,
        "ridge_lambda": tr.get("ridge_lambda"),
        "artifact_trained_at_utc": artifact.get("trained_at_utc"),
        "artifact_training_source": tr.get("source"),
        "artifact_version": ver,
        "feature_names": artifact.get("feature_names"),
        "horizons": out_horizons,
        "bias_summary": bias,
        "intraday_used": bool(use_intraday_features and (volatility_5m_pct is not None or momentum_2h_pct is not None)),
        "premarket_live_used": bool(ver == ARTIFACT_VERSION_V2 and np.any(np.abs(pm_live) > 1e-12)),
        "n_features": int(len(x_pred)),
        "lambda_grid_cv": tr.get("lambda_grid_cv"),
    }


def build_readiness_report(
    engine: Optional[Engine],
    *,
    tickers: Sequence[str],
    quotes_since: str = "2025-02-01",
    bars_5m_lookback_days: int = 14,
) -> Dict[str, Any]:
    """
    Готовность по «сетке»: объём дневных котировок, 5m в БД, наличие/возраст артефакта, снимок последнего train_summary.
    """
    mdir = multiday_lr_model_dir()
    summary_path = mdir / "train_summary.json"
    last_train: Any = None
    if summary_path.is_file():
        try:
            last_train = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            last_train = {"parse_error": True}

    per: List[Dict[str, Any]] = []
    ready_quotes = 0
    ready_art = 0
    for raw in tickers:
        t = str(raw).strip().upper()
        if not t:
            continue
        nq: Optional[int] = None
        n5: Optional[int] = None
        if engine is not None:
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text(
                            "SELECT COUNT(*)::int FROM public.quotes WHERE ticker = :t AND date >= :d AND close IS NOT NULL"
                        ),
                        {"t": t, "d": quotes_since},
                    ).fetchone()
                    nq = int(row[0]) if row and row[0] is not None else 0
            except Exception:
                nq = None
            n5 = count_5m_bars_recent(engine, t, days=bars_5m_lookback_days)
        ap = artifact_path(t, mdir)
        art_ok = ap.is_file()
        if art_ok:
            ready_art += 1
        try:
            mtime = datetime.fromtimestamp(ap.stat().st_mtime, tz=timezone.utc).isoformat() if art_ok else None
        except Exception:
            mtime = None
        npm: Optional[int] = None
        if engine is not None:
            try:
                with engine.connect() as conn:
                    rowp = conn.execute(
                        text(
                            "SELECT COUNT(*)::int FROM public.premarket_daily_features "
                            "WHERE symbol = :t AND exchange = 'US' AND trade_date >= :d"
                        ),
                        {"t": t, "d": quotes_since},
                    ).fetchone()
                    npm = int(rowp[0]) if rowp and rowp[0] is not None else 0
            except Exception:
                npm = None
        if nq is not None and nq >= 80:
            ready_quotes += 1
        per.append(
            {
                "ticker": t,
                "quotes_rows_since_cutoff": nq,
                "quotes_cutoff_date": quotes_since,
                "bars_5m_last_ndays": n5,
                "premarket_daily_features_rows_since_cutoff": npm,
                "artifact_path": str(ap),
                "artifact_present": art_ok,
                "artifact_mtime_utc": mtime,
            }
        )

    min_q = 80
    grid_ok = bool(last_train and isinstance(last_train, dict) and last_train.get("tickers_ok"))
    return {
        "model_dir": str(mdir),
        "quotes_since": quotes_since,
        "bars_5m_lookback_days": int(bars_5m_lookback_days),
        "min_quotes_rows_recommended": min_q,
        "tickers_checked": len(per),
        "tickers_quotes_ready_ge_min": ready_quotes,
        "tickers_artifact_present": ready_art,
        "readiness_ratio_quotes": round(ready_quotes / len(per), 3) if per else 0.0,
        "readiness_ratio_artifacts": round(ready_art / len(per), 3) if per else 0.0,
        "last_train_summary": last_train,
        "per_ticker": per,
        "insufficient_for_retrain": ready_quotes < max(1, len(per) // 2),
        "note_ru": "Дневные quotes; 5m (market_bars_5m); премаркет (premarket_daily_features, ingest_premarket_daily_features.py). Сетка λ — last_train_summary.",
    }
