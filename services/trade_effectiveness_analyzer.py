from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls
from services.recommend_5m import fetch_5m_ohlc
from services.deal_params_5m import normalize_entry_context
from config_loader import load_config, is_editable_config_env_key


@dataclass
class TradeEffect:
    trade_id: int
    ticker: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    hold_minutes: float
    qty: float
    entry_price: float
    exit_price: float
    net_pnl: float
    realized_pct: float
    realized_log_return: float
    exit_signal: str
    exit_strategy: str
    potential_best_pct: Optional[float]
    preventable_worst_pct: Optional[float]
    missed_upside_pct: Optional[float]
    avoidable_loss_pct: Optional[float]
    likely_late_polling: bool
    entry_rsi_5m: Optional[float]
    entry_vol_5m_pct: Optional[float]
    entry_momentum_2h_pct: Optional[float]
    entry_prob_up: Optional[float]
    entry_prob_down: Optional[float]
    entry_news_impact: Optional[str]
    entry_advice: Optional[str]
    decision_rule_version: Optional[str]
    decision_rule_params: Optional[Dict[str, Any]]


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return None


def _as_et(ts: pd.Timestamp) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("America/New_York")
    return t.tz_convert("America/New_York")


def _load_closed_trades(days: int, strategy_name: Optional[str]) -> List[Any]:
    engine = get_engine()
    if strategy_name and strategy_name.upper() != "ALL":
        raw = load_trade_history(engine, strategy_name=strategy_name)
    else:
        raw = load_trade_history(engine)
    closed = compute_closed_trade_pnls(raw)
    if not closed:
        return []
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    out = []
    for t in closed:
        ts = pd.Timestamp(t.ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        if ts >= cutoff:
            out.append(t)
    return out


def _prepare_ohlc_cache(tickers: List[str], days: int) -> Dict[str, Optional[pd.DataFrame]]:
    cache: Dict[str, Optional[pd.DataFrame]] = {}
    fetch_days = min(7, max(3, days + 2))
    for t in sorted(set(tickers)):
        try:
            df = fetch_5m_ohlc(t, days=fetch_days)
            if df is None or df.empty:
                cache[t] = None
                continue
            d = df.copy()
            dt = pd.to_datetime(d["datetime"])
            if dt.dt.tz is None:
                dt = dt.dt.tz_localize("America/New_York", ambiguous="infer")
            else:
                dt = dt.dt.tz_convert("America/New_York")
            d["datetime"] = dt
            cache[t] = d.sort_values("datetime").reset_index(drop=True)
        except Exception:
            cache[t] = None
    return cache


def _slice_window(df: Optional[pd.DataFrame], start_et: pd.Timestamp, end_et: pd.Timestamp) -> Optional[pd.DataFrame]:
    if df is None or df.empty or end_et <= start_et:
        return None
    m = (df["datetime"] >= start_et) & (df["datetime"] <= end_et)
    w = df.loc[m]
    if w.empty:
        return None
    return w


def _estimate_trade_effects(closed_trades: List[Any], ohlc_cache: Dict[str, Optional[pd.DataFrame]]) -> List[TradeEffect]:
    effects: List[TradeEffect] = []
    for t in closed_trades:
        if not t.entry_ts:
            continue
        entry_ts = _as_et(pd.Timestamp(t.entry_ts))
        exit_ts = _as_et(pd.Timestamp(t.ts))
        if exit_ts <= entry_ts:
            continue
        qty = float(t.quantity)
        entry = float(t.entry_price)
        exit_p = float(t.exit_price)
        if qty <= 0 or entry <= 0 or exit_p <= 0:
            continue

        cost_basis = qty * entry
        realized_pct = (float(t.net_pnl) / cost_basis) * 100 if cost_basis > 0 else 0.0
        realized_log_return = float(np.log1p(realized_pct / 100.0)) if realized_pct > -100 else -999.0

        window = _slice_window(ohlc_cache.get(t.ticker), entry_ts, exit_ts)
        potential_best_pct = preventable_worst_pct = None
        missed_upside_pct = avoidable_loss_pct = None
        likely_late_polling = False
        if window is not None and not window.empty:
            try:
                mfe_price = float(window["High"].max())
                mae_price = float(window["Low"].min())
                potential_best_pct = ((mfe_price / entry) - 1.0) * 100.0
                preventable_worst_pct = ((mae_price / entry) - 1.0) * 100.0
                missed_upside_pct = max(0.0, potential_best_pct - realized_pct)
                avoidable_loss_pct = max(0.0, realized_pct - preventable_worst_pct)
                likely_late_polling = abs(exit_p - mfe_price) / mfe_price > 0.004 if mfe_price > 0 else False
            except Exception:
                pass

        entry_ctx = normalize_entry_context(getattr(t, "context_json", None))
        effects.append(
            TradeEffect(
                trade_id=int(t.trade_id),
                ticker=str(t.ticker),
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                hold_minutes=float((exit_ts - entry_ts) / pd.Timedelta(minutes=1)),
                qty=qty,
                entry_price=entry,
                exit_price=exit_p,
                net_pnl=float(t.net_pnl),
                realized_pct=realized_pct,
                realized_log_return=realized_log_return,
                exit_signal=str(getattr(t, "signal_type", "") or ""),
                exit_strategy=str(getattr(t, "exit_strategy", "") or ""),
                potential_best_pct=potential_best_pct,
                preventable_worst_pct=preventable_worst_pct,
                missed_upside_pct=missed_upside_pct,
                avoidable_loss_pct=avoidable_loss_pct,
                likely_late_polling=likely_late_polling,
                entry_rsi_5m=_safe_float(entry_ctx.get("rsi_5m")),
                entry_vol_5m_pct=_safe_float(entry_ctx.get("volatility_5m_pct")),
                entry_momentum_2h_pct=_safe_float(entry_ctx.get("momentum_2h_pct")),
                entry_prob_up=_safe_float(entry_ctx.get("prob_up")),
                entry_prob_down=_safe_float(entry_ctx.get("prob_down")),
                entry_news_impact=(entry_ctx.get("kb_news_impact") or None),
                entry_advice=(entry_ctx.get("entry_advice") or None),
                decision_rule_version=(entry_ctx.get("decision_rule_version") or None),
                decision_rule_params=(entry_ctx.get("decision_rule_params") if isinstance(entry_ctx.get("decision_rule_params"), dict) else None),
            )
        )
    return effects


def _aggregate(effects: List[TradeEffect]) -> Dict[str, Any]:
    if not effects:
        return {"total": 0}
    realized = [e.realized_pct for e in effects]
    net_pnl = [e.net_pnl for e in effects]
    logrets = [e.realized_log_return for e in effects if e.realized_log_return > -900]
    missed = [e.missed_upside_pct for e in effects if e.missed_upside_pct is not None]
    avoidable = [e.avoidable_loss_pct for e in effects if e.avoidable_loss_pct is not None]
    wins = [e for e in effects if e.realized_pct > 0]
    losses = [e for e in effects if e.realized_pct <= 0]

    by_exit: Dict[str, Dict[str, float]] = {}
    for e in effects:
        k = e.exit_signal or "UNKNOWN"
        by_exit.setdefault(k, {"count": 0, "avg_realized_pct": 0.0, "avg_missed_pct": 0.0})
        by_exit[k]["count"] += 1
        by_exit[k]["avg_realized_pct"] += e.realized_pct
        by_exit[k]["avg_missed_pct"] += e.missed_upside_pct or 0.0
    for v in by_exit.values():
        c = max(1, int(v["count"]))
        v["avg_realized_pct"] = round(v["avg_realized_pct"] / c, 3)
        v["avg_missed_pct"] = round(v["avg_missed_pct"] / c, 3)

    late_polling_count = sum(1 for e in effects if e.likely_late_polling and (e.missed_upside_pct or 0) > 0.3)
    high_vol_losses = [e for e in losses if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    weak_prob_entries = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up < 0.55]
    neg_news_losses = [e for e in losses if (e.entry_news_impact or "").lower().startswith("негатив")]

    losses_with_allow = [e for e in losses if (e.entry_advice or "").upper() == "ALLOW"]
    losses_with_high_prob = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up >= 0.60]
    losses_with_high_rsi = [e for e in losses if e.entry_rsi_5m is not None and e.entry_rsi_5m >= 60]
    rule_versions = sorted({(e.decision_rule_version or "unknown") for e in effects})
    return {
        "total": len(effects),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / len(effects), 2),
        "sum_net_pnl_usd": round(float(sum(net_pnl)), 2),
        "avg_realized_pct": round(float(np.mean(realized)), 3),
        "median_realized_pct": round(float(np.median(realized)), 3),
        "avg_log_return": round(float(np.mean(logrets)), 5) if logrets else None,
        "sum_missed_upside_pct": round(float(sum(missed)), 3),
        "avg_missed_upside_pct": round(float(np.mean(missed)), 3) if missed else None,
        "sum_avoidable_loss_pct": round(float(sum(avoidable)), 3),
        "avg_avoidable_loss_pct": round(float(np.mean(avoidable)), 3) if avoidable else None,
        "late_polling_signals": late_polling_count,
        "high_vol_losses_count": len(high_vol_losses),
        "weak_prob_up_losses_count": len(weak_prob_entries),
        "negative_news_losses_count": len(neg_news_losses),
        "losses_with_allow_entry_count": len(losses_with_allow),
        "losses_with_high_prob_up_count": len(losses_with_high_prob),
        "losses_with_high_rsi_count": len(losses_with_high_rsi),
        "decision_rule_versions": rule_versions,
        "by_exit_signal": by_exit,
    }


