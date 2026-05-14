"""
Блоки анализатора: multiday ridge vs факт (walk-forward OOS) и сводный «арбитр» готовности ML к продакшену.

Используется из trade_effectiveness_analyzer; не в hot path торговли.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sqlalchemy.engine import Engine

from config_loader import get_config_value


def _ridge_lambda_from_config() -> float:
    try:
        return float((get_config_value("GAME_5M_MULTIDAY_LR_REG_RIDGE_LAMBDA", "1.0") or "1.0").strip())
    except (ValueError, TypeError):
        return 1.0


def _use_premarket_from_config() -> bool:
    return (get_config_value("GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB", "true") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _daily_index_on_or_before_entry(dates: pd.DatetimeIndex, entry_ts: Any) -> Optional[int]:
    """Индекс последнего дневного бара с календарной датой <= даты входа (ET)."""
    try:
        et = pd.Timestamp(entry_ts)
        if et.tzinfo is None:
            et = et.tz_localize("UTC")
        d_et = et.tz_convert("America/New_York").date()
    except Exception:
        return None
    last_i: Optional[int] = None
    for i in range(len(dates)):
        di = pd.Timestamp(dates[i]).normalize()
        try:
            dd = di.tz_localize("UTC").tz_convert("America/New_York").date() if di.tzinfo else di.date()
        except Exception:
            dd = di.date() if hasattr(di, "date") else pd.Timestamp(di).date()
        if dd <= d_et:
            last_i = i
        else:
            break
    return last_i


def build_multiday_lr_reality_check(
    engine: Optional[Engine],
    strategy: str,
    *,
    closed_trades: Sequence[Any],
    effects: Sequence[Any],
) -> Dict[str, Any]:
    """
    Walk-forward OOS по тикерам игры 5m + срез «на день входа» vs дневной forward 1d/2d/3d
    (и отдельно realized сделки — другой масштаб, см. trade_rows).
    """
    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Multiday ridge в отчёте привязан к дневным рядам GAME_5M; для портфельной стратегии не считаем.",
        }
    if su not in ("GAME_5M", "ALL"):
        return {"mode": "skipped", "note": f"strategy={strategy!r}: только GAME_5M или ALL."}
    if engine is None:
        return {"mode": "skipped", "note": "Нет подключения к БД (engine)."}

    from services.multiday_lr_pipeline import (
        fetch_daily_close_series_from_quotes,
        walkforward_oos_multiday_single_ticker,
    )
    from services.ticker_groups import get_tickers_game_5m

    tickers = [str(x).strip().upper() for x in get_tickers_game_5m() if str(x).strip()]
    lam = _ridge_lambda_from_config()
    use_pm = _use_premarket_from_config()

    per_ticker: List[Dict[str, Any]] = []

    for t in tickers:
        one = walkforward_oos_multiday_single_ticker(
            engine,
            t,
            ridge_lambda=lam,
            min_train_rows=80,
            stride=5,
            max_eval_points=72,
            use_premarket_db=use_pm,
        )
        per_ticker.append(one)

    pooled_rmse: Dict[str, Optional[float]] = {}
    pooled_sign: Dict[str, Optional[float]] = {}
    pooled_n: Dict[str, int] = {}
    for hk in ("1", "2", "3"):
        rmses: List[float] = []
        signs: List[float] = []
        ns = 0
        for one in per_ticker:
            if one.get("mode") != "ok":
                continue
            ph = one.get("per_horizon") or {}
            b = ph.get(hk) if isinstance(ph, dict) else None
            if not isinstance(b, dict) or not b.get("n_points"):
                continue
            if b.get("rmse_oos_log") is not None:
                try:
                    rmses.append(float(b["rmse_oos_log"]))
                except (TypeError, ValueError):
                    pass
            if b.get("sign_accuracy") is not None:
                try:
                    signs.append(float(b["sign_accuracy"]))
                except (TypeError, ValueError):
                    pass
            ns += int(b.get("n_points") or 0)
        pooled_rmse[hk] = round(float(sum(rmses) / len(rmses)), 6) if rmses else None
        pooled_sign[hk] = round(float(sum(signs) / len(signs)), 4) if signs else None
        pooled_n[hk] = ns

    verdict, rationale = _multiday_walkforward_verdict(pooled_rmse, pooled_sign, pooled_n)

    trade_rows = _multiday_trade_alignment_rows(engine, closed_trades, effects, strategy, lam, use_pm)

    return {
        "mode": "ok",
        "description": (
            "Walk-forward OOS: на каждой контрольной дате end — ridge как в live (последний дневной close, "
            "премаркет при включении), ошибка предсказания log-ret vs факт log(c[end+h]/c[end]) по quotes. "
            "Сделки: дневной forward с дня входа vs realized_pct сделки (интрадей — разные шкалы)."
        ),
        "ridge_lambda_config": lam,
        "tickers_walkforward": tickers,
        "per_ticker_walkforward": per_ticker,
        "pooled_by_horizon": {
            "1": {"mean_rmse_oos_log_across_tickers": pooled_rmse.get("1"), "mean_sign_accuracy": pooled_sign.get("1"), "n_points_sum": pooled_n.get("1")},
            "2": {"mean_rmse_oos_log_across_tickers": pooled_rmse.get("2"), "mean_sign_accuracy": pooled_sign.get("2"), "n_points_sum": pooled_n.get("2")},
            "3": {"mean_rmse_oos_log_across_tickers": pooled_rmse.get("3"), "mean_sign_accuracy": pooled_sign.get("3"), "n_points_sum": pooled_n.get("3")},
        },
        "walkforward_production_verdict": verdict,
        "walkforward_verdict_rationale_ru": rationale,
        "trade_alignment_sample": trade_rows,
    }


def _multiday_walkforward_verdict(
    pooled_rmse: Dict[str, Optional[float]],
    pooled_sign: Dict[str, Optional[float]],
    pooled_n: Dict[str, int],
) -> Tuple[str, str]:
    """Грубый вердикт по средним метрикам по тикерам (не строгая статистика)."""
    n1 = int(pooled_n.get("1") or 0)
    rm1 = pooled_rmse.get("1")
    s1 = pooled_sign.get("1")
    if n1 < 120 or rm1 is None or s1 is None:
        return (
            "caution",
            "Мало суммарных OOS-точек по горизонту 1d или неполные ряды — наращивайте историю quotes / премаркет.",
        )
    if rm1 <= 0.095 and s1 >= 0.52:
        return (
            "ready",
            "Средний OOS RMSE(log) 1d по тикерам и доля верного знака в допустимых порогах — можно рассматривать включение в прод после ручного подтверждения.",
        )
    if rm1 <= 0.12 and s1 >= 0.48:
        return ("caution", "Метрики на грани: полезны для мониторинга; в прод как фактор решения — только с осторожностью.")
    return ("not_ready", "OOS ошибка или знак заметно слабее порога — не включать GAME_5M_MULTIDAY_LR_REG_ENABLED для правил входа.")


def _multiday_trade_alignment_rows(
    engine: Engine,
    closed_trades: Sequence[Any],
    effects: Sequence[Any],
    strategy: str,
    ridge_lambda: float,
    use_premarket_db: bool,
    *,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    from services.multiday_lr_pipeline import (
        build_training_stack,
        fetch_daily_close_series_from_quotes,
        fetch_premarket_features_dataframe,
        _premarket_vec_for_date,
    )
    from services.log_return_multiday_forecast import _aligned_lr, _build_feature_row, _ridge_weights

    horizons = (1, 2, 3)
    max_h = 3
    by_tid: Dict[int, Any] = {}
    for tp in closed_trades:
        try:
            tid = int(getattr(tp, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tid:
            by_tid[tid] = tp

    rows_out: List[Dict[str, Any]] = []
    su = (strategy or "").strip().upper()
    for e in effects:
        if len(rows_out) >= limit:
            break
        tp = by_tid.get(int(e.trade_id))
        if not tp:
            continue
        es = (getattr(tp, "entry_strategy", None) or "").strip()
        if su == "ALL" and es != "GAME_5M":
            continue
        if su == "GAME_5M" and es and es != "GAME_5M":
            continue
        t = str(e.ticker).strip().upper()
        s = fetch_daily_close_series_from_quotes(engine, t, min_date=None)
        if s is None or len(s) < 80:
            continue
        dates = s.index
        c_full = s.values.astype(float)
        idx = _daily_index_on_or_before_entry(dates, e.entry_ts)
        if idx is None or idx + max_h >= len(c_full):
            continue
        sub_dates = dates[: idx + 1]
        sub_c = c_full[: idx + 1]
        pm_full = None
        if use_premarket_db:
            try:
                d0 = dates[0]
                min_d = d0.strftime("%Y-%m-%d") if hasattr(d0, "strftime") else str(d0)[:10]
                pm_full = fetch_premarket_features_dataframe(engine, t, min_date=min_d)
            except Exception:
                pm_full = None
        use_pm = bool(use_premarket_db and pm_full is not None and not pm_full.empty)
        try:
            X, ydict, _, _, n_pm = build_training_stack(
                sub_dates, sub_c, horizons, pm_df=pm_full, use_premarket=use_pm
            )
        except Exception:
            continue
        if X is None or X.shape[0] < 40:
            continue
        lr = _aligned_lr(sub_c)
        last_i = len(sub_c) - 1
        row_base = _build_feature_row(sub_c, lr, last_i, vol_window=10, mean_window=5)
        if row_base is None:
            continue
        pm_live = _premarket_vec_for_date(pm_full, sub_dates[-1]) if n_pm else np.zeros(0, dtype=float)

        intra2 = np.zeros(2, dtype=float)
        if n_pm:
            x_pred = np.concatenate([row_base, pm_live, intra2])
        else:
            x_pred = np.concatenate([row_base, intra2])
        preds: Dict[str, Optional[float]] = {}
        acts: Dict[str, Optional[float]] = {}
        for h in horizons:
            y = ydict.get(int(h))
            if y is None or len(y) != X.shape[0]:
                continue
            try:
                w = _ridge_weights(X, y, float(ridge_lambda))
                preds[str(h)] = round(float(x_pred @ w), 6)
            except Exception:
                preds[str(h)] = None
            if idx + int(h) < len(c_full) and c_full[idx] > 0 and c_full[idx + int(h)] > 0:
                acts[str(h)] = round(float(math.log(c_full[idx + int(h)] / c_full[idx])), 6)
            else:
                acts[str(h)] = None
        rows_out.append(
            {
                "trade_id": e.trade_id,
                "ticker": t,
                "entry_date_et": str(pd.Timestamp(e.entry_ts).tz_convert("America/New_York").date())
                if e.entry_ts is not None
                else None,
                "realized_pct_trade": round(float(e.realized_pct), 4),
                "pred_log_ret_1d": preds.get("1"),
                "pred_log_ret_2d": preds.get("2"),
                "pred_log_ret_3d": preds.get("3"),
                "actual_log_ret_1d": acts.get("1"),
                "actual_log_ret_2d": acts.get("2"),
                "actual_log_ret_3d": acts.get("3"),
            }
        )
    return rows_out


def build_ml_production_arbiter(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Сводная рекомендация по ML в прод: multiday ridge, CatBoost entry, портфельный CatBoost, recovery (файлы/мета).
    Текст — ориентир для оператора; не меняет config.
    """
    lines: List[str] = []
    verdicts: Dict[str, str] = {}

    mlr = report.get("multiday_lr_reality_check") or {}
    if mlr.get("mode") == "ok":
        v = str(mlr.get("walkforward_production_verdict") or "caution")
        verdicts["multiday_ridge"] = v
        lines.append(f"• Multiday ridge (walk-forward OOS): **{v}** — {mlr.get('walkforward_verdict_rationale_ru', '')}")
        ph = mlr.get("pooled_by_horizon") or {}
        b1 = ph.get("1") if isinstance(ph, dict) else {}
        if isinstance(b1, dict):
            lines.append(
                f"  (пул 1d: средний RMSE по тикерам ≈ {b1.get('mean_rmse_oos_log_across_tickers')}, "
                f"средняя доля верного знака ≈ {b1.get('mean_sign_accuracy')}, суммарно точек n≈ {b1.get('n_points_sum')})"
            )
    elif mlr.get("note"):
        verdicts["multiday_ridge"] = "skipped"
        lines.append(f"• Multiday ridge: пропуск — {mlr.get('note')}")

    cb = report.get("catboost_entry_backtest") or {}
    if cb.get("mode") == "game5m_entry_context":
        cal = cb.get("calibration") or {}
        n_ok = int(cb.get("trades_scored_ok") or 0)
        mw, ml = cal.get("mean_p_given_win"), cal.get("mean_p_given_loss")
        if n_ok >= 25 and mw is not None and ml is not None and float(mw) > float(ml) + 0.04:
            verdicts["catboost_entry"] = "ready"
            lines.append(
                f"• CatBoost entry: **ready** — n_ok={n_ok}, средний P|win={mw} > P|loss={ml} (разделение классов на входе)."
            )
        elif n_ok >= 12:
            verdicts["catboost_entry"] = "caution"
            lines.append(f"• CatBoost entry: **caution** — мало пар или слабое разделение P (n_ok={n_ok}).")
        else:
            verdicts["catboost_entry"] = "not_ready"
            lines.append(f"• CatBoost entry: **not_ready** — n_ok={n_ok}; включайте модель и копите закрытия.")
    else:
        verdicts["catboost_entry"] = "skipped"
        lines.append(f"• CatBoost entry: пропуск — {cb.get('note', '—')}")

    pf = report.get("portfolio_catboost_status") or {}
    if isinstance(pf, dict) and pf.get("enabled") and not pf.get("error"):
        ms = pf.get("meta_summary") if isinstance(pf.get("meta_summary"), dict) else {}
        met = ms.get("metrics") if isinstance(ms, dict) else {}
        rm = None
        if isinstance(met, dict):
            rm = met.get("valid_rmse") or met.get("RMSE") or met.get("rmse_valid")
        if rm is not None and float(rm) < 0.09:
            verdicts["portfolio_catboost"] = "ready"
            lines.append(f"• Portfolio CatBoost: **ready** — valid RMSE {rm} в разумном диапазоне (advisory).")
        else:
            verdicts["portfolio_catboost"] = "caution"
            lines.append("• Portfolio CatBoost: **caution** — смотрите meta/RMSE и объём train.")
    else:
        verdicts["portfolio_catboost"] = "off_or_unknown"
        lines.append("• Portfolio CatBoost: выключен или нет метаданных — только справочно.")

    rec = report.get("game5m_recovery_model_status") or {}
    if isinstance(rec, dict) and rec.get("model_file_exists"):
        verdicts["recovery_catboost"] = "artifact_ok"
        lines.append("• Recovery CatBoost: файлы модели на месте; прод-использование в game_5m — отдельное решение.")
    else:
        verdicts["recovery_catboost"] = "no_model"
        lines.append("• Recovery CatBoost: нет .cbm / meta — офлайн-контур.")

    overall = "not_ready"
    if verdicts.get("multiday_ridge") == "ready" and verdicts.get("catboost_entry") in ("ready", "caution"):
        overall = "caution"
    if verdicts.get("multiday_ridge") == "ready" and verdicts.get("catboost_entry") == "ready":
        overall = "ready"
    if verdicts.get("multiday_ridge") == "not_ready" or verdicts.get("catboost_entry") == "not_ready":
        overall = "not_ready"

    rationale = (
        "Итог арбитра: **" + overall + "**. Готовность к прод — только при зелёных OOS/walk-forward и достаточном n; "
        "включение флагов в config.env делайте вручную после проверки этого блока и LLM-сводки."
    )
    lines.append("")
    lines.append(rationale)

    return {
        "overall_verdict": overall,
        "verdicts": verdicts,
        "summary_lines_ru": lines,
        "conclusion_ru": "\n".join(lines),
    }
