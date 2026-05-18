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
    return _env_flag_true("GAME_5M_MULTIDAY_LR_USE_PREMARKET_DB", "true")


def _env_flag_true(key: str, default: str = "false") -> bool:
    return (get_config_value(key, default) or default).strip().lower() in ("1", "true", "yes")


# Наборы для walk-forward сравнения (OOS). v3c = macro+symbol вместе; v3mac/v3sym — по отдельности.
MULTIDAY_FEATURE_SET_SPECS: Dict[str, Dict[str, Any]] = {
    "v2": {
        "label_ru": "цена + премаркет (текущий прод по умолчанию)",
        "use_premarket_db": True,
        "use_news_db": False,
        "use_macro_calendar_db": False,
        "use_symbol_calendar_db": False,
    },
    "v3n": {
        "label_ru": "v2 + news_daily_features",
        "use_premarket_db": True,
        "use_news_db": True,
        "use_macro_calendar_db": False,
        "use_symbol_calendar_db": False,
    },
    "v3mac": {
        "label_ru": "v2 + macro_calendar_daily_features",
        "use_premarket_db": True,
        "use_news_db": False,
        "use_macro_calendar_db": True,
        "use_symbol_calendar_db": False,
    },
    "v3sym": {
        "label_ru": "v2 + symbol_calendar_daily_features",
        "use_premarket_db": True,
        "use_news_db": False,
        "use_macro_calendar_db": False,
        "use_symbol_calendar_db": True,
    },
    "v3c": {
        "label_ru": "v2 + macro + symbol (без news)",
        "use_premarket_db": True,
        "use_news_db": False,
        "use_macro_calendar_db": True,
        "use_symbol_calendar_db": True,
    },
    "v3": {
        "label_ru": "полный v3 (news + macro + symbol)",
        "use_premarket_db": True,
        "use_news_db": True,
        "use_macro_calendar_db": True,
        "use_symbol_calendar_db": True,
    },
}

ENV_FLAG_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("GAME_5M_MULTIDAY_LR_USE_NEWS_DB", "v3n", "новости (news_daily_features)"),
    ("GAME_5M_MULTIDAY_LR_USE_MACRO_CALENDAR_DB", "v3mac", "макро-календарь (macro_calendar_daily_features)"),
    ("GAME_5M_MULTIDAY_LR_USE_SYMBOL_CALENDAR_DB", "v3sym", "календарь тикера / earnings (symbol_calendar_daily_features)"),
)


def _live_feature_set_key() -> str:
    news = _env_flag_true("GAME_5M_MULTIDAY_LR_USE_NEWS_DB", "false")
    macro = _env_flag_true("GAME_5M_MULTIDAY_LR_USE_MACRO_CALENDAR_DB", "false")
    sym = _env_flag_true("GAME_5M_MULTIDAY_LR_USE_SYMBOL_CALENDAR_DB", "false")
    if news and macro and sym:
        return "v3"
    if news and not macro and not sym:
        return "v3n"
    if not news and macro and not sym:
        return "v3mac"
    if not news and not macro and sym:
        return "v3sym"
    if not news and macro and sym:
        return "v3c"
    return "v2"


def _pool_walkforward_runs(per_ticker: List[Dict[str, Any]]) -> Dict[str, Any]:
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
                    signs.append(float(float(b["sign_accuracy"])))
                except (TypeError, ValueError):
                    pass
            ns += int(b.get("n_points") or 0)
        pooled_rmse[hk] = round(float(sum(rmses) / len(rmses)), 6) if rmses else None
        pooled_sign[hk] = round(float(sum(signs) / len(signs)), 4) if signs else None
        pooled_n[hk] = ns
    return {
        "pooled_by_horizon": {
            str(h): {
                "mean_rmse_oos_log_across_tickers": pooled_rmse.get(str(h)),
                "mean_sign_accuracy": pooled_sign.get(str(h)),
                "n_points_sum": pooled_n.get(str(h)),
            }
            for h in (1, 2, 3)
        },
        "pooled_rmse": pooled_rmse,
        "pooled_sign": pooled_sign,
        "pooled_n": pooled_n,
    }