def _top_cases(effects: List[TradeEffect], limit: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    def row(e: TradeEffect) -> Dict[str, Any]:
        return {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "entry_ts": e.entry_ts.isoformat(),
            "exit_ts": e.exit_ts.isoformat(),
            "exit_signal": e.exit_signal,
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "avoidable_loss_pct": round(e.avoidable_loss_pct or 0.0, 3),
            "entry_rsi_5m": e.entry_rsi_5m,
            "entry_vol_5m_pct": e.entry_vol_5m_pct,
            "entry_prob_up": e.entry_prob_up,
            "entry_news_impact": e.entry_news_impact,
            "entry_advice": e.entry_advice,
            "decision_rule_version": e.decision_rule_version,
            "decision_rule_params": e.decision_rule_params,
        }

    by_missed = sorted(effects, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    by_loss = sorted(effects, key=lambda x: x.realized_pct)[:limit]
    return {"top_missed_upside": [row(e) for e in by_missed], "top_losses": [row(e) for e in by_loss]}


def _parse_llm_json_response(text: str) -> Any:
    """
    Парсит JSON из ответа модели: чистый JSON, ```json ... ```, или текст с JSON-объектом внутри.
    """
    if not text or not str(text).strip():
        return {"raw_text": text}
    s = str(text).strip()
    # Блок markdown ```json ... ``` или ``` ... ```
    fence = re.match(r"^```(?:json)?\s*\r?\n?", s, re.IGNORECASE)
    if fence:
        rest = s[fence.end() :]
        end = rest.rfind("```")
        if end != -1:
            s = rest[:end].strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # Первый { ... последний } (если модель добавила пояснения)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    return {"raw_text": text}


def _build_llm_recommendations(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        from services.llm_service import get_llm_service

        llm = get_llm_service()
        if not getattr(llm, "client", None):
            return {"status": "disabled", "reason": "LLM client unavailable"}

        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        current_rules = (
            meta.get("current_decision_rule_params")
            if isinstance(meta.get("current_decision_rule_params"), dict)
            else {}
        )
        algorithm_context = {
            "decision_source_expected": meta.get("decision_source_expected"),
            "current_decision_rule_params": current_rules,
            "parameter_to_env_key_hint": {
                "momentum_buy_min": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
                "rsi_buy_max": "GAME_5M_RSI_BUY_MAX",
                "rsi_strong_buy_max": "GAME_5M_RSI_STRONG_BUY_MAX",
                "volatility_wait_min": "GAME_5M_VOLATILITY_WAIT_MIN",
                "price_polling_interval": "GAME_5M_SIGNAL_CRON_MINUTES",
            },
            "scope_for_in_algorithm_changes": [
                "entry thresholds and guards from decision_rule_params",
                "config flags and numeric limits from current_decision_rule_params.config",
                "exit cadence and de-risk knobs from current_decision_rule_params.exit_strategy",
            ],
            "hard_constraints": [
                "propose changes only with concrete fields from current_decision_rule_params",
                "if there is no matching field, put proposal into algorithm_change_proposals",
                "do not suggest vague ideas without target parameter or code area",
            ],
        }
        llm_input = {"algorithm_context": algorithm_context, "report": payload}
        system_prompt = (
            "Ты senior quant и знаешь текущую реализацию стратегии.\n"
            "Твоя задача: по отчету предложить улучшения, строго привязанные к текущему алгоритму и параметрам.\n"
            "Сначала ищи решения В РАМКАХ текущего алгоритма (перенастройка существующих параметров), "
            "и только затем предлагай изменения алгоритма.\n"
            "Используй только данные из входного JSON, не фантазируй.\n\n"
            "Верни ТОЛЬКО валидный JSON без markdown и без пояснений вне JSON со следующими ключами:\n"
            "{\n"
            "  \"priorities\": [\"...\"],\n"
            "  \"in_algorithm_parameter_changes\": [\n"
            "    {\n"
            "      \"parameter\": \"...\",\n"
            "      \"current\": \"...\",\n"
            "      \"proposed\": \"...\",\n"
            "      \"where_used\": \"services.recommend_5m.py|services.game_5m.py|config.env\",\n"
            "      \"reason_from_metrics\": \"...\",\n"
            "      \"expected_effect\": \"...\",\n"
            "      \"confidence\": \"low|medium|high\"\n"
            "    }\n"
            "  ],\n"
            "  \"algorithm_change_proposals\": [\n"
            "    {\n"
            "      \"change\": \"...\",\n"
            "      \"why_current_algo_not_enough\": \"...\",\n"
            "      \"code_areas\": [\"services/game_5m.py\", \"services/recommend_5m.py\"],\n"
            "      \"risk\": \"low|medium|high\",\n"
            "      \"expected_effect\": \"...\"\n"
            "    }\n"
            "  ],\n"
            "  \"monitoring_fixes\": [\n"
            "    {\"issue\": \"...\", \"proposed_fix\": \"...\", \"expected_effect\": \"...\"}\n"
            "  ],\n"
            "  \"expected_impact\": {\n"
            "    \"win_rate_increase_pct\": 0,\n"
            "    \"reduction_in_avg_loss_pct\": 0,\n"
            "    \"increase_in_avg_realized_pct\": 0,\n"
            "    \"reduction_in_missed_upside_pct\": 0\n"
            "  },\n"
            "  \"validation_plan\": [\"...\"]\n"
            "}\n"
        )
        out = llm.generate_response(
            messages=[{"role": "user", "content": json.dumps(llm_input, ensure_ascii=False, indent=2)}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=1600,
        )
        text = out.get("response") or ""
        parsed = _parse_llm_json_response(text)
        return {
            "status": "ok",
            "model": out.get("model"),
            "usage": out.get("usage"),
            "analysis": parsed,
        }
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _build_practical_parameter_suggestions(
    effects: List[TradeEffect],
    summary: Dict[str, Any],
    current_rules: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Грубые, но практичные рекомендации по порогам на основе текущей выборки."""
    if not effects:
        return []
    losses = [e for e in effects if e.realized_pct <= 0]
    wins = [e for e in effects if e.realized_pct > 0]
    suggestions: List[Dict[str, Any]] = []

    # 1) Volatility gate
    loss_high_vol = [e for e in losses if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    win_high_vol = [e for e in wins if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6]
    if len(loss_high_vol) >= 3 and len(loss_high_vol) >= len(win_high_vol):
        suggestions.append(
            {
                "parameter": "volatility_wait_min",
                "current": current_rules.get("volatility_wait_min"),
                "proposed": 0.7,
                "why": f"Убыточных сделок при vol>=0.6: {len(loss_high_vol)} (выигрышных: {len(win_high_vol)}).",
                "expected_effect": "Меньше входов в шуме, ниже avoidable loss.",
            }
        )

    # 2) prob_up gate (если доступно)
    losses_high_prob = [e for e in losses if e.entry_prob_up is not None and e.entry_prob_up >= 0.6]
    if len(losses_high_prob) >= 2:
        suggestions.append(
            {
                "parameter": "min_prob_up_for_entry",
                "current": "not enforced",
                "proposed": 0.65,
                "why": f"Даже при prob_up>=0.6 есть {len(losses_high_prob)} убыточных кейсов: стоит повысить порог.",
                "expected_effect": "Фильтрация ложных BUY в пограничной зоне.",
            }
        )

    # 3) Early take / trailing hint based on missed upside
    missed = [e.missed_upside_pct or 0.0 for e in effects]
    if float(np.mean(missed)) >= 1.0 and summary.get("sum_missed_upside_pct", 0) >= 8:
        suggestions.append(
            {
                "parameter": "take_profit_management",
                "current": "fixed/early exit dominates",
                "proposed": "partial TP + trailing on strong momentum",
                "why": f"Средний missed_upside={float(np.mean(missed)):.2f}%, суммарно={summary.get('sum_missed_upside_pct', 0):.2f}%.",
                "expected_effect": "Снижение недобора прибыли на сильных движениях.",
            }
        )

    # 4) Polling cadence
    late = int(summary.get("late_polling_signals", 0))
    if late >= 3:
        suggestions.append(
            {
                "parameter": "price_polling_interval",
                "current": "5m checks",
                "proposed": "faster checks near exit levels (e.g. 1m guard)",
                "why": f"Обнаружено late_polling_signals={late}.",
                "expected_effect": "Меньше запаздывающих выходов, лучше фиксация у high.",
            }
        )
    return suggestions


def _build_critical_case_analysis(effects: List[TradeEffect], limit: int = 5) -> List[Dict[str, Any]]:
    """Разбор критичных кейсов: где одновременно большой убыток и/или большой missed upside."""
    if not effects:
        return []
    ranked = sorted(
        effects,
        key=lambda e: ((-e.realized_pct if e.realized_pct < 0 else 0.0) + (e.missed_upside_pct or 0.0)),
        reverse=True,
    )
    out: List[Dict[str, Any]] = []
    for e in ranked[:limit]:
        reason_parts = []
        if e.realized_pct < 0:
            reason_parts.append(f"loss {e.realized_pct:+.2f}%")
        if (e.missed_upside_pct or 0) > 0.8:
            reason_parts.append(f"missed {e.missed_upside_pct:+.2f}%")
        if e.likely_late_polling:
            reason_parts.append("late polling signal")
        if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6:
            reason_parts.append(f"high vol {e.entry_vol_5m_pct:.2f}%")
        if (e.entry_news_impact or "").lower().startswith("негатив"):
            reason_parts.append("negative news at entry")
        action = "Проверить пороги входа/выхода и тайминг опроса для кейса."
        if e.exit_signal == "TAKE_PROFIT" and (e.missed_upside_pct or 0) > 1.0:
            action = "Рассмотреть частичный тейк + trailing вместо полного раннего выхода."
        elif e.exit_signal == "SELL" and e.realized_pct < -1.5:
            action = "Проверить условие SELL: добавить подтверждение/буфер перед выходом."
        out.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "exit_signal": e.exit_signal,
                "realized_pct": round(e.realized_pct, 3),
                "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
                "diagnosis": ", ".join(reason_parts) if reason_parts else "key outlier",
                "action": action,
            }
        )
    return out


PARAM_TO_ENV_KEY: Dict[str, str] = {
    "volatility_wait_min": "GAME_5M_VOLATILITY_WAIT_MIN",
    "sell_confirm_bars": "GAME_5M_SELL_CONFIRM_BARS",
    "momentum_min_session_bars": "GAME_5M_MOMENTUM_MIN_SESSION_BARS",
    "momentum_allow_cross_day_buy": "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
    "premarket_momentum_buy_min": "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
    "premarket_momentum_block_below": "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
    "rsi_strong_buy_max": "GAME_5M_RSI_STRONG_BUY_MAX",
    "momentum_for_strong_buy_min": "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
    "rsi_buy_max": "GAME_5M_RSI_BUY_MAX",
    "price_to_low5d_multiplier_max": "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
    "rsi_sell_min": "GAME_5M_RSI_SELL_MIN",
    "rsi_hold_overbought_min": "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN",
    "momentum_buy_min": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
    "rsi_for_momentum_buy_max": "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
    "volatility_warn_buy_min": "GAME_5M_VOLATILITY_WARN_BUY_MIN",
    "price_polling_interval": "GAME_5M_SIGNAL_CRON_MINUTES",
    "signal_cron_minutes": "GAME_5M_SIGNAL_CRON_MINUTES",
    "cfg_min_volume_vs_avg_pct": "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
    "cfg_max_atr_5m_pct": "GAME_5M_MAX_ATR_5M_PCT",
    "entry_quality_guard_enabled": "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED",
    "entry_quality_min_rr": "GAME_5M_ENTRY_QUALITY_MIN_RR",
    "entry_quality_min_ev_pct": "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT",
    "max_position_minutes": "GAME_5M_MAX_POSITION_MINUTES",
    "stop_loss_pct_effective": "GAME_5M_STOP_LOSS_PCT",
    "take_profit_pct_effective": "GAME_5M_TAKE_PROFIT_PCT",
    "GAME_5M_VOLATILITY_WAIT_MIN": "GAME_5M_VOLATILITY_WAIT_MIN",
    "GAME_5M_SELL_CONFIRM_BARS": "GAME_5M_SELL_CONFIRM_BARS",
    "GAME_5M_MOMENTUM_MIN_SESSION_BARS": "GAME_5M_MOMENTUM_MIN_SESSION_BARS",
    "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY": "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
    "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN": "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
    "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW": "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
    "GAME_5M_MIN_VOLUME_VS_AVG_PCT": "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
    "GAME_5M_MAX_ATR_5M_PCT": "GAME_5M_MAX_ATR_5M_PCT",
    "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED": "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED",
    "GAME_5M_ENTRY_QUALITY_MIN_RR": "GAME_5M_ENTRY_QUALITY_MIN_RR",
    "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT": "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT",
    "GAME_5M_MAX_POSITION_MINUTES": "GAME_5M_MAX_POSITION_MINUTES",
    "GAME_5M_STOP_LOSS_PCT": "GAME_5M_STOP_LOSS_PCT",
    "GAME_5M_TAKE_PROFIT_PCT": "GAME_5M_TAKE_PROFIT_PCT",
    "GAME_5M_RSI_STRONG_BUY_MAX": "GAME_5M_RSI_STRONG_BUY_MAX",
    "GAME_5M_MOMENTUM_STRONG_BUY_MIN": "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
    "GAME_5M_RSI_BUY_MAX": "GAME_5M_RSI_BUY_MAX",
    "GAME_5M_PRICE_TO_LOW5D_MULT_MAX": "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
    "GAME_5M_RSI_SELL_MIN": "GAME_5M_RSI_SELL_MIN",
    "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN": "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN",
    "GAME_5M_RTH_MOMENTUM_BUY_MIN": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
    "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX": "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
    "GAME_5M_VOLATILITY_WARN_BUY_MIN": "GAME_5M_VOLATILITY_WARN_BUY_MIN",
    "GAME_5M_SIGNAL_CRON_MINUTES": "GAME_5M_SIGNAL_CRON_MINUTES",
}


def _normalize_env_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if v is None:
        return ""
    return str(v).strip()


def _coerce_polling_minutes(proposed: Any) -> Optional[str]:
    """LLM часто возвращает '1m near exit levels' — извлекаем целые минуты для GAME_5M_SIGNAL_CRON_MINUTES."""
    if isinstance(proposed, (int, float)) and not isinstance(proposed, bool):
        m = int(round(float(proposed)))
        return str(max(1, min(30, m)))
    s = str(proposed).strip().lower()
    if not s:
        return None
    m = re.search(r"(\d+)\s*m\b", s)
    if m:
        return str(max(1, min(30, int(m.group(1)))))
    m2 = re.search(r"\b(\d+)\b", s)
    if m2:
        return str(max(1, min(30, int(m2.group(1)))))
    return None


def _build_auto_config_override(report: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    llm = report.get("llm") if isinstance(report.get("llm"), dict) else {}
    llm_ana = llm.get("analysis") if isinstance(llm.get("analysis"), dict) else {}
    llm_changes = llm_ana.get("in_algorithm_parameter_changes")
    if not isinstance(llm_changes, list):
        llm_changes = llm_ana.get("threshold_changes")
    llm_changes = llm_changes if isinstance(llm_changes, list) else []

    updates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for row in llm_changes:
        if not isinstance(row, dict):
            continue
        parameter = str(row.get("parameter") or "").strip()
        proposed = row.get("proposed")
        if not parameter or proposed is None:
            continue
        env_key = PARAM_TO_ENV_KEY.get(parameter) or PARAM_TO_ENV_KEY.get(parameter.upper())
        if not env_key:
            skipped.append(
                {
                    "parameter": parameter,
                    "reason": "no_env_mapping",
                    "note": "Нужна доработка алгоритма/маппинга (ключ не найден).",
                }
            )
            continue
        if env_key in seen:
            continue
        seen.add(env_key)
        if not is_editable_config_env_key(env_key):
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "not_editable",
                    "note": "Ключ не разрешён для веб-редактора config.env.",
                }
            )
            continue
        current = cfg.get(env_key, "")
        if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
            proposed_str = _coerce_polling_minutes(proposed)
            if proposed_str is None:
                skipped.append(
                    {
                        "parameter": parameter,
                        "env_key": env_key,
                        "reason": "unparseable_polling",
                        "note": "Ожидались минуты (например 1 или 1m).",
                    }
                )
                continue
        else:
            proposed_str = _normalize_env_value(proposed)
        updates.append(
            {
                "env_key": env_key,
                "current": current,
                "proposed": proposed_str,
                "source_parameter": parameter,
                "reason": row.get("reason_from_metrics") or row.get("why") or row.get("expected_effect") or "",
            }
        )

    practical = report.get("practical_parameter_suggestions")
    if isinstance(practical, list):
        for row in practical:
            if not isinstance(row, dict):
                continue
            parameter = str(row.get("parameter") or "").strip()
            proposed = row.get("proposed")
            if not parameter or proposed is None:
                continue
            env_key = PARAM_TO_ENV_KEY.get(parameter)
            if not env_key or env_key in seen:
                continue
            if not is_editable_config_env_key(env_key):
                continue
            seen.add(env_key)
            current = cfg.get(env_key, "")
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                proposed_str = _coerce_polling_minutes(proposed)
                if proposed_str is None:
                    continue
            else:
                proposed_str = _normalize_env_value(proposed)
            updates.append(
                {
                    "env_key": env_key,
                    "current": current,
                    "proposed": proposed_str,
                    "source_parameter": parameter,
                    "reason": row.get("why") or row.get("expected_effect") or "",
                }
            )

    env_lines = [f"{u['env_key']}={u['proposed']}" for u in updates]
    manual_notes: List[str] = []
    if any(u.get("env_key") == "GAME_5M_SIGNAL_CRON_MINUTES" for u in updates):
        manual_notes.append(
            "GAME_5M_SIGNAL_CRON_MINUTES должен совпадать с crontab (см. setup_cron.sh: */N * * * * ... send_sndk_signal_cron.py). "
            "После смены N — обновите crontab вручную или запустите ./setup_cron.sh (перезапишет весь блок LSE)."
        )
    return {
        "updates": updates,
        "skipped": skipped,
        "env_block": "\n".join(env_lines),
        "can_apply": len(updates) > 0,
        "manual_notes": manual_notes,
    }


def _get_current_decision_rule_params() -> Dict[str, Any]:
    """Текущие параметры правил из кода/config (для LLM, даже если в старых сделках нет snapshot)."""
    try:
        from config_loader import get_config_value
        from services.recommend_5m import GAME_5M_RULE_VERSION, get_decision_5m_rule_thresholds
        from services.game_5m import (
            _max_position_minutes,
            _stop_loss_enabled,
            _strategy_stop_and_take,
        )

        def _cfg_str(key: str, default: str = "") -> Optional[str]:
            v = (get_config_value(key, default) or "").strip()
            return v or None

        def _cfg_bool(key: str, default: str = "false") -> bool:
            return (_cfg_str(key, default) or "").lower() in ("1", "true", "yes")

        def _cfg_int(key: str, default: str) -> Optional[int]:
            raw = _cfg_str(key, default)
            if raw is None:
                return None
            try:
                return int(raw)
            except Exception:
                return None

        def _cfg_float(key: str, default: str) -> Optional[float]:
            raw = _cfg_str(key, default)
            if raw is None:
                return None
            try:
                return float(raw)
            except Exception:
                return None

        th = get_decision_5m_rule_thresholds()
        try:
            cron_min = int((get_config_value("GAME_5M_SIGNAL_CRON_MINUTES", "5") or "5").strip())
        except (ValueError, TypeError):
            cron_min = 5
        cron_min = max(1, min(30, cron_min))

        stop_pct, take_pct = _strategy_stop_and_take()
        return {
            "rule_version": GAME_5M_RULE_VERSION,
            "source_fn": "services.recommend_5m.get_decision_5m",
            **th,
            "signal_cron_minutes": cron_min,
            "news_negative_min": 0.4,
            "news_very_negative_min": 0.35,
            "news_positive_min": 0.65,
            "cfg_min_volume_vs_avg_pct": _cfg_str("GAME_5M_MIN_VOLUME_VS_AVG_PCT", ""),
            "cfg_max_atr_5m_pct": _cfg_str("GAME_5M_MAX_ATR_5M_PCT", ""),
            "config": {
                "GAME_5M_SIGNAL_CRON_MINUTES": cron_min,
                "GAME_5M_RSI_STRONG_BUY_MAX": th.get("rsi_strong_buy_max"),
                "GAME_5M_MOMENTUM_STRONG_BUY_MIN": th.get("momentum_for_strong_buy_min"),
                "GAME_5M_RSI_BUY_MAX": th.get("rsi_buy_max"),
                "GAME_5M_PRICE_TO_LOW5D_MULT_MAX": th.get("price_to_low5d_multiplier_max"),
                "GAME_5M_RSI_SELL_MIN": th.get("rsi_sell_min"),
                "GAME_5M_RSI_HOLD_OVERBOUGHT_MIN": th.get("rsi_hold_overbought_min"),
                "GAME_5M_RTH_MOMENTUM_BUY_MIN": th.get("momentum_buy_min"),
                "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX": th.get("rsi_for_momentum_buy_max"),
                "GAME_5M_VOLATILITY_WARN_BUY_MIN": th.get("volatility_warn_buy_min"),
                "GAME_5M_VOLATILITY_WAIT_MIN": _cfg_float("GAME_5M_VOLATILITY_WAIT_MIN", "0.7"),
                "GAME_5M_SELL_CONFIRM_BARS": _cfg_int("GAME_5M_SELL_CONFIRM_BARS", "2"),
                "GAME_5M_MOMENTUM_MIN_SESSION_BARS": _cfg_int("GAME_5M_MOMENTUM_MIN_SESSION_BARS", "7"),
                "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY": _cfg_bool("GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY", "false"),
                "GAME_5M_EARLY_USE_PREMARKET_MOMENTUM": _cfg_bool("GAME_5M_EARLY_USE_PREMARKET_MOMENTUM", "true"),
                "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN": _cfg_float("GAME_5M_PREMARKET_MOMENTUM_BUY_MIN", "0.5"),
                "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW": _cfg_float("GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW", "-2.0"),
                "GAME_5M_MIN_VOLUME_VS_AVG_PCT": _cfg_float("GAME_5M_MIN_VOLUME_VS_AVG_PCT", ""),
                "GAME_5M_MAX_ATR_5M_PCT": _cfg_float("GAME_5M_MAX_ATR_5M_PCT", ""),
                "GAME_5M_ENTRY_QUALITY_GUARD_ENABLED": _cfg_bool("GAME_5M_ENTRY_QUALITY_GUARD_ENABLED", "false"),
                "GAME_5M_ENTRY_QUALITY_MIN_RR": _cfg_float("GAME_5M_ENTRY_QUALITY_MIN_RR", "1.2"),
                "GAME_5M_ENTRY_QUALITY_MIN_EV_PCT": _cfg_float("GAME_5M_ENTRY_QUALITY_MIN_EV_PCT", "0.0"),
            },
            "exit_strategy": {
                "max_position_minutes": _max_position_minutes(),
                "stop_loss_enabled": _stop_loss_enabled(),
                "stop_loss_pct_effective": stop_pct,
                "take_profit_pct_effective": take_pct,
                "GAME_5M_SESSION_END_EXIT_MINUTES": _cfg_int("GAME_5M_SESSION_END_EXIT_MINUTES", "30"),
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT": _cfg_float("GAME_5M_SESSION_END_MIN_PROFIT_PCT", "0.3"),
                "GAME_5M_EARLY_DERISK_ENABLED": _cfg_bool("GAME_5M_EARLY_DERISK_ENABLED", "false"),
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": _cfg_int("GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES", "180"),
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": _cfg_float("GAME_5M_EARLY_DERISK_MAX_LOSS_PCT", "-2.0"),
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": _cfg_float("GAME_5M_EARLY_DERISK_MOMENTUM_BELOW", "0.0"),
            },
        }
    except Exception:
        return {}


def analyze_trade_effectiveness(days: int = 7, strategy: str = "GAME_5M", use_llm: bool = False) -> Dict[str, Any]:
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    if not closed:
        return {"meta": {"days": days, "strategy": strategy, "trades_analyzed": 0}, "summary": {"total": 0}}

    tickers = [str(t.ticker) for t in closed if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers, days=days)
    effects = _estimate_trade_effects(closed, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    current_rules = _get_current_decision_rule_params()
    practical = _build_practical_parameter_suggestions(effects, summary, current_rules)
    critical_cases = _build_critical_case_analysis(effects, limit=5)
    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "trades_analyzed": len(effects),
            "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness",
            "decision_source_expected": "services.recommend_5m.get_decision_5m",
            "current_decision_rule_params": current_rules,
        },
        "summary": summary,
        "top_cases": tops,
        "practical_parameter_suggestions": practical,
        "critical_case_analysis": critical_cases,
    }
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload)
    payload["auto_config_override"] = _build_auto_config_override(payload)
    return payload


def format_trade_effectiveness_text(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    if summary.get("total", 0) == 0:
        return "За выбранный период закрытых сделок не найдено."
    top = report.get("top_cases") or {}
    lines = [
        "📊 Анализатор эффективности сделок",
        f"Период: {report.get('meta', {}).get('days', '—')} дн. | Стратегия: {report.get('meta', {}).get('strategy', '—')}",
        f"Сделок: {summary['total']} | Win rate: {summary['win_rate_pct']:.2f}% | Net PnL: ${summary['sum_net_pnl_usd']:+.2f}",
        f"Средний результат: {summary['avg_realized_pct']:+.3f}% | Медиана: {summary['median_realized_pct']:+.3f}%",
        f"Упущенный upside: Σ {summary['sum_missed_upside_pct']:+.3f}% | Избежимый loss: Σ {summary['sum_avoidable_loss_pct']:+.3f}%",
        f"Сигналы риска: late polling={summary['late_polling_signals']}, high-vol losses={summary['high_vol_losses_count']}, weak P(up) losses={summary['weak_prob_up_losses_count']}",
        f"Параметрические причины: losses@ALLOW={summary.get('losses_with_allow_entry_count', 0)}, losses@prob_up>=0.60={summary.get('losses_with_high_prob_up_count', 0)}, losses@RSI>=60={summary.get('losses_with_high_rsi_count', 0)}",
        "",
        "Top losses:",
    ]
    for r in (top.get("top_losses") or [])[:4]:
        lines.append(f"• {r['ticker']} #{r['trade_id']}: {r['realized_pct']:+.2f}% (exit={r['exit_signal']})")
    practical = report.get("practical_parameter_suggestions") or []
    if practical:
        lines.append("")
        lines.append("Практические изменения:")
        for p in practical[:4]:
            lines.append(
                f"• {p.get('parameter')}: {p.get('current')} -> {p.get('proposed')} | {p.get('why')}"
            )
    critical = report.get("critical_case_analysis") or []
    if critical:
        lines.append("")
        lines.append("Критичные кейсы:")
        for c in critical[:4]:
            lines.append(
                f"• {c.get('ticker')} #{c.get('trade_id')}: {c.get('diagnosis')} | action: {c.get('action')}"
            )
    if report.get("llm"):
        llm = report["llm"]
        lines.append("")
        lines.append("LLM:")
        if llm.get("status") == "ok":
            ana = llm.get("analysis")
            if isinstance(ana, dict):
                lines.append(json.dumps(ana, ensure_ascii=False, indent=2)[:1800])
            else:
                lines.append(str(ana)[:1800])
        else:
            lines.append(f"{llm.get('status')}: {llm.get('reason')}")
    return "\n".join(lines)
