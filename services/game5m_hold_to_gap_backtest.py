# -*- coding: utf-8 -*-
"""
Контрфакт «удержать 2–3 дня / продать на open следующего дня» vs фактический выход GAME_5M.

Использует BUY context_json (multiday flat fields), SELL exit_context_json, 5m OHLC.
Пересчитывает multiday на дату выхода (ridge по дневкам) если в exit нет снимка.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from services.deal_params_5m import normalize_entry_context


def _json_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            import json

            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


def _horizons_from_ctx(ctx: dict) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for suffix in ("1d", "2d", "3d"):
        k = f"multiday_lr_horizon_{suffix}_pct_vs_spot"
        v = ctx.get(k)
        if v is None:
            out[suffix] = None
            continue
        try:
            out[suffix] = float(v)
        except (TypeError, ValueError):
            out[suffix] = None
    snap = ctx.get("multiday_lr_at_exit")
    if isinstance(snap, dict) and isinstance(snap.get("horizons_pct"), dict):
        for suffix in ("1d", "2d", "3d"):
            if out.get(suffix) is None and snap["horizons_pct"].get(suffix) is not None:
                try:
                    out[suffix] = float(snap["horizons_pct"][suffix])
                except (TypeError, ValueError):
                    pass
    hg = ctx.get("multiday_lr_hold_gate")
    if isinstance(hg, dict) and isinstance(hg.get("horizons_pct"), dict):
        for suffix in ("1d", "2d", "3d"):
            if out.get(suffix) is None and hg["horizons_pct"].get(suffix) is not None:
                try:
                    out[suffix] = float(hg["horizons_pct"][suffix])
                except (TypeError, ValueError):
                    pass
    return out


def _bullish_horizons(pcts: Dict[str, Optional[float]], *, tau: float = 0.20, pos_min: int = 2) -> bool:
    positives = sum(1 for k in ("1d", "2d", "3d") if pcts.get(k) is not None and float(pcts[k]) > tau)
    return positives >= pos_min


def _pct_vs_entry(entry_px: float, price: float) -> Optional[float]:
    if entry_px <= 0 or price <= 0:
        return None
    return round((price / entry_px - 1.0) * 100.0, 4)


def _next_session_opens(
    df: Optional[pd.DataFrame],
    exit_ts: Any,
    *,
    max_days: int = 3,
) -> Dict[str, Any]:
    if df is None or df.empty:
        return {}
    t_exit = pd.Timestamp(exit_ts)
    if t_exit.tzinfo is None:
        t_exit = t_exit.tz_localize("UTC").tz_convert("America/New_York")
    else:
        t_exit = t_exit.tz_convert("America/New_York")
    exit_day = t_exit.normalize()
    dts = pd.to_datetime(df["datetime"])
    if getattr(dts.dt, "tz", None) is None:
        dts = dts.dt.tz_localize("America/New_York", ambiguous="infer")
    else:
        dts = dts.dt.tz_convert("America/New_York")
    sessions = sorted({pd.Timestamp(x).normalize() for x in dts if pd.Timestamp(x).normalize() > exit_day})
    out: Dict[str, Any] = {}
    for i, sess in enumerate(sessions[:max_days], start=1):
        day = df.loc[dts.dt.normalize() == sess]
        if day.empty:
            continue
        op = float(day.iloc[0]["Open"])
        hi = float(day["High"].max())
        out[f"d{i}_date"] = str(sess.date())
        out[f"d{i}_open"] = round(op, 4)
        out[f"d{i}_high"] = round(hi, 4)
    return out


def _recompute_multiday_at_ts(engine, ticker: str, ts: Any) -> Dict[str, Optional[float]]:
    """Ridge multiday % на дату ts (ET), log→pct."""
    try:
        from services.analyzer_ml_arbiter import _daily_index_on_or_before_entry, _ridge_lambda_from_config, _use_premarket_from_config
        from services.log_return_multiday_forecast import _aligned_lr, _build_feature_row, _ridge_weights
        from services.multiday_lr_pipeline import (
            build_training_stack,
            fetch_daily_close_series_from_quotes,
            fetch_premarket_features_dataframe,
            _premarket_vec_for_date,
        )
    except Exception:
        return {}

    horizons = (1, 2, 3)
    t = str(ticker).strip().upper()
    s = fetch_daily_close_series_from_quotes(engine, t, min_date=None)
    if s is None or len(s) < 80:
        return {}
    dates = s.index
    c_full = s.values.astype(float)
    idx = _daily_index_on_or_before_entry(dates, ts)
    if idx is None:
        return {}
    sub_dates = dates[: idx + 1]
    sub_c = c_full[: idx + 1]
    use_pm = _use_premarket_from_config()
    pm_full = None
    if use_pm:
        try:
            d0 = dates[0]
            min_d = d0.strftime("%Y-%m-%d") if hasattr(d0, "strftime") else str(d0)[:10]
            pm_full = fetch_premarket_features_dataframe(engine, t, min_date=min_d)
        except Exception:
            pm_full = None
    use_pm = bool(use_pm and pm_full is not None and not pm_full.empty)
    try:
        X, ydict, _, _, n_pm = build_training_stack(
            sub_dates, sub_c, horizons, pm_df=pm_full, use_premarket=use_pm
        )
    except Exception:
        return {}
    if X is None or X.shape[0] < 40:
        return {}
    lam = _ridge_lambda_from_config()
    lr = _aligned_lr(sub_c)
    last_i = len(sub_c) - 1
    row_base = _build_feature_row(sub_c, lr, last_i, vol_window=10, mean_window=5)
    if row_base is None:
        return {}
    pm_live = _premarket_vec_for_date(pm_full, sub_dates[-1]) if n_pm else np.zeros(0, dtype=float)
    intra2 = np.zeros(2, dtype=float)
    x_pred = np.concatenate([row_base, pm_live, intra2]) if n_pm else np.concatenate([row_base, intra2])
    out: Dict[str, Optional[float]] = {}
    for h in horizons:
        y = ydict.get(int(h))
        if y is None or len(y) != X.shape[0]:
            continue
        try:
            w = _ridge_weights(X, y, float(lam))
            pred_log = float(x_pred @ w)
            out[f"{h}d"] = round((math.exp(pred_log) - 1.0) * 100.0, 4) if math.isfinite(pred_log) else None
        except Exception:
            out[f"{h}d"] = None
    return out


def _simulate_current_eod_would_flatten(
    exit_pcts: Dict[str, Optional[float]],
    *,
    current_decision: str,
    pnl_current_pct: Optional[float],
    always_flat: bool,
) -> Tuple[bool, str]:
    """Упрощённая симуляция should_eod_flatten (always=false ветка)."""
    from config_loader import get_config_value

    if always_flat:
        return True, "overnight_eod_flat_always"
    dec = (current_decision or "").strip().upper()
    allow_strong = str(get_config_value("GAME_5M_EOD_FLATTEN_ALLOW_STRONG_BUY_HOLD", "false")).lower() in (
        "1",
        "true",
        "yes",
    )
    tau = float(get_config_value("GAME_5M_MULTIDAY_HOLD_TAU_PCT", "0.20") or 0.20)
    pos_min = int(get_config_value("GAME_5M_MULTIDAY_HOLD_POSITIVE_HORIZONS_MIN", "2") or 2)
    if allow_strong and dec == "STRONG_BUY" and _bullish_horizons(exit_pcts, tau=tau, pos_min=pos_min):
        return False, "hold_strong_buy_bullish"
    if _bullish_horizons(exit_pcts, tau=tau, pos_min=pos_min):
        deep_loss = float(get_config_value("GAME_5M_MULTIDAY_HOLD_MAX_LOSS_PCT", "-4.0") or -4.0)
        if pnl_current_pct is not None and float(pnl_current_pct) <= deep_loss:
            return True, "overnight_eod_flat_loss_deep"
        return False, "hold_bullish_multiday"
    max_loss = float(get_config_value("GAME_5M_EOD_FLATTEN_MAX_LOSS_TO_FORCE_PCT", "-0.5") or -0.5)
    if pnl_current_pct is not None and float(pnl_current_pct) <= max_loss:
        return True, "overnight_eod_flat_loss"
    if dec not in ("STRONG_BUY",):
        return True, "overnight_eod_flat_weak_signal"
    return False, ""


def build_hold_to_gap_backtest(
    closed_trades: Sequence[Any],
    effects: Sequence[Any],
    ohlc_cache: Dict[str, Optional[pd.DataFrame]],
    *,
    engine=None,
    limit: int = 50,
    cost_bps_roundtrip: float = 12.0,
) -> Dict[str, Any]:
    from config_loader import get_config_value

    by_tid = {int(getattr(t, "trade_id", 0) or 0): t for t in closed_trades}
    cost_pct = float(cost_bps_roundtrip) / 100.0
    eod_always_cfg = str(get_config_value("GAME_5M_EOD_FLATTEN_ALWAYS", "true")).lower() in ("1", "true", "yes")
    hold_mode = (get_config_value("GAME_5M_MULTIDAY_HOLD_GATE_MODE", "none") or "none").strip().lower()

    rows: List[Dict[str, Any]] = []
    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp:
            continue
        entry_ctx = normalize_entry_context(getattr(tp, "context_json", None))
        exit_ctx = _json_dict(getattr(tp, "exit_context_json", None))
        entry_px = float(e.entry_price or 0)
        exit_px = float(e.exit_price or 0)
        if entry_px <= 0 or exit_px <= 0:
            continue

        md_entry = _horizons_from_ctx(entry_ctx)
        md_exit_saved = _horizons_from_ctx(exit_ctx)
        md_exit = md_exit_saved if any(v is not None for v in md_exit_saved.values()) else md_entry
        if engine is not None and not any(v is not None for v in md_exit.values()):
            md_exit = _recompute_multiday_at_ts(engine, e.ticker, e.exit_ts) or md_entry

        opens = _next_session_opens(ohlc_cache.get(e.ticker), e.exit_ts, max_days=3)
        actual_pct = round(float(e.realized_pct), 4)
        d1_open = opens.get("d1_open")
        d2_open = opens.get("d2_open")
        d3_open = opens.get("d3_open")
        alt_d1 = _pct_vs_entry(entry_px, float(d1_open)) if d1_open else None
        alt_d2 = _pct_vs_entry(entry_px, float(d2_open)) if d2_open else None
        alt_d3 = _pct_vs_entry(entry_px, float(d3_open)) if d3_open else None

        exit_detail = str(exit_ctx.get("exit_detail") or e.exit_detail or "")
        is_eod = exit_detail.startswith("overnight_eod")
        is_early = str(e.exit_signal or "").upper() == "TIME_EXIT_EARLY"

        pnl_at_exit = (exit_px / entry_px - 1.0) * 100.0 if entry_px > 0 else None
        dec_at_exit = str(
            exit_ctx.get("decision") or entry_ctx.get("decision") or entry_ctx.get("technical_decision_core") or ""
        )
        would_flat_new, flat_reason = _simulate_current_eod_would_flatten(
            md_exit,
            current_decision=dec_at_exit,
            pnl_current_pct=pnl_at_exit,
            always_flat=False,
        )
        would_flat_old = is_eod and exit_detail in ("overnight_eod_flat", "overnight_eod_flat_weak_signal")

        hold_gate = exit_ctx.get("multiday_lr_hold_gate") if isinstance(exit_ctx.get("multiday_lr_hold_gate"), dict) else {}
        would_defer_early = bool(hold_gate.get("would_defer_exit"))

        policy_pct = actual_pct
        policy_note = "actual"
        if is_early and would_defer_early and hold_mode == "apply" and alt_d1 is not None:
            policy_pct = round(alt_d1 - cost_pct, 4)
            policy_note = "hold_gate_apply_d1_open"
        elif is_eod and would_flat_old and not would_flat_new and alt_d1 is not None:
            policy_pct = round(alt_d1 - cost_pct, 4)
            policy_note = "new_eod_policy_d1_open"
        elif is_eod and would_flat_old and alt_d1 is not None:
            policy_pct = round(alt_d1 - cost_pct, 4)
            policy_note = "counterfactual_d1_open_if_held"

        rows.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "exit_signal": e.exit_signal,
                "exit_detail": exit_detail[:80] if exit_detail else None,
                "actual_pct": actual_pct,
                "alt_d1_open_pct": alt_d1,
                "alt_d2_open_pct": alt_d2,
                "alt_d3_open_pct": alt_d3,
                "delta_d1_vs_actual": round(alt_d1 - actual_pct, 4) if alt_d1 is not None else None,
                "multiday_at_entry": {k: v for k, v in md_entry.items() if v is not None},
                "multiday_at_exit": {k: v for k, v in md_exit.items() if v is not None},
                "bullish_multiday_at_exit": _bullish_horizons(md_exit),
                "would_flatten_new_policy": would_flat_new if is_eod else None,
                "flat_reason_new_policy": flat_reason if is_eod else None,
                "hold_gate_would_defer": would_defer_early if is_early else None,
                "simulated_policy_pct": policy_pct,
                "simulated_policy": policy_note,
                "next_opens": opens,
            }
        )

    rows = sorted(rows, key=lambda r: abs(float(r.get("delta_d1_vs_actual") or 0.0)), reverse=True)[: max(1, int(limit))]

    def _avg(vals: List[Optional[float]]) -> Optional[float]:
        xs = [float(v) for v in vals if v is not None]
        return round(sum(xs) / len(xs), 4) if xs else None

    n = len(rows)
    missed_d1 = [r for r in rows if (r.get("delta_d1_vs_actual") or 0) > 0.5]
    eod_rows = [r for r in rows if (r.get("exit_detail") or "").startswith("overnight_eod")]
    early_rows = [r for r in rows if r.get("exit_signal") == "TIME_EXIT_EARLY"]
    policy_better = [r for r in rows if (r.get("simulated_policy_pct") or 0) > (r.get("actual_pct") or 0) + 0.3]

    return {
        "mode": "hold_to_gap_counterfactual",
        "description": (
            "Сравнение фактического PnL с продажей на open d+1/d+2/d+3 и симуляцией текущей EOD/hold политики. "
            f"Комиссии в simulated_policy: ~{cost_bps_roundtrip} bps roundtrip."
        ),
        "config_snapshot": {
            "eod_flatten_always": eod_always_cfg,
            "multiday_hold_gate_mode": hold_mode,
            "note_ru": (
                "EOD_FLATTEN_ALWAYS=false: flat только при weak signal / loss / bearish multiday; "
                "bullish 2/3 horizons → hold overnight."
            ),
        },
        "trades_analyzed": n,
        "eod_flat_trades": len(eod_rows),
        "time_exit_early_trades": len(early_rows),
        "avg_actual_pct": _avg([r.get("actual_pct") for r in rows]),
        "avg_alt_d1_open_pct": _avg([r.get("alt_d1_open_pct") for r in rows]),
        "avg_delta_d1_vs_actual": _avg([r.get("delta_d1_vs_actual") for r in rows]),
        "missed_d1_open_gt_0p5pp": len(missed_d1),
        "simulated_policy_better_count": len(policy_better),
        "avg_simulated_policy_pct": _avg([r.get("simulated_policy_pct") for r in rows]),
        "by_ticker": _aggregate_by_ticker(rows),
        "rows": rows,
    }


def _aggregate_by_ticker(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(str(r.get("ticker") or "?"), []).append(r)
    out = []
    for t, lst in sorted(by.items()):
        deltas = [float(x["delta_d1_vs_actual"]) for x in lst if x.get("delta_d1_vs_actual") is not None]
        out.append(
            {
                "ticker": t,
                "n": len(lst),
                "avg_actual_pct": round(sum(x["actual_pct"] for x in lst) / len(lst), 4),
                "avg_delta_d1_vs_actual": round(sum(deltas) / len(deltas), 4) if deltas else None,
                "missed_d1_count": sum(1 for x in lst if (x.get("delta_d1_vs_actual") or 0) > 0.5),
            }
        )
    return sorted(out, key=lambda x: float(x.get("avg_delta_d1_vs_actual") or 0), reverse=True)