def _run_walkforward_for_feature_set(
    engine: Engine,
    tickers: Sequence[str],
    ridge_lambda: float,
    spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    from services.multiday_lr_pipeline import walkforward_oos_multiday_single_ticker

    out: List[Dict[str, Any]] = []
    for t in tickers:
        one = walkforward_oos_multiday_single_ticker(
            engine,
            t,
            ridge_lambda=ridge_lambda,
            min_train_rows=80,
            stride=5,
            max_eval_points=72,
            use_premarket_db=bool(spec.get("use_premarket_db")),
            use_news_db=bool(spec.get("use_news_db")),
            use_macro_calendar_db=bool(spec.get("use_macro_calendar_db")),
            use_symbol_calendar_db=bool(spec.get("use_symbol_calendar_db")),
        )
        out.append(one)
    return out


def _enrichment_tables_populated(engine: Engine) -> bool:
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            n_news = conn.execute(text("SELECT COUNT(*)::int FROM news_daily_features")).scalar()
            n_macro = conn.execute(text("SELECT COUNT(*)::int FROM macro_calendar_daily_features")).scalar()
        return int(n_news or 0) >= 50 and int(n_macro or 0) >= 50
    except Exception:
        return False


def _horizon_better_or_tied(
    baseline_rmse: Optional[float],
    test_rmse: Optional[float],
    baseline_sign: Optional[float],
    test_sign: Optional[float],
    *,
    rmse_tol: float = 1.01,
    sign_tol: float = 0.02,
) -> bool:
    if baseline_rmse is None or test_rmse is None or baseline_sign is None or test_sign is None:
        return False
    if float(test_rmse) > float(baseline_rmse) * rmse_tol:
        return False
    if float(test_sign) < float(baseline_sign) - sign_tol:
        return False
    return True


def _recommend_env_flag(
    v2_pool: Dict[str, Any],
    test_pool: Dict[str, Any],
    env_key: str,
    label_ru: str,
) -> Dict[str, Any]:
    v2n = (v2_pool.get("pooled_n") or {}).get("1") or 0
    if int(v2n) < 120:
        return {
            "env_key": env_key,
            "label_ru": label_ru,
            "config_current": _env_flag_true(env_key, "false"),
            "recommendation": "insufficient_data",
            "rationale_ru": "Мало OOS-точек по v2 (нужна история quotes и ingest feature-таблиц).",
        }
    pr2 = v2_pool.get("pooled_rmse") or {}
    prt = test_pool.get("pooled_rmse") or {}
    ps2 = v2_pool.get("pooled_sign") or {}
    pst = test_pool.get("pooled_sign") or {}
    wins = 0
    deltas: Dict[str, Optional[float]] = {}
    for h in ("1", "2", "3"):
        b_rm, t_rm = pr2.get(h), prt.get(h)
        b_sg, t_sg = ps2.get(h), pst.get(h)
        if b_rm is not None and t_rm is not None:
            deltas[h] = round(float(t_rm) - float(b_rm), 6)
        if _horizon_better_or_tied(b_rm, t_rm, b_sg, t_sg):
            wins += 1
    cur = _env_flag_true(env_key, "false")
    if wins >= 2:
        rec = "try_true" if not cur else "keep_true"
        rat = (
            f"OOS: на {wins}/3 горизонтах RMSE не хуже v2 (допуск 1%) и знак не просел. "
            f"ΔRMSE 1d/2d/3d: {deltas.get('1')}/{deltas.get('2')}/{deltas.get('3')}."
        )
    elif wins == 1:
        rec = "caution"
        rat = (
            f"Смешанный OOS (лучше только на части горизонтов). ΔRMSE: {deltas}. "
            "Включайте флаг вручную и перепроверьте на карточках."
        )
    else:
        rec = "keep_false" if not cur else "try_false"
        rat = (
            f"OOS не лучше v2 на 2+ горизонтах (ΔRMSE 1d/2d/3d: {deltas.get('1')}/{deltas.get('2')}/{deltas.get('3')}). "
            "Оставить false."
        )
    return {
        "env_key": env_key,
        "label_ru": label_ru,
        "config_current": cur,
        "recommendation": rec,
        "rationale_ru": rat,
        "horizons_better_count": wins,
        "rmse_delta_vs_v2": deltas,
    }


def _build_feature_set_comparison(
    pools: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    v2 = pools.get("v2") or {}
    v2_rm = v2.get("pooled_rmse") or {}
    rows: List[Dict[str, Any]] = []
    for key, pool in pools.items():
        if key == "v2":
            continue
        pr = pool.get("pooled_rmse") or {}
        row = {"feature_set": key, "label_ru": MULTIDAY_FEATURE_SET_SPECS.get(key, {}).get("label_ru", key)}
        for h in ("1", "2", "3"):
            b, t = v2_rm.get(h), pr.get(h)
            if b is not None and t is not None:
                row[f"rmse_{h}d"] = t
                row[f"rmse_delta_vs_v2_{h}d"] = round(float(t) - float(b), 6)
        rows.append(row)
    full = pools.get("v3") or {}
    summary = "v2"
    if full:
        v3_wins = sum(
            1
            for h in ("1", "2", "3")
            if _horizon_better_or_tied(
                v2_rm.get(h),
                (full.get("pooled_rmse") or {}).get(h),
                (v2.get("pooled_sign") or {}).get(h),
                (full.get("pooled_sign") or {}).get(h),
            )
        )
        if v3_wins >= 2:
            summary = "v3_better_oos"
        elif v3_wins == 0:
            summary = "v2_better_oos"
        else:
            summary = "mixed"
    return {"vs_v2": rows, "summary_ru": summary}


def _build_multiday_env_recommendations(
    pools: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    v2 = pools.get("v2") or {}
    items: List[Dict[str, Any]] = []
    for env_key, test_key, label in ENV_FLAG_SPECS:
        test_pool = pools.get(test_key) or {}
        items.append(_recommend_env_flag(v2, test_pool, env_key, label))
    v3_pool = pools.get("v3") or {}
    full_rec = _recommend_env_flag(
        v2,
        v3_pool,
        "GAME_5M_MULTIDAY_LR_USE_ALL_ENRICHMENT",
        "все три флага enrichment вместе (v3)",
    )
    lines: List[str] = []
    for it in items:
        if it.get("env_key") == "GAME_5M_MULTIDAY_LR_USE_ALL_ENRICHMENT":
            continue
        cur = "true" if it.get("config_current") else "false"
        lines.append(
            f"• `{it.get('env_key')}` (сейчас {cur}): **{it.get('recommendation')}** — {it.get('rationale_ru')}"
        )
    lines.append(
        f"• Полный v3 (все флаги): **{full_rec.get('recommendation')}** — {full_rec.get('rationale_ru')}"
    )
    return {
        "flags": items,
        "full_v3": full_rec,
        "summary_lines_ru": lines,
        "note_ru": "Рекомендации не меняют config.env автоматически. После true — restart lse-bot.",
    }


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

    При заполненных таблицах enrichment — сравнение v2 vs v3n/v3mac/v3sym/v3 и рекомендации env-флагов.
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

    from services.ticker_groups import get_tickers_game_5m

    tickers = [str(x).strip().upper() for x in get_tickers_game_5m() if str(x).strip()]
    lam = _ridge_lambda_from_config()
    use_pm = _use_premarket_from_config()
    live_key = _live_feature_set_key()
    compare_all = _env_flag_true("GAME_5M_MULTIDAY_LR_ANALYZER_COMPARE_FEATURE_SETS", "true")
    enrichment_ready = _enrichment_tables_populated(engine)

    pools: Dict[str, Dict[str, Any]] = {}
    per_ticker_by_set: Dict[str, List[Dict[str, Any]]] = {}

    pt_v2 = _run_walkforward_for_feature_set(engine, tickers, lam, MULTIDAY_FEATURE_SET_SPECS["v2"])
    per_ticker_by_set["v2"] = pt_v2
    pools["v2"] = _pool_walkforward_runs(pt_v2)

    comparison_note: Optional[str] = None
    if compare_all and enrichment_ready:
        for key in ("v3n", "v3mac", "v3sym", "v3"):
            spec = MULTIDAY_FEATURE_SET_SPECS[key]
            pt = _run_walkforward_for_feature_set(engine, tickers, lam, spec)
            per_ticker_by_set[key] = pt
            pools[key] = _pool_walkforward_runs(pt)
    elif not enrichment_ready:
        comparison_note = (
            "Сравнение v2/v3 не запущено: мало строк в news_daily_features / macro_calendar_daily_features "
            "(запустите ingest_* и migrate 023–025)."
        )
    elif not compare_all:
        comparison_note = "Сравнение наборов отключено (GAME_5M_MULTIDAY_LR_ANALYZER_COMPARE_FEATURE_SETS=false)."

    active_spec = MULTIDAY_FEATURE_SET_SPECS.get(live_key) or MULTIDAY_FEATURE_SET_SPECS["v2"]
    if live_key not in per_ticker_by_set:
        pt_live = _run_walkforward_for_feature_set(engine, tickers, lam, active_spec)
        per_ticker_by_set[live_key] = pt_live
        pools[live_key] = _pool_walkforward_runs(pt_live)

    per_ticker = per_ticker_by_set.get(live_key) or per_ticker_by_set["v2"]
    active_pool = pools.get(live_key) or pools["v2"]
    pooled_rmse = dict(active_pool.get("pooled_rmse") or {})
    pooled_sign = dict(active_pool.get("pooled_sign") or {})
    pooled_n = dict(active_pool.get("pooled_n") or {})

    verdict, rationale = _multiday_walkforward_verdict(pooled_rmse, pooled_sign, pooled_n)
    if live_key != "v2" and rationale:
        rationale = f"{rationale} (вердикт по активному набору {live_key}: {active_spec.get('label_ru', live_key)}.)"

    trade_rows = _multiday_trade_alignment_rows(engine, closed_trades, effects, strategy, lam, use_pm)

    env_recs: Dict[str, Any] = {"note_ru": comparison_note or "Нет сравнения с v2 — рекомендации по флагам недоступны."}
    feature_comparison: Dict[str, Any] = {}
    if len(pools) >= 2 and "v2" in pools:
        env_recs = _build_multiday_env_recommendations(pools)
        if comparison_note:
            env_recs["comparison_skipped_note_ru"] = comparison_note
        feature_comparison = _build_feature_set_comparison(pools)

    pooled_by_horizon = (active_pool.get("pooled_by_horizon") or {}).copy()
    if not pooled_by_horizon:
        pooled_by_horizon = {
            str(h): {
                "mean_rmse_oos_log_across_tickers": pooled_rmse.get(str(h)),
                "mean_sign_accuracy": pooled_sign.get(str(h)),
                "n_points_sum": pooled_n.get(str(h)),
            }
            for h in (1, 2, 3)
        }

    return {
        "mode": "ok",
        "description": (
            "Walk-forward OOS: на каждой контрольной дате end — ridge как в live (последний дневной close, "
            "премаркет при включении), ошибка предсказания log-ret vs факт log(c[end+h]/c[end]) по quotes. "
            "При ingest enrichment — сравнение v2/v3* и multiday_env_recommendations для GAME_5M_MULTIDAY_LR_USE_*_DB. "
            "Сделки: дневной forward с дня входа vs realized_pct сделки (интрадей — разные шкалы)."
        ),
        "ridge_lambda_config": lam,
        "tickers_walkforward": tickers,
        "active_feature_set": live_key,
        "active_feature_set_label_ru": active_spec.get("label_ru", live_key),
        "config_flags": {
            "premarket": use_pm,
            "news": _env_flag_true("GAME_5M_MULTIDAY_LR_USE_NEWS_DB", "false"),
            "macro_calendar": _env_flag_true("GAME_5M_MULTIDAY_LR_USE_MACRO_CALENDAR_DB", "false"),
            "symbol_calendar": _env_flag_true("GAME_5M_MULTIDAY_LR_USE_SYMBOL_CALENDAR_DB", "false"),
        },
        "per_ticker_walkforward": per_ticker,
        "pooled_by_horizon": pooled_by_horizon,
        "walkforward_production_verdict": verdict,
        "walkforward_verdict_rationale_ru": rationale,
        "multiday_lr_feature_comparison": feature_comparison,
        "multiday_env_recommendations": env_recs,
        "pooled_by_feature_set": {
            k: v.get("pooled_by_horizon") for k, v in pools.items() if isinstance(v, dict)
        },
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


def _as_bool(v: Any) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes")


def _mean_pct(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def build_multiday_lr_gates_arbiter(
    report: Dict[str, Any],
    *,
    strategy: str,
    closed_trades: Sequence[Any],
    effects: Sequence[Any],
) -> Dict[str, Any]:
    """
    Арбитр log-only гейтов multiday ridge: достаточность выборки телеметрии и следующие шаги (config вручную).

    Смотрит BUY context_json (entry gate) и SELL exit_context_json (hold gate на TIME_EXIT_EARLY).
    """
    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Гейты multiday ridge относятся к GAME_5M; для портфеля не считаем.",
        }
    if su not in ("GAME_5M", "ALL"):
        return {"mode": "skipped", "note": f"strategy={strategy!r}: только GAME_5M или ALL."}

    from config_loader import get_config_value
    from services.deal_params_5m import normalize_entry_context

    min_buy_ok = 12
    min_would_hold = 6
    min_early_telemetry = 8
    min_would_defer = 5
    pnl_edge_pp = 0.20

    entry_mode = (get_config_value("GAME_5M_MULTIDAY_ENTRY_GATE_MODE", "none") or "none").strip().lower()
    hold_mode = (get_config_value("GAME_5M_MULTIDAY_HOLD_GATE_MODE", "none") or "none").strip().lower()
    reg_on = (get_config_value("GAME_5M_MULTIDAY_LR_REG_ENABLED", "false") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    by_tid: Dict[int, Any] = {}
    for t in closed_trades:
        try:
            tid = int(getattr(t, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tid:
            by_tid[tid] = t

    pnl_hold: List[float] = []
    pnl_pass: List[float] = []
    n_buy = 0
    n_forecast = 0
    n_gate_ok = 0
    n_would_hold = 0

    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp:
            continue
        ctx = normalize_entry_context(getattr(tp, "context_json", None))
        if not ctx:
            continue
        n_buy += 1
        if ctx.get("multiday_lr_horizon_1d_pct_vs_spot") is not None or ctx.get("multiday_lr_entry_gate_status"):
            n_forecast += 1
        st = (ctx.get("multiday_lr_entry_gate_status") or "").strip().lower()
        if st == "ok":
            n_gate_ok += 1
            wh = _as_bool(ctx.get("multiday_lr_entry_gate_would_hold"))
            rp = float(e.realized_pct)
            if wh:
                n_would_hold += 1
                pnl_hold.append(rp)
            else:
                pnl_pass.append(rp)

    pnl_defer: List[float] = []
    pnl_no_defer: List[float] = []
    n_early = 0
    n_hold_telemetry = 0
    n_would_defer = 0

    for e in effects:
        if str(e.exit_signal or "").upper() != "TIME_EXIT_EARLY":
            continue
        n_early += 1
        tp = by_tid.get(int(e.trade_id))
        if not tp:
            continue
        raw_exit = getattr(tp, "exit_context_json", None)
        if isinstance(raw_exit, str):
            try:
                import json

                exit_ctx = json.loads(raw_exit) if raw_exit.strip() else {}
            except Exception:
                exit_ctx = {}
        elif isinstance(raw_exit, dict):
            exit_ctx = raw_exit
        else:
            exit_ctx = {}
        hg = exit_ctx.get("multiday_lr_hold_gate")
        if not isinstance(hg, dict):
            continue
        n_hold_telemetry += 1
        rp = float(e.realized_pct)
        if _as_bool(hg.get("would_defer_exit")):
            n_would_defer += 1
            pnl_defer.append(rp)
        else:
            pnl_no_defer.append(rp)

    mlr = report.get("multiday_lr_reality_check") or {}
    wf_verdict = str(mlr.get("walkforward_production_verdict") or "unknown") if mlr.get("mode") == "ok" else "unknown"

    def _entry_verdict() -> Tuple[str, str, List[str]]:
        steps: List[str] = []
        if not reg_on:
            steps.append("Включите GAME_5M_MULTIDAY_LR_REG_ENABLED=true (прогноз на карточке).")
            return "not_configured", "Регрессия multiday выключена в config.", steps
        if entry_mode == "none":
            steps.append("Задайте GAME_5M_MULTIDAY_ENTRY_GATE_MODE=log_only для накопления телеметрии.")
            return "not_configured", "Гейт входа не включён (mode=none).", steps
        if n_forecast == 0:
            steps.append("Проверьте деплой multiday_lr_gate.py и перезапуск lse-bot после BUY.")
            steps.append("Дождитесь новых BUY с полями multiday_lr_* в context_json.")
            return "insufficient_data", "Нет BUY с multiday-полями в окне отчёта.", steps
        if n_gate_ok < min_buy_ok:
            need = min_buy_ok - n_gate_ok
            steps.append(f"Копите log_only: нужно ещё ≥{need} BUY со status=ok (сейчас {n_gate_ok}/{min_buy_ok}).")
            return "insufficient_data", f"Мало BUY с гейтом ok: {n_gate_ok} < {min_buy_ok}.", steps
        if n_would_hold < min_would_hold:
            need = min_would_hold - n_would_hold
            steps.append(f"Нужно ещё ≥{need} случаев would_hold для оценки фильтра входа.")
            return "insufficient_data", f"Мало would_hold: {n_would_hold} < {min_would_hold}.", steps

        mh, mp = _mean_pct(pnl_hold), _mean_pct(pnl_pass)
        if mh is not None and mp is not None and mh > mp + pnl_edge_pp:
            steps.append("Не включать ENTRY apply: would_hold сделки в среднем не хуже остальных.")
            steps.append("Пересмотрите GAME_5M_MULTIDAY_ENTRY_TAU_* или кворум NEGATIVE_HORIZONS_MIN.")
            return "caution", f"would_hold PnL {mh:+.2f}% vs pass {mp:+.2f}% — фильтр может отрезать прибыль.", steps

        if wf_verdict == "not_ready":
            steps.append("Сначала улучшите walk-forward (multiday_lr_reality_check → not_ready).")
            steps.append("Продолжайте log_only ещё 1–2 недели.")
            return "caution", "OOS multiday not_ready — рано для apply на вход.", steps

        if entry_mode == "log_only":
            steps.append("Шаг 3: при согласии — GAME_5M_MULTIDAY_ENTRY_GATE_MODE=apply (BUY→HOLD).")
            steps.append("Сначала 1 неделя apply + мониторинг PnL vs контрфакт в следующем прогоне анализатора.")
            return "ready_for_entry_apply", f"would_hold PnL {mh:+.2f}% vs pass {mp:+.2f}% — фильтр выглядит полезным.", steps

        if entry_mode == "apply":
            steps.append("Вход уже в apply — сравните новые закрытия с предыдущим окном log_only.")
            return "monitoring", "ENTRY apply включён; следите за деградацией PnL.", steps

        return "caution", f"Неизвестный entry_mode={entry_mode!r}.", steps

    def _hold_verdict() -> Tuple[str, str, List[str]]:
        steps: List[str] = []
        if not reg_on:
            return "not_configured", "REG выключен.", steps
        if hold_mode == "none":
            steps.append("Задайте GAME_5M_MULTIDAY_HOLD_GATE_MODE=log_only.")
            return "not_configured", "Гейт удержания не включён.", steps
        if n_early == 0:
            steps.append("Дождитесь TIME_EXIT_EARLY (early_derisk / stale_reversal).")
            return "insufficient_data", "Нет TIME_EXIT_EARLY в окне.", steps
        if n_hold_telemetry == 0:
            steps.append("Проверьте деплой и SELL context_json → multiday_lr_hold_gate.")
            return "insufficient_data", "Нет телеметрии hold_gate на ранних выходах.", steps
        if n_hold_telemetry < min_early_telemetry:
            need = min_early_telemetry - n_hold_telemetry
            steps.append(f"Копите log_only: ещё ≥{need} SELL с multiday_lr_hold_gate.")
            return "insufficient_data", f"Мало SELL с hold_gate: {n_hold_telemetry}/{min_early_telemetry}.", steps
        if n_would_defer < min_would_defer:
            need = min_would_defer - n_would_defer
            steps.append(f"Нужно ещё ≥{need} would_defer для оценки отложенного выхода.")
            return "insufficient_data", f"Мало would_defer: {n_would_defer}/{min_would_defer}.", steps

        md, mn = _mean_pct(pnl_defer), _mean_pct(pnl_no_defer)
        if md is not None and mn is not None and md <= mn - pnl_edge_pp:
            steps.append("Не включать HOLD apply: ранние выходы с would_defer не лучше по PnL.")
            return "caution", f"would_defer PnL {md:+.2f}% vs остальные {mn:+.2f}%.", steps

        if entry_mode != "apply":
            steps.append("Рекомендуется сначала пройти шаг ENTRY apply и мониторинг 1–2 недели.")
            return "caution", "Держите hold в log_only до стабилизации entry apply.", steps

        if hold_mode == "log_only":
            steps.append("Шаг 4 (после ревью recovery D4b): HOLD apply — отдельный PR defer TIME_EXIT.")
            steps.append("Сверьте с time_exit_early_review и recovery_ml_time_exit_early.")
            return "ready_for_hold_apply", f"would_defer PnL {md:+.2f}% vs {mn:+.2f}% — кандидат на defer.", steps

        return "monitoring", "HOLD apply пока не реализован в кроне.", steps

    ev, erat, esteps = _entry_verdict()
    hv, hrat, hsteps = _hold_verdict()

    overall = "accumulating"
    if ev == "ready_for_entry_apply" and hv in ("insufficient_data", "not_configured", "caution"):
        overall = "ready_entry_step"
    if ev in ("monitoring", "ready_for_entry_apply") and hv == "ready_for_hold_apply":
        overall = "ready_hold_step"
    if ev == "insufficient_data" and hv == "insufficient_data":
        overall = "accumulating"
    if ev == "caution" or hv == "caution":
        overall = "caution"

    lines: List[str] = [
        "• **Multiday ridge — гейты входа/удержания** (log-only телеметрия):",
        f"  Вход: mode={entry_mode}, вердикт **{ev}** — {erat}",
        f"  (BUY n={n_buy}, forecast={n_forecast}, gate_ok={n_gate_ok}, would_hold={n_would_hold}; "
        f"mean PnL hold={_mean_pct(pnl_hold)}% pass={_mean_pct(pnl_pass)}%)",
        f"  Удержание: mode={hold_mode}, вердикт **{hv}** — {hrat}",
        f"  (TIME_EXIT_EARLY n={n_early}, hold_telemetry={n_hold_telemetry}, would_defer={n_would_defer}; "
        f"mean PnL defer={_mean_pct(pnl_defer)}% other={_mean_pct(pnl_no_defer)}%)",
        f"  Walk-forward OOS (справочно): **{wf_verdict}**",
        "",
        "**Следующие шаги:**",
    ]
    for i, s in enumerate(esteps + hsteps, 1):
        lines.append(f"  {i}. {s}")

    thresholds = {
        "min_buy_gate_ok": min_buy_ok,
        "min_would_hold": min_would_hold,
        "min_early_exit_with_hold_gate": min_early_telemetry,
        "min_would_defer": min_would_defer,
        "pnl_edge_pp": pnl_edge_pp,
    }

    return {
        "mode": "ok",
        "description": (
            "Оценка достаточности выборки для перевода multiday ridge gates из log_only в apply. "
            "Не меняет config.env автоматически."
        ),
        "config": {
            "GAME_5M_MULTIDAY_LR_REG_ENABLED": reg_on,
            "GAME_5M_MULTIDAY_ENTRY_GATE_MODE": entry_mode,
            "GAME_5M_MULTIDAY_HOLD_GATE_MODE": hold_mode,
        },
        "thresholds": thresholds,
        "entry_gate": {
            "verdict": ev,
            "rationale_ru": erat,
            "n_buy": n_buy,
            "n_with_forecast": n_forecast,
            "n_gate_ok": n_gate_ok,
            "n_would_hold": n_would_hold,
            "mean_realized_pct_would_hold": _mean_pct(pnl_hold),
            "mean_realized_pct_pass": _mean_pct(pnl_pass),
            "next_steps_ru": esteps,
        },
        "hold_gate": {
            "verdict": hv,
            "rationale_ru": hrat,
            "n_time_exit_early": n_early,
            "n_with_hold_telemetry": n_hold_telemetry,
            "n_would_defer": n_would_defer,
            "mean_realized_pct_would_defer": _mean_pct(pnl_defer),
            "mean_realized_pct_other": _mean_pct(pnl_no_defer),
            "next_steps_ru": hsteps,
        },
        "overall_verdict": overall,
        "summary_lines_ru": lines,
        "conclusion_ru": "\n".join(lines),
    }


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
        env_rec = mlr.get("multiday_env_recommendations") or {}
        for sl in env_rec.get("summary_lines_ru") or []:
            lines.append(f"  {sl}")
        if env_rec.get("note_ru"):
            lines.append(f"  ({env_rec.get('note_ru')})")
        fc = mlr.get("multiday_lr_feature_comparison") or {}
        if fc.get("summary_ru"):
            lines.append(f"  Сводка OOS v2 vs v3: {fc.get('summary_ru')}")
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

    mga = report.get("multiday_lr_gates_arbiter") or {}
    if mga.get("mode") == "ok":
        verdicts["multiday_lr_gates"] = str(mga.get("overall_verdict") or "accumulating")
        for sl in mga.get("summary_lines_ru") or []:
            lines.append(sl)
    elif mga.get("note"):
        verdicts["multiday_lr_gates"] = "skipped"
        lines.append(f"• Multiday gates arbiter: {mga.get('note')}")

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
