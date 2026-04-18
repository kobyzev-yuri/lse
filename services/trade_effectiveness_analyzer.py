from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from report_generator import get_engine, load_trade_history, compute_closed_trade_pnls
from services.recommend_5m import fetch_5m_ohlc
from services.deal_params_5m import normalize_entry_context
from config_loader import load_config, is_editable_config_env_key

# Дольше — считаем позицию «подвисшей» для пристрастного разбора порогов входа vs выхода.
LONG_HOLD_MINUTES = 7 * 24 * 60
ENTRY_REASONING_ANALYZER_MAX = 420

# Пояснения для UI/LLM (исторически «late_polling» вводит в заблуждение — это не опрос Telegram/cron).
ANALYZER_METRIC_DEFINITIONS: Dict[str, str] = {
    "late_polling_signals": (
        "Число сделок, где одновременно: (1) цена выхода отстает от максимума High в 5m-окне "
        "вход→выход более чем на ~0.4% (|exit−MFE|/MFE>0.004), (2) missed_upside_pct>0.3%. "
        "Прокси «вышли заметно ниже пика окна» (часто норма для лимитного тейка). "
        "НЕ связано с интервалом cron GAME_5M_SIGNAL_CRON_MINUTES."
    ),
    "exit_below_window_mfe_count": (
        "То же значение, что late_polling_signals — предпочтительное имя для интерпретации."
    ),
    "sum_avoidable_loss_pct": (
        "Сумма по сделкам max(0, realized_pct − preventable_worst_pct), "
        "где preventable_worst_pct = (min Low окна / entry − 1)·100. "
        "На прибыльных long сумма может быть большой: вы сильно лучше, чем «если бы вышли на минимуме окна» — "
        "это не «сумма убытков, которых можно было избежать»."
    ),
    "sum_missed_upside_pct": (
        "Сумма max(0, potential_best_pct − realized_pct), potential_best от max High окна; "
        "характеризует недобор до пика окна после фиксации."
    ),
    "likely_late_polling": (
        "Поле по сделке (boolean): |exit−max High окна|/max High > 0.004. "
        "Историческое имя; семантика — «выход не у пика MFE окна», не задержка опроса."
    ),
    "high_vol_losses_count": (
        "Число убыточных сделок, где на входе entry_vol_5m_pct ≥ 0.6 (волатильность из context_json)."
    ),
    "weak_prob_up_losses_count": (
        "Убыточные сделки с entry_prob_up < 0.55 — модель/прогноз на входе не давали высокой вероятности роста."
    ),
    "by_exit_signal": (
        "Агрегаты по типу выхода (exit_signal): count, avg_realized_pct, avg_missed_pct. "
        "Показывает, какой сигнал закрытия доминирует и насколько он «дорогой» по missed upside."
    ),
    "realized_pct": (
        "Результат сделки в % от cost basis из net_pnl (как в отчёте); для рядов также считается realized_log_return = log(1+p/100)."
    ),
}

# Краткий конспект для LLM: как устроен отчёт и где живёт торговая логика (без выдумывания путей).
ANALYZER_LLM_ALGORITHM_DIGEST: Dict[str, Any] = {
    "report_role": (
        "Постфактум по закрытым сделкам: загрузка истории, 5m OHLC на интервал вход→выход, "
        "сравнение факта выхода с max/min ценами окна и снимком context_json на входе."
    ),
    "not_in_report": (
        "Отчёт не воспроизводит внутрибаровый тайминг исполнения лимитов и не знает реальной задержки брокера; "
        "не выводить «медленный cron» из late_polling_signals / exit_below_window_mfe_count."
    ),
    "window_metrics": {
        "ohlc_source": "services.recommend_5m.fetch_5m_ohlc, таймзона баров America/New_York",
        "slice": "строки 5m где entry_ts <= datetime <= exit_ts",
        "mfe_price": "max(High) окна → potential_best_pct = (mfe/entry−1)*100",
        "mae_price": "min(Low) окна → preventable_worst_pct = (mae/entry−1)*100",
        "missed_upside_pct": "max(0, potential_best_pct − realized_pct)",
        "avoidable_loss_pct": "max(0, realized_pct − preventable_worst_pct)",
        "exit_vs_mfe_flag": "|exit−mfe|/mfe > 0.004 → likely_late_polling (переименование в UI: ниже MFE окна)",
        "late_polling_signals_summary": (
            "число сделок с likely_late_polling И missed_upside_pct > 0.003 (0.3%)"
        ),
    },
    "entry_snapshot": (
        "Поля входа (rsi_5m, prob_up, decision_rule_params, …) из context_json сделки через "
        "services.deal_params_5m.normalize_entry_context — это снимок на момент входа, не текущий config."
    ),
    "runtime_trading_logic": (
        "Живые сигналы входа: services.recommend_5m.get_decision_5m (версия и пороги — "
        "current_decision_rule_params в meta). Управление позицией/стоп/тейк/время: services.game_5m "
        "(эффективные stop/take и max_position — в current_decision_rule_params.exit_strategy)."
    ),
    "code_map": [
        {
            "path": "services/trade_effectiveness_analyzer.py",
            "role": "Сбор отчёта, агрегаты summary, эвристики practical_parameter_suggestions, game_5m_config_hints, вызов LLM.",
        },
        {
            "path": "services/recommend_5m.py",
            "role": "Правила STRONG_BUY/BUY/HOLD/SELL, чтение GAME_5M_* порогов, get_decision_5m_rule_thresholds.",
        },
        {
            "path": "services/game_5m.py",
            "role": "Стоп-лосс, тейк-профит, лимиты удержания, выход по времени сессии и связанные ветки.",
        },
        {
            "path": "services/deal_params_5m.py",
            "role": "Нормализация context_json при записи/чтении сделки.",
        },
        {
            "path": "services/game5m_param_hypothesis_backtest.py",
            "role": "Офлайн 5m-реплей: открытые BUY в trade_history (или legacy JSON), недобор (missed upside) в bundle анализатора → mergeable_recommendations; "
            "CLI: scripts/backtest_game5m_param_hypotheses.py --mode open|json|bundle.",
        },
        {
            "path": "report_generator.py",
            "role": "load_trade_history, compute_closed_trade_pnls — источник списка закрытых сделок.",
        },
    ],
    "advice_discipline": [
        "Сначала проверь metric_definitions и этот digest — не путай метрики окна с инфраструктурой cron.",
        "Привязывай выводы к конкретным trade_id / ticker из top_cases, entry_underperformance_review или trade_effects.",
        "Перенастройка: логическое имя параметра → env_key из algorithm_context.parameter_to_env_key (или полное GAME_5M_*).",
        "Если проблема в ветвлении/формуле, а не в пороге — algorithm_change_proposals с path из code_map и именем функции/ветки.",
        "Оценка impact в expected_impact — осторожные числа; validation_plan — повторный прогон анализатора на следующем окне или ограниченный paper-run.",
    ],
    # Как крон решает тейк (без полного исходника — достаточно для подбора GAME_5M_*).
    "game_5m_take_exit_runtime": {
        "primary_code": "services/game_5m.py: _effective_take_profit_pct, _effective_stop_loss_pct, should_close_position",
        "cron_entrypoint": "scripts/send_sndk_signal_cron.py вызывает should_close_position(..., momentum_2h_pct с карточки 5m)",
        "take_threshold_formula": (
            "cap = GAME_5M_TAKE_PROFIT_PCT или GAME_5M_TAKE_PROFIT_PCT_<TICKER> (потолок). "
            "Если momentum_2h_pct >= GAME_5M_TAKE_PROFIT_MIN_PCT: "
            "эффективный_тейк = min(momentum_2h_pct × GAME_5M_TAKE_MOMENTUM_FACTOR, cap). "
            "Иначе эффективный_тейк = cap. "
            "Закрытие TAKE_PROFIT если нереализованный % (по max(close, bar_high) vs entry) >= эффективный_тейк − 0.05."
        ),
        "env_keys_take": [
            "GAME_5M_TAKE_PROFIT_PCT",
            "GAME_5M_TAKE_PROFIT_PCT_<TICKER>",
            "GAME_5M_TAKE_PROFIT_MIN_PCT",
            "GAME_5M_TAKE_MOMENTUM_FACTOR",
        ],
        "env_keys_stop_time": [
            "GAME_5M_STOP_LOSS_ENABLED",
            "GAME_5M_STOP_LOSS_PCT",
            "GAME_5M_STOP_TO_TAKE_RATIO",
            "GAME_5M_STOP_LOSS_MIN_PCT",
            "GAME_5M_MAX_POSITION_MINUTES",
            "GAME_5M_MAX_POSITION_MINUTES_<TICKER>",
            "GAME_5M_MAX_POSITION_DAYS",
            "GAME_5M_MAX_POSITION_DAYS_<TICKER>",
            "GAME_5M_SESSION_END_EXIT_MINUTES",
            "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
            "GAME_5M_EXIT_ONLY_TAKE",
            "GAME_5M_EARLY_DERISK_*",
            "GAME_5M_ALLOW_PYRAMID_BUY",
            "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED",
            "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT",
            "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
        ],
        "note_on_meta_take": (
            "В report.meta.current_decision_rule_params.exit_strategy числа take_profit_pct_effective / stop_loss_pct_effective "
            "— снимок при momentum_2h=None (часто совпадает с потолком тейка). Реальный порог в кроне зависит от текущего momentum_2h на баре."
        ),
        "sell_does_not_close": (
            "Сигнал решения SELL по 5m не закрывает уже открытый long; только TAKE_PROFIT / STOP_LOSS / TIME_EXIT / TIME_EXIT_EARLY (см. should_close_position)."
        ),
        "tuning_hint_from_report": (
            "Много TAKE_PROFIT и высокий sum_missed_upside_pct → чаще трогают TAKE_MOMENTUM_FACTOR или потолок тейка; "
            "много убытков при vol — VOLATILITY_*, SELL_CONFIRM_BARS; долгое удержание — MAX_POSITION_*."
        ),
    },
}


def _analyzer_state_path() -> Path:
    """
    Куда писать «память» анализатора о последних параметрах.
    По умолчанию — local/analyzer_state.json (внутри repo). Можно переопределить env ANALYZER_STATE_PATH.
    """
    raw = (os.environ.get("ANALYZER_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    root = Path(__file__).resolve().parent.parent
    return root / "local" / "analyzer_state.json"


def _load_analyzer_state() -> Dict[str, Any]:
    p = _analyzer_state_path()
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_analyzer_state(state: Dict[str, Any]) -> None:
    p = _analyzer_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Не валим API из‑за невозможности сохранить «память» (read-only FS, sandbox, etc.)
        return


def _extract_game5m_config_snapshot(current_rules: Dict[str, Any]) -> Dict[str, Any]:
    cfg = current_rules.get("config") if isinstance(current_rules.get("config"), dict) else {}
    exit_s = current_rules.get("exit_strategy") if isinstance(current_rules.get("exit_strategy"), dict) else {}
    return {
        "rule_version": current_rules.get("rule_version"),
        "signal_cron_minutes": current_rules.get("signal_cron_minutes"),
        "config": dict(cfg) if isinstance(cfg, dict) else {},
        "exit_strategy": {
            k: exit_s.get(k)
            for k in (
                "max_position_minutes",
                "stop_loss_enabled",
                "stop_loss_pct_effective",
                "take_profit_pct_effective",
                "GAME_5M_TAKE_MOMENTUM_FACTOR",
                "GAME_5M_EXIT_ONLY_TAKE",
                "GAME_5M_SESSION_END_EXIT_MINUTES",
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                "GAME_5M_EARLY_DERISK_ENABLED",
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES",
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT",
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW",
                "GAME_5M_ALLOW_PYRAMID_BUY",
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED",
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT",
                "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT",
            )
        },
    }


def _diff_flat_config(prev: Dict[str, Any], cur: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Плоский diff по ключам GAME_5M_* в meta: что изменилось со времени прошлого прогона анализатора."""
    out: List[Dict[str, Any]] = []
    p_cfg = prev.get("config") if isinstance(prev.get("config"), dict) else {}
    c_cfg = cur.get("config") if isinstance(cur.get("config"), dict) else {}
    keys = sorted(set([*p_cfg.keys(), *c_cfg.keys()]))
    for k in keys:
        pv = p_cfg.get(k)
        cv = c_cfg.get(k)
        if pv != cv:
            out.append({"env_key": k, "prev": pv, "current": cv})
    p_ex = prev.get("exit_strategy") if isinstance(prev.get("exit_strategy"), dict) else {}
    c_ex = cur.get("exit_strategy") if isinstance(cur.get("exit_strategy"), dict) else {}
    keys2 = sorted(set([*p_ex.keys(), *c_ex.keys()]))
    for k in keys2:
        pv = p_ex.get(k)
        cv = c_ex.get(k)
        if pv != cv:
            out.append({"env_key": f"exit_strategy.{k}", "prev": pv, "current": cv})
    return out


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
    entry_price_forecast_5m_summary: Optional[str]
    entry_prob_up: Optional[float]
    entry_prob_down: Optional[float]
    entry_news_impact: Optional[str]
    entry_advice: Optional[str]
    entry_decision: Optional[str]
    entry_reasoning: Optional[str]
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


def _filter_closed_trades_for_focus(
    closed: List[Any],
    tickers: Optional[List[str]] = None,
    trade_ids: Optional[List[int]] = None,
) -> List[Any]:
    """Узкая выборка: по списку тикеров и/или id закрывающей сделки (trade_id из TradePnL)."""
    if not tickers and not trade_ids:
        return closed
    tid_set = {int(x) for x in trade_ids} if trade_ids else None
    tkr_set = {str(t).strip().upper() for t in tickers if str(t).strip()} if tickers else None
    out: List[Any] = []
    for t in closed:
        if tid_set is not None:
            try:
                tid = int(getattr(t, "trade_id", 0) or 0)
            except (TypeError, ValueError):
                tid = 0
            if tid not in tid_set:
                continue
        if tkr_set is not None:
            tkr = str(getattr(t, "ticker", "") or "").strip().upper()
            if tkr not in tkr_set:
                continue
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
                # Историческое имя likely_late_polling: фактически «выход не у MFE high окна», не задержка cron.
                likely_late_polling = abs(exit_p - mfe_price) / mfe_price > 0.004 if mfe_price > 0 else False
            except Exception:
                pass

        entry_ctx = normalize_entry_context(getattr(t, "context_json", None))
        raw_decision = entry_ctx.get("decision")
        entry_decision = str(raw_decision).strip() if raw_decision is not None and str(raw_decision).strip() else None
        raw_reason = entry_ctx.get("reasoning")
        entry_reasoning: Optional[str] = None
        if isinstance(raw_reason, str):
            rs = raw_reason.strip()
            if rs:
                entry_reasoning = (
                    rs[:ENTRY_REASONING_ANALYZER_MAX] + "…" if len(rs) > ENTRY_REASONING_ANALYZER_MAX else rs
                )
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
                entry_price_forecast_5m_summary=(
                    (lambda v: (str(v).strip()[:2000] or None) if v is not None else None)(
                        entry_ctx.get("price_forecast_5m_summary")
                    )
                ),
                entry_prob_up=_safe_float(entry_ctx.get("prob_up")),
                entry_prob_down=_safe_float(entry_ctx.get("prob_down")),
                entry_news_impact=(entry_ctx.get("kb_news_impact") or None),
                entry_advice=(entry_ctx.get("entry_advice") or None),
                entry_decision=entry_decision,
                entry_reasoning=entry_reasoning,
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
    missed_on_wins = [(e.missed_upside_pct or 0.0) for e in wins if e.missed_upside_pct is not None]
    wins_missed_ge_1 = [e for e in wins if (e.missed_upside_pct or 0.0) >= 1.0]
    stuck_ge_7d = [e for e in effects if e.hold_minutes >= LONG_HOLD_MINUTES]
    stuck_poor = [
        e
        for e in stuck_ge_7d
        if e.realized_pct <= 0.15
    ]
    missing_entry_decision = sum(1 for e in effects if not (e.entry_decision or "").strip())
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
        "sum_missed_upside_pct_on_wins": round(float(sum(missed_on_wins)), 3) if missed_on_wins else 0.0,
        "avg_missed_upside_pct_on_wins": round(float(np.mean(missed_on_wins)), 3) if missed_on_wins else None,
        "wins_with_missed_upside_ge_1pct_count": len(wins_missed_ge_1),
        "sum_avoidable_loss_pct": round(float(sum(avoidable)), 3),
        "avg_avoidable_loss_pct": round(float(np.mean(avoidable)), 3) if avoidable else None,
        "late_polling_signals": late_polling_count,
        "exit_below_window_mfe_count": late_polling_count,
        "high_vol_losses_count": len(high_vol_losses),
        "weak_prob_up_losses_count": len(weak_prob_entries),
        "negative_news_losses_count": len(neg_news_losses),
        "losses_with_allow_entry_count": len(losses_with_allow),
        "losses_with_high_prob_up_count": len(losses_with_high_prob),
        "losses_with_high_rsi_count": len(losses_with_high_rsi),
        "decision_rule_versions": rule_versions,
        "by_exit_signal": by_exit,
        "long_hold_ge_7d_count": len(stuck_ge_7d),
        "long_hold_ge_7d_poor_outcome_count": len(stuck_poor),
        "trades_missing_entry_decision_count": missing_entry_decision,
    }


def _suggested_entry_env_keys(e: TradeEffect) -> List[str]:
    """
    Какие ключи config.env логично пересмотреть, если результат входа плохий или позиция долго «висела».
    Тип входа (STRONG_BUY vs BUY) задаёт разные ветки в recommend_5m; точную ветку без reasoning не восстановить —
    для BUY перечисляем оба семейства порогов.
    """
    d = (e.entry_decision or "").strip().upper()
    keys: List[str] = []
    if d == "STRONG_BUY":
        keys = ["GAME_5M_MOMENTUM_STRONG_BUY_MIN", "GAME_5M_RSI_STRONG_BUY_MAX"]
    elif d == "BUY":
        keys = [
            "GAME_5M_RTH_MOMENTUM_BUY_MIN",
            "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
            "GAME_5M_RSI_BUY_MAX",
            "GAME_5M_PRICE_TO_LOW5D_MULT_MAX",
            "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
            "GAME_5M_PREMARKET_MOMENTUM_BUY_MIN",
            "GAME_5M_PREMARKET_MOMENTUM_BLOCK_BELOW",
        ]
    else:
        keys = [
            "GAME_5M_MOMENTUM_STRONG_BUY_MIN",
            "GAME_5M_RSI_STRONG_BUY_MAX",
            "GAME_5M_RTH_MOMENTUM_BUY_MIN",
            "GAME_5M_RSI_FOR_MOMENTUM_BUY_MAX",
        ]
    if e.hold_minutes >= LONG_HOLD_MINUTES:
        keys = keys + [
            "GAME_5M_MAX_POSITION_DAYS",
            "GAME_5M_MAX_POSITION_MINUTES",
            "GAME_5M_SESSION_END_EXIT_MINUTES",
            "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
        ]
    seen: set[str] = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _build_entry_underperformance_review(
    effects: List[TradeEffect],
    *,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """
    Сделки почти без профита / в минусе и особенно с удержанием ≥7 суток —
    явная привязка к тексту причины входа и к порогам из decision_rule_params на момент BUY.
    """
    scored: List[tuple[float, TradeEffect]] = []
    for e in effects:
        long_hold = e.hold_minutes >= LONG_HOLD_MINUTES
        poor = e.realized_pct <= 0.15
        loss = e.realized_pct < 0
        score = 0.0
        if loss:
            score += 2.0 + max(0.0, -e.realized_pct) / 15.0
        elif poor:
            score += 0.8
        if long_hold:
            score += 1.2
            if poor or loss:
                score += 1.5
        if score < 0.75:
            continue
        scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    for _, e in scored[:limit]:
        hold_d = round(e.hold_minutes / (24 * 60), 2)
        poor_out = e.realized_pct <= 0.15
        long_hold = e.hold_minutes >= LONG_HOLD_MINUTES
        if long_hold and poor_out:
            note = (
                "Долгое удержание (≥7 суток) при слабом или отрицательном результате: "
                "в первую очередь проверить пороги входа и лимиты удержания (см. suggested_config_env_review)."
            )
        elif long_hold:
            note = "Долгое удержание: даже при плюсе стоит проверить MAX_POSITION_* и условия выхода по времени."
        else:
            note = "Слабый или отрицательный результат — пересмотреть пороги ветки входа (см. suggested_config_env_review и reasoning)."
        out.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "hold_days": hold_d,
                "hold_minutes": round(e.hold_minutes, 1),
                "long_hold_ge_7d": e.hold_minutes >= LONG_HOLD_MINUTES,
                "realized_pct": round(e.realized_pct, 3),
                "exit_signal": e.exit_signal,
                "entry_decision": e.entry_decision,
                "entry_reasoning_excerpt": (e.entry_reasoning or "")[:320],
                "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
                "entry_rsi_5m": e.entry_rsi_5m,
                "decision_rule_params_at_entry": e.decision_rule_params,
                "suggested_config_env_review": _suggested_entry_env_keys(e),
                "note": note,
            }
        )
    return out


def _top_cases(effects: List[TradeEffect], limit: int = 8) -> Dict[str, List[Dict[str, Any]]]:
    def row(e: TradeEffect) -> Dict[str, Any]:
        return {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "entry_ts": e.entry_ts.isoformat(),
            "exit_ts": e.exit_ts.isoformat(),
            "hold_minutes": round(e.hold_minutes, 1),
            "exit_signal": e.exit_signal,
            "realized_pct": round(e.realized_pct, 3),
            "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
            "avoidable_loss_pct": round(e.avoidable_loss_pct or 0.0, 3),
            "entry_decision": e.entry_decision,
            "entry_reasoning": e.entry_reasoning,
            "entry_rsi_5m": e.entry_rsi_5m,
            "entry_vol_5m_pct": e.entry_vol_5m_pct,
            "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
            "entry_prob_up": e.entry_prob_up,
            "entry_price_forecast_5m_summary": e.entry_price_forecast_5m_summary,
            "entry_news_impact": e.entry_news_impact,
            "entry_advice": e.entry_advice,
            "decision_rule_version": e.decision_rule_version,
            "decision_rule_params": e.decision_rule_params,
        }

    by_missed = sorted(effects, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    by_loss = sorted(effects, key=lambda x: x.realized_pct)[:limit]
    winners = [e for e in effects if e.realized_pct > 0]
    by_win_missed = sorted(winners, key=lambda x: x.missed_upside_pct or 0.0, reverse=True)[:limit]
    return {
        "top_missed_upside": [row(e) for e in by_missed],
        "top_losses": [row(e) for e in by_loss],
        "top_profitable_missed_upside": [row(e) for e in by_win_missed],
    }


def _trade_effect_detail_dict(e: TradeEffect) -> Dict[str, Any]:
    """Полная сериализация сделки для внешнего JSON (локальный LLM, скрипты, jq)."""
    return {
        "trade_id": e.trade_id,
        "ticker": e.ticker,
        "entry_ts": e.entry_ts.isoformat(),
        "exit_ts": e.exit_ts.isoformat(),
        "hold_minutes": round(e.hold_minutes, 2),
        "qty": e.qty,
        "entry_price": e.entry_price,
        "exit_price": e.exit_price,
        "net_pnl": round(e.net_pnl, 4),
        "realized_pct": round(e.realized_pct, 4),
        "realized_log_return": round(e.realized_log_return, 6) if e.realized_log_return > -900 else None,
        "exit_signal": e.exit_signal,
        "exit_strategy": e.exit_strategy,
        "potential_best_pct": None if e.potential_best_pct is None else round(e.potential_best_pct, 4),
        "preventable_worst_pct": None if e.preventable_worst_pct is None else round(e.preventable_worst_pct, 4),
        "missed_upside_pct": None if e.missed_upside_pct is None else round(e.missed_upside_pct, 4),
        "avoidable_loss_pct": None if e.avoidable_loss_pct is None else round(e.avoidable_loss_pct, 4),
        "likely_late_polling": e.likely_late_polling,
        "entry_decision": e.entry_decision,
        "entry_reasoning": e.entry_reasoning,
        "entry_rsi_5m": e.entry_rsi_5m,
        "entry_vol_5m_pct": e.entry_vol_5m_pct,
        "entry_momentum_2h_pct": e.entry_momentum_2h_pct,
        "entry_prob_up": e.entry_prob_up,
        "entry_prob_down": e.entry_prob_down,
        "entry_price_forecast_5m_summary": e.entry_price_forecast_5m_summary,
        "entry_news_impact": e.entry_news_impact,
        "entry_advice": e.entry_advice,
        "decision_rule_version": e.decision_rule_version,
        "decision_rule_params": e.decision_rule_params,
        "suggested_config_env_review_for_entry": _suggested_entry_env_keys(e),
    }


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


def _llm_trade_analyzer_response_parsed_ok(parsed: Any) -> bool:
    """Ожидаемый контракт ответа модели: объект с priorities или in_algorithm_parameter_changes."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("priorities") is not None:
        return True
    if parsed.get("in_algorithm_parameter_changes") is not None:
        return True
    return False


def _analyzer_llm_max_output_tokens(*, game_5m_config_focus: bool) -> int:
    """Лимит completion: большие промпты + длинный JSON; 2000 часто режет gpt-5.x на середине объекта."""
    raw = (os.environ.get("ANALYZER_LLM_MAX_COMPLETION_TOKENS") or "").strip()
    if raw.isdigit():
        return max(1024, min(16384, int(raw)))
    return 6144 if game_5m_config_focus else 4096


def _build_llm_recommendations(
    payload: Dict[str, Any],
    *,
    game_5m_config_focus: bool = False,
) -> Optional[Dict[str, Any]]:
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
            "metric_definitions": meta.get("metric_definitions") or ANALYZER_METRIC_DEFINITIONS,
            "algorithm_digest": ANALYZER_LLM_ALGORITHM_DIGEST,
            "parameter_to_env_key": dict(PARAM_TO_ENV_KEY),
            "llm_critical_notes": [
                "summary.late_polling_signals НЕ означает задержку опроса/cron. См. metric_definitions.late_polling_signals.",
                "Жёсткий запрет: не включать GAME_5M_SIGNAL_CRON_MINUTES в in_algorithm_parameter_changes, config_env_proposals и monitoring_fixes, если главное «доказательство» — late_polling_signals / exit_below_window_mfe_count / формулировки про «late polling» или «запаздывание опроса».",
                "Для недобора до MFE при TAKE_PROFIT предлагай TAKE_PROFIT_*, TAKE_MOMENTUM_FACTOR, trailing/лимиты — не cron.",
                "sum_avoidable_loss_pct на прибыльных сделках может быть велик — см. metric_definitions.sum_avoidable_loss_pct.",
                "Корреляции в отчёте (vol, prob_up, exit_signal) не доказывают причинность — указывай confidence и validation_plan.",
            ],
            "parameter_to_env_key_hint": {
                "momentum_buy_min": "GAME_5M_RTH_MOMENTUM_BUY_MIN",
                "rsi_buy_max": "GAME_5M_RSI_BUY_MAX",
                "rsi_strong_buy_max": "GAME_5M_RSI_STRONG_BUY_MAX",
                "volatility_wait_min": "GAME_5M_VOLATILITY_WAIT_MIN",
                "signal_cron_minutes": "GAME_5M_SIGNAL_CRON_MINUTES",
            },
            "scope_for_in_algorithm_changes": [
                "entry thresholds and guards from decision_rule_params",
                "config flags and numeric limits from current_decision_rule_params.config",
                "exit cadence and de-risk knobs from current_decision_rule_params.exit_strategy",
            ],
            "hard_constraints": [
                "read algorithm_digest before interpreting summary and top_cases",
                "FORBIDDEN: GAME_5M_SIGNAL_CRON_MINUTES or signal_cron_minutes in in_algorithm_parameter_changes / config_env_proposals / monitoring_fixes when the cited evidence is late_polling_signals, exit_below_window_mfe_count, or wording like 'late polling' / 'polling delay' — those metrics are exit vs 5m window MFE, not cron latency",
                "tie at least one priority or parameter_change to concrete trade_id or ticker from the report when evidence exists",
                "for each in_algorithm_parameter_changes include env_key from parameter_to_env_key when the parameter maps",
                "propose changes only with concrete fields from current_decision_rule_params when tuning thresholds",
                "if there is no matching field, put proposal into algorithm_change_proposals with code_areas from algorithm_digest.code_map",
                "algorithm_change_proposals: name the function or branch (e.g. RSI sell branch), not only the file",
                "do not suggest vague ideas without target parameter, env_key, or code area",
            ],
        }
        if game_5m_config_focus:
            algorithm_context["game_5m_exit_and_tuning_env_keys"] = [
                "GAME_5M_TAKE_PROFIT_PCT",
                "GAME_5M_TAKE_PROFIT_PCT_<TICKER>",
                "GAME_5M_TAKE_PROFIT_MIN_PCT",
                "GAME_5M_TAKE_MOMENTUM_FACTOR",
                "GAME_5M_MAX_POSITION_DAYS",
                "GAME_5M_MAX_POSITION_DAYS_<TICKER>",
                "GAME_5M_MAX_POSITION_MINUTES",
                "GAME_5M_MAX_POSITION_MINUTES_<TICKER>",
                "GAME_5M_SESSION_END_EXIT_MINUTES",
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                "GAME_5M_STOP_LOSS_ENABLED",
                "GAME_5M_STOP_LOSS_PCT",
                "GAME_5M_STOP_TO_TAKE_RATIO",
                "GAME_5M_SIGNAL_CRON_MINUTES",
                "GAME_5M_COOLDOWN_MINUTES",
                "GAME_5M_MAX_ATR_5M_PCT",
                "GAME_5M_MIN_VOLUME_VS_AVG_PCT",
                "GAME_5M_SELL_CONFIRM_BARS",
                "GAME_5M_VOLATILITY_WAIT_MIN",
            ]
            algorithm_context["focus_instruction"] = (
                "Это УЗКИЙ отчёт по нескольким дням и/или выбранным тикерам/сделкам. "
                "Приоритет: конкретные правки config.env из списка game_5m_exit_and_tuning_env_keys "
                "(полное имя ключа, например GAME_5M_TAKE_PROFIT_PCT_SNDK). "
                "Числа предлагай осторожно, с обоснованием по метрикам отчёта (missed_upside, exit_signal, losses)."
            )
        llm_input: Dict[str, Any] = {"algorithm_context": algorithm_context, "report": payload}
        if isinstance(payload.get("game_5m_config_hints"), list):
            llm_input["heuristic_hints"] = payload["game_5m_config_hints"]
        system_prompt = (
            "Ты senior quant и инженер по торговым системам; у тебя есть только входной JSON (отчёт + algorithm_context).\n"
            "Твоя задача: предложить дельные улучшения — в первую очередь перенастройка GAME_5M_* / порогов из "
            "current_decision_rule_params, во вторую — точечные изменения кода (ветки входа/выхода), если порогом не лечится.\n"
            "Сначала прочитай algorithm_context.algorithm_digest (как считаются метрики окна и откуда context_json), "
            "затем metric_definitions и llm_critical_notes.\n"
            "Используй только факты из отчёта; не выдумывай сделки, тикеры и значения, которых нет во входе.\n"
            "В reason_from_metrics указывай trade_id и/или ticker, если опираешься на top_cases, entry_underperformance_review "
            "или trade_effects.\n"
            "Для параметра из practical_parameter_suggestions или heuristic_hints подставь env_key из "
            "algorithm_context.parameter_to_env_key (или сам ключ GAME_5M_*).\n"
            "Никогда не связывай GAME_5M_SIGNAL_CRON_MINUTES с late_polling_signals: это разные вещи (см. llm_critical_notes). "
            "Не пиши в priorities фразы про «late polling» как про инфраструктуру — говори «выход ниже MFE окна» или «недобор после тейка».\n"
            "Для тейка/стопа опирайся на algorithm_digest.game_5m_take_exit_runtime и "
            "report.meta.current_decision_rule_params.exit_strategy (в т.ч. strategy_params_snapshot, TAKE_MOMENTUM_FACTOR).\n\n"
            "Верни ТОЛЬКО валидный JSON без markdown и без пояснений вне JSON со следующими ключами:\n"
            "{\n"
            "  \"priorities\": [\"...\"],\n"
            "  \"in_algorithm_parameter_changes\": [\n"
            "    {\n"
            "      \"parameter\": \"...\",\n"
            "      \"env_key\": \"GAME_5M_... или пусто если нет в parameter_to_env_key\",\n"
            "      \"current\": \"...\",\n"
            "      \"proposed\": \"число или true/false — без фраз на русском/английском; для GAME_5M_*_PCT только цифры\",\n"
            "      \"where_used\": \"services/recommend_5m.py|services/game_5m.py|config.env\",\n"
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
        if game_5m_config_focus:
            system_prompt += (
                "\nФОКУС-РЕЖИМ: в том же корневом JSON добавь ключ \"config_env_proposals\" (массив 1–8 объектов) "
                "ПЕРЕД финальной закрывающей скобкой документа — т.е. после \"validation_plan\" поставь запятую и массив:\n"
                "\"config_env_proposals\": [\n"
                "  {\"env_key\": \"GAME_5M_TAKE_PROFIT_PCT_SNDK\", \"proposed_value\": \"6.5\", "
                "\"reason_from_metrics\": \"...\", \"confidence\": \"medium\"}\n"
                "]\n"
                "Ключи env_key только из algorithm_context.game_5m_exit_and_tuning_env_keys; для тикера — суффикс _TICKER.\n"
            )
        max_tok = _analyzer_llm_max_output_tokens(game_5m_config_focus=game_5m_config_focus)
        out = llm.generate_response(
            messages=[{"role": "user", "content": json.dumps(llm_input, ensure_ascii=False, indent=2)}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=max_tok,
        )
        text = out.get("response") or ""
        finish_reason = out.get("finish_reason")
        parsed = _parse_llm_json_response(text)
        parse_ok = _llm_trade_analyzer_response_parsed_ok(parsed)
        status = "ok" if parse_ok else "parse_failed"
        warnings: List[str] = []
        if finish_reason == "length":
            warnings.append(
                "Ответ обрезан по лимиту completion (finish_reason=length). "
                "Задайте ANALYZER_LLM_MAX_COMPLETION_TOKENS или сократите отчёт (меньше дней / без trade_effects)."
            )
        if not parse_ok:
            if not (text or "").strip():
                warnings.append("Пустой ответ модели — проверьте лимиты API и ключ.")
            elif finish_reason != "length":
                warnings.append("Ответ не распознан как JSON с priorities — см. raw_fragment в analysis.")
            frag = (text or "")[:4000]
            if not isinstance(parsed, dict):
                parsed = {"raw_text": text or ""}
            if frag:
                parsed["raw_fragment"] = frag
        return {
            "status": status,
            "parse_ok": parse_ok,
            "finish_reason": finish_reason,
            "warnings": warnings,
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

    # 3) Missed upside → конкретный env (иначе practical не попадает в auto_config_override)
    missed = [e.missed_upside_pct or 0.0 for e in effects]
    mean_missed = float(np.mean(missed)) if missed else 0.0
    sum_missed = float(summary.get("sum_missed_upside_pct", 0) or 0)
    if mean_missed >= 1.0 and sum_missed >= 8:
        cfg = load_config()
        try:
            cur_f = float(str(cfg.get("GAME_5M_TAKE_MOMENTUM_FACTOR") or "1.0").replace(",", "."))
        except (ValueError, TypeError):
            cur_f = 1.0
        proposed_f = round(min(cur_f + 0.05, 1.35), 3)
        if proposed_f > cur_f + 1e-9:
            suggestions.append(
                {
                    "parameter": "take_momentum_factor",
                    "current": cur_f,
                    "proposed": proposed_f,
                    "why": (
                        f"Средний missed_upside={mean_missed:.2f}%, суммарно={sum_missed:.2f}% — "
                        "поднять factor тейка от 2h-импульса (частичный недобор vs high окна)."
                    ),
                    "expected_effect": "Выше динамическая цель при сильном импульсе; согласовать с GAME_5M_TAKE_PROFIT_PCT / MIN_PCT.",
                }
            )

    # 4) Выход заметно ниже MFE окна (счётчик late_polling_signals — не про cron)
    late = int(summary.get("late_polling_signals", 0))
    if late >= 3:
        suggestions.append(
            {
                "parameter": "take_vs_window_mfe",
                "current": "фиксированный % тейка",
                "proposed": "пересмотреть тейк/трейлинг относительно intraday high (см. missed_upside)",
                "why": (
                    f"В {late} сделках цена выхода заметно ниже max High 5m-окна вход→выход при недоборе "
                    f"(late_polling_signals / exit_below_window_mfe_count; это не метрика cron)."
                ),
                "expected_effect": "Меньше «недожатого» запаса после тейка, если цель — ближе к пику окна.",
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
            reason_parts.append("exit below window MFE (not cron)")
        if e.hold_minutes >= LONG_HOLD_MINUTES:
            reason_parts.append(f"long hold {e.hold_minutes / (24 * 60):.1f}d")
        if e.entry_vol_5m_pct is not None and e.entry_vol_5m_pct >= 0.6:
            reason_parts.append(f"high vol {e.entry_vol_5m_pct:.2f}%")
        if (e.entry_news_impact or "").lower().startswith("негатив"):
            reason_parts.append("negative news at entry")
        action = "Проверить пороги входа/выхода и цель тейка относительно high окна для кейса."
        if e.exit_signal == "TAKE_PROFIT" and (e.missed_upside_pct or 0) > 1.0:
            action = (
                f"{e.ticker} #{e.trade_id}: при TAKE_PROFIT недобор {e.missed_upside_pct:.2f}% — "
                "см. GAME_5M_TAKE_MOMENTUM_FACTOR / потолок тейка (в т.ч. per-ticker GAME_5M_TAKE_PROFIT_PCT_*)."
            )
        elif e.exit_signal == "SELL" and e.realized_pct < -1.5:
            action = "Проверить условие SELL: добавить подтверждение/буфер перед выходом."
        entry_line = ""
        if e.entry_decision or e.entry_reasoning:
            ex = (e.entry_reasoning or "")[:160]
            entry_line = f"entry={e.entry_decision or '?'}" + (f" | {ex}" if ex else "")
        out.append(
            {
                "trade_id": e.trade_id,
                "ticker": e.ticker,
                "exit_signal": e.exit_signal,
                "hold_days": round(e.hold_minutes / (24 * 60), 2),
                "long_hold_ge_7d": e.hold_minutes >= LONG_HOLD_MINUTES,
                "entry_decision": e.entry_decision,
                "entry_reasoning_excerpt": (e.entry_reasoning or "")[:200],
                "entry_context_line": entry_line or None,
                "suggested_config_env_review": _suggested_entry_env_keys(e),
                "realized_pct": round(e.realized_pct, 3),
                "missed_upside_pct": round(e.missed_upside_pct or 0.0, 3),
                "diagnosis": ", ".join(reason_parts) if reason_parts else "key outlier",
                "action": action,
            }
        )
    return out


def _build_game5m_config_hints(
    effects: List[TradeEffect],
    summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Эвристики по выборке: какие ключи config.env разумно пересмотреть (без автоподстановки чисел)."""
    hints: List[Dict[str, Any]] = []
    if not effects:
        return hints
    from collections import defaultdict

    stuck_poor = [
        e for e in effects if e.hold_minutes >= LONG_HOLD_MINUTES and e.realized_pct <= 0.15
    ]
    if len(stuck_poor) >= 1:
        hints.append(
            {
                "env_key": "GAME_5M_MAX_POSITION_DAYS",
                "direction": "review_with_entry_thresholds",
                "evidence": (
                    f"{len(stuck_poor)}× удержание ≥7 суток при слабом/нулевом результате (≤0.15%) "
                    f"из {len(effects)} сделок"
                ),
                "rationale": (
                    "Долго «висит» без ощутимого плюса — чаще всего либо слишком мягкие пороги входа, "
                    "либо слишком мягкие лимиты удержания; см. также entry_underperformance_review в JSON отчёта."
                ),
            }
        )

    take_by_ticker: Dict[str, List[TradeEffect]] = defaultdict(list)
    for e in effects:
        if e.exit_signal == "TAKE_PROFIT":
            take_by_ticker[e.ticker].append(e)
    for ticker, lst in take_by_ticker.items():
        if len(lst) < 2:
            continue
        missed = [e.missed_upside_pct or 0.0 for e in lst]
        if float(np.mean(missed)) >= 1.0:
            tu = str(ticker).strip().upper()
            hints.append(
                {
                    "env_key": f"GAME_5M_TAKE_PROFIT_PCT_{tu}",
                    "direction": "review_raise_cap_or_min_pct",
                    "evidence": f"{tu}: {len(lst)}× TAKE_PROFIT, средний missed_upside {float(np.mean(missed)):.2f}%",
                    "rationale": "После тейка цена часто уходит существенно выше — потолок тейка или ранняя цель (импульсная ветка) могут быть узки.",
                }
            )

    time_exit_loss = [e for e in effects if e.exit_signal == "TIME_EXIT" and e.realized_pct <= 0]
    if len(time_exit_loss) >= 2:
        hints.append(
            {
                "env_key": "GAME_5M_SESSION_END_MIN_PROFIT_PCT",
                "direction": "review_with_SESSION_END_EXIT_MINUTES",
                "evidence": f"{len(time_exit_loss)}× TIME_EXIT без плюса",
                "rationale": "Закрытие в хвосте сессии даёт слабый или отрицательный результат — порог минимального профита или окно минут.",
            }
        )

    late = int(summary.get("late_polling_signals", 0))
    if late >= 2:
        hints.append(
            {
                "env_key": "GAME_5M_TAKE_MOMENTUM_FACTOR",
                "direction": "review_with_TAKE_PROFIT_PCT_and_missed_upside",
                "evidence": (
                    f"exit_below_window_mfe_count={late} (legacy late_polling_signals) на {len(effects)} сделках"
                ),
                "rationale": (
                    "Счётчик — цена выхода заметно ниже max High 5m-окна при недоборе; это не доказательство «медленного cron». "
                    "Сначала тейк/потолок (TAKE_PROFIT_PCT*, TAKE_MOMENTUM_FACTOR), не интервал опроса."
                ),
            }
        )

    sell_loss = [e for e in effects if e.exit_signal == "SELL" and e.realized_pct < -1.0]
    if len(sell_loss) >= 2:
        hints.append(
            {
                "env_key": "GAME_5M_SELL_CONFIRM_BARS",
                "direction": "review_raise",
                "evidence": f"{len(sell_loss)}× SELL с результатом < -1%",
                "rationale": "Усилить подтверждение перед выходом по перекупленности (RSI).",
            }
        )

    tp_all = [e for e in effects if e.exit_signal == "TAKE_PROFIT"]
    tp_small = [e for e in tp_all if 0 < e.realized_pct < 2.5]
    if len(tp_all) >= 3 and len(tp_small) >= max(3, int(0.6 * len(tp_all))):
        hints.append(
            {
                "env_key": "GAME_5M_TAKE_PROFIT_MIN_PCT",
                "direction": "review_vs_GAME_5M_TAKE_MOMENTUM_FACTOR",
                "evidence": f"Мелкие TAKE_PROFIT (<2.5%): {len(tp_small)} из {len(tp_all)}",
                "rationale": "Много ранних тейков — порог MIN_PCT для ветки «тейк от импульса» или factor тянут цель вниз относительно потолка.",
            }
        )
    return hints


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
    "take_momentum_factor": "GAME_5M_TAKE_MOMENTUM_FACTOR",
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
    "GAME_5M_TAKE_MOMENTUM_FACTOR": "GAME_5M_TAKE_MOMENTUM_FACTOR",
}


def _normalize_env_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    if v is None:
        return ""
    return str(v).strip()


def _game_5m_env_key_expects_bool(env_key: str) -> bool:
    if "_ENABLED" in env_key:
        return True
    return env_key in (
        "GAME_5M_MOMENTUM_ALLOW_CROSS_DAY_BUY",
        "GAME_5M_EARLY_USE_PREMARKET_MOMENTUM",
    )


def _game_5m_env_key_expects_number(env_key: str) -> bool:
    """Ключи GAME_5M с числовым значением в config.env (не bool)."""
    if not env_key.startswith("GAME_5M_") or _game_5m_env_key_expects_bool(env_key):
        return False
    if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
        return False
    markers = (
        "_PCT",
        "_MIN",
        "_MAX",
        "_FACTOR",
        "_RATIO",
        "_MINUTES",
        "_DAYS",
        "_BARS",
        "_EV_",
        "_RR",
    )
    return any(m in env_key for m in markers)


def _proposed_str_valid_for_env_key(env_key: str, proposed_str: str) -> tuple[bool, str]:
    """
    Отсекает ответы LLM вроде «слегка повысить тейк» для числовых ключей — иначе ломается config.env.
    """
    if not proposed_str or not proposed_str.strip():
        return False, "пустое значение"
    s = proposed_str.strip().replace(",", ".").rstrip("%").strip()
    if _game_5m_env_key_expects_bool(env_key):
        low = s.lower()
        if low in ("true", "false", "1", "0", "yes", "no"):
            return True, ""
        return False, "ожидалось true/false"
    if _game_5m_env_key_expects_number(env_key):
        if len(proposed_str) > 48:
            return False, "слишком длинная строка (похоже на текст, а не число)"
        if re.search(r"[\u0400-\u04FF]", proposed_str):
            return False, "в значении есть кириллица — укажите число"
        try:
            float(s)
        except ValueError:
            return False, "не число: задайте proposed числом (например 5.5), без пояснений"
        return True, ""
    return True, ""


def _cron_row_ties_mfe_exit_metric_to_polling(row: dict) -> bool:
    """
    True, если текст строки ошибочно связывает cron с late_polling_signals / «запаздыванием опроса».
    Такие предложения не применяем в auto_config_override (метрика — выход vs MFE окна, не интервал cron).
    """
    parts = [
        row.get("reason_from_metrics"),
        row.get("why"),
        row.get("expected_effect"),
        row.get("parameter"),
        row.get("proposed"),
        row.get("issue"),
        row.get("proposed_fix"),
        row.get("reason"),
    ]
    blob = " ".join(str(p) for p in parts if p is not None and str(p).strip())
    low = blob.lower()
    if "late_polling" in low or "late polling" in low:
        return True
    if "polling signals" in low or "polling signal" in low or "signal polling" in low:
        return True
    if "запаздыван" in blob.lower():
        return True
    if "exit_below_window" in low.replace(" ", "_") and ("cron" in low or "polling" in low):
        return True
    return False


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
        env_key_hint = str(row.get("env_key") or "").strip()
        proposed = row.get("proposed")
        if proposed is None:
            continue
        if not parameter and not env_key_hint:
            continue
        env_key = PARAM_TO_ENV_KEY.get(parameter) or PARAM_TO_ENV_KEY.get(parameter.upper())
        if not env_key and parameter.startswith("GAME_5M_") and is_editable_config_env_key(parameter):
            env_key = parameter
        if not env_key and env_key_hint.startswith("GAME_5M_") and is_editable_config_env_key(env_key_hint):
            env_key = env_key_hint
        if not env_key:
            skipped.append(
                {
                    "parameter": parameter,
                    "reason": "no_env_mapping",
                    "note": "Нужна доработка алгоритма/маппинга (ключ не найден).",
                }
            )
            continue
        if env_key == "GAME_5M_SIGNAL_CRON_MINUTES" and _cron_row_ties_mfe_exit_metric_to_polling(row):
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "cron_blocked_late_polling_misread",
                    "note": (
                        "late_polling_signals / «запаздывание опроса» не доказывают интервал cron; "
                        "см. metric_definitions. Предложение не применяется автоматически."
                    ),
                }
            )
            continue
        if env_key in seen:
            continue
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
        ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
        if not ok_pv:
            skipped.append(
                {
                    "parameter": parameter,
                    "env_key": env_key,
                    "reason": "invalid_proposed",
                    "note": pv_note,
                }
            )
            continue
        seen.add(env_key)
        updates.append(
            {
                "env_key": env_key,
                "current": current,
                "proposed": proposed_str,
                "source_parameter": parameter,
                "reason": row.get("reason_from_metrics") or row.get("why") or row.get("expected_effect") or "",
            }
        )

    proposals = llm_ana.get("config_env_proposals")
    if isinstance(proposals, list):
        for row in proposals:
            if not isinstance(row, dict):
                continue
            env_key = str(row.get("env_key") or "").strip()
            proposed = row.get("proposed_value")
            if not env_key or proposed is None:
                continue
            if env_key in seen:
                continue
            if not is_editable_config_env_key(env_key):
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "not_editable",
                        "note": "Ключ не в списке редактируемых.",
                    }
                )
                continue
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES" and _cron_row_ties_mfe_exit_metric_to_polling(row):
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "cron_blocked_late_polling_misread",
                        "note": (
                            "late_polling_signals не доказывает интервал cron; предложение из config_env_proposals отклонено."
                        ),
                    }
                )
                continue
            current = cfg.get(env_key, "")
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                proposed_str = _coerce_polling_minutes(proposed)
                if proposed_str is None:
                    skipped.append(
                        {
                            "parameter": env_key,
                            "env_key": env_key,
                            "reason": "unparseable_polling",
                            "note": "Ожидались минуты.",
                        }
                    )
                    continue
            else:
                proposed_str = _normalize_env_value(proposed)
            ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
            if not ok_pv:
                skipped.append(
                    {
                        "parameter": env_key,
                        "env_key": env_key,
                        "reason": "invalid_proposed",
                        "note": pv_note,
                    }
                )
                continue
            seen.add(env_key)
            updates.append(
                {
                    "env_key": env_key,
                    "current": current,
                    "proposed": proposed_str,
                    "source_parameter": "config_env_proposals",
                    "reason": str(row.get("reason_from_metrics") or row.get("reason") or ""),
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
            if not env_key and parameter.startswith("GAME_5M_") and is_editable_config_env_key(parameter):
                env_key = parameter
            if not env_key or env_key in seen:
                continue
            if not is_editable_config_env_key(env_key):
                continue
            current = cfg.get(env_key, "")
            if env_key == "GAME_5M_SIGNAL_CRON_MINUTES":
                proposed_str = _coerce_polling_minutes(proposed)
                if proposed_str is None:
                    continue
            else:
                proposed_str = _normalize_env_value(proposed)
            ok_pv, pv_note = _proposed_str_valid_for_env_key(env_key, proposed_str)
            if not ok_pv:
                skipped.append(
                    {
                        "parameter": parameter,
                        "env_key": env_key,
                        "reason": "invalid_proposed",
                        "note": pv_note,
                    }
                )
                continue
            seen.add(env_key)
            updates.append(
                {
                    "env_key": env_key,
                    "current": current,
                    "proposed": proposed_str,
                    "source_parameter": parameter,
                    "reason": row.get("why") or row.get("expected_effect") or "",
                }
            )

    # Крон — частый шум в индексе [0]; торговые пороги оставляем первыми.
    updates.sort(
        key=lambda u: (
            u.get("env_key") == "GAME_5M_SIGNAL_CRON_MINUTES",
            u.get("env_key") or "",
        )
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
            _effective_stop_loss_pct,
            _effective_take_profit_pct,
            _game_5m_stop_loss_enabled,
            _max_position_minutes,
            get_strategy_params,
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

        sp = get_strategy_params()
        # Пороги при отсутствии momentum в снимке (= потолок тейка и стоп от него); в кроне подставляется живой momentum_2h.
        take_pct = _effective_take_profit_pct(None, ticker=None)
        stop_pct = _effective_stop_loss_pct(None, ticker=None)
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
                "stop_loss_enabled": _game_5m_stop_loss_enabled(),
                "stop_loss_pct_effective": stop_pct,
                "take_profit_pct_effective": take_pct,
                "strategy_params_snapshot": {
                    "take_profit_pct_cap": sp.get("take_profit_pct"),
                    "take_profit_min_pct": sp.get("take_profit_min_pct"),
                    "stop_loss_pct_config": sp.get("stop_loss_pct"),
                    "stop_to_take_ratio": sp.get("stop_to_take_ratio"),
                    "take_profit_rule": sp.get("take_profit_rule"),
                    "stop_loss_rule": sp.get("stop_loss_rule"),
                },
                "GAME_5M_TAKE_MOMENTUM_FACTOR": _cfg_float("GAME_5M_TAKE_MOMENTUM_FACTOR", "1.0"),
                "GAME_5M_EXIT_ONLY_TAKE": _cfg_bool("GAME_5M_EXIT_ONLY_TAKE", "false"),
                "GAME_5M_SESSION_END_EXIT_MINUTES": _cfg_int("GAME_5M_SESSION_END_EXIT_MINUTES", "30"),
                "GAME_5M_SESSION_END_MIN_PROFIT_PCT": _cfg_float("GAME_5M_SESSION_END_MIN_PROFIT_PCT", "0.3"),
                "GAME_5M_EARLY_DERISK_ENABLED": _cfg_bool("GAME_5M_EARLY_DERISK_ENABLED", "false"),
                "GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES": _cfg_int("GAME_5M_EARLY_DERISK_MIN_AGE_MINUTES", "180"),
                "GAME_5M_EARLY_DERISK_MAX_LOSS_PCT": _cfg_float("GAME_5M_EARLY_DERISK_MAX_LOSS_PCT", "-2.0"),
                "GAME_5M_EARLY_DERISK_MOMENTUM_BELOW": _cfg_float("GAME_5M_EARLY_DERISK_MOMENTUM_BELOW", "0.0"),
                "GAME_5M_ALLOW_PYRAMID_BUY": _cfg_bool("GAME_5M_ALLOW_PYRAMID_BUY", "false"),
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED": _cfg_bool("GAME_5M_SOFT_TAKE_NEAR_HIGH_ENABLED", "true"),
                "GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT": _cfg_float("GAME_5M_SOFT_TAKE_NEAR_HIGH_MIN_PCT", "2.0"),
                "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT": _cfg_float(
                    "GAME_5M_SOFT_TAKE_MAX_PULLBACK_FROM_HIGH_PCT", "0.35"
                ),
            },
        }
    except Exception:
        return {}


def _trade_qualifies_for_game5m_catboost(strategy: str, trade_pnl: Any) -> bool:
    """CatBoost entry-модель только для сделок, открытых в игре GAME_5M."""
    su = (strategy or "").strip().upper()
    if su == "GAME_5M":
        return True
    if su == "ALL":
        es = (getattr(trade_pnl, "entry_strategy", None) or "").strip()
        return es == "GAME_5M"
    return False


def _build_catboost_entry_backtest(strategy: str, closed: List[Any], effects: List[TradeEffect]) -> Dict[str, Any]:
    """
    Бэктест скора CatBoost на закрытых сделках: сохранённый context_json на BUY → P(благоприятный исход)
    vs фактический realized_pct (прибыль / не прибыль).

    Не применяется к портфельной игре (другой контекст входа). При strategy=ALL учитываются только
    сделки с entry_strategy GAME_5M.
    """
    from services.catboost_5m_signal import predict_entry_favorability_from_saved_context

    su = (strategy or "").strip().upper()
    if su == "PORTFOLIO":
        return {
            "mode": "skipped",
            "note": "Модель CatBoost в репозитории обучена на входах GAME_5M; для портфеля отдельный контур не подключён.",
        }
    if su not in ("GAME_5M", "ALL"):
        return {
            "mode": "skipped",
            "note": f"Стратегия {strategy!r}: бэктест CatBoost только при strategy=GAME_5M или ALL.",
        }

    by_tid: Dict[int, Any] = {}
    for t in closed:
        try:
            tid = int(getattr(t, "trade_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if tid:
            by_tid[tid] = t

    per_trade: List[Dict[str, Any]] = []
    paired: List[Tuple[float, bool]] = []  # (p_good, win) при status ok
    skipped_reasons: Dict[str, int] = {}

    for e in effects:
        tp = by_tid.get(int(e.trade_id))
        if not tp or not _trade_qualifies_for_game5m_catboost(strategy, tp):
            continue
        pred = predict_entry_favorability_from_saved_context(str(e.ticker), getattr(tp, "context_json", None))
        st = pred.get("catboost_signal_status") or ""
        row: Dict[str, Any] = {
            "trade_id": e.trade_id,
            "ticker": e.ticker,
            "catboost_signal_status": st,
            "catboost_entry_proba_good": pred.get("catboost_entry_proba_good"),
            "realized_pct": round(e.realized_pct, 4),
            "win": bool(e.realized_pct > 0),
            "estimated_upside_pct_day_at_entry": None,
            "prob_up_at_entry": None,
            "price_forecast_5m_summary_excerpt": None,
        }
        if st == "ok" and pred.get("catboost_entry_proba_good") is not None:
            try:
                pg = float(pred["catboost_entry_proba_good"])
                paired.append((pg, bool(e.realized_pct > 0)))
            except (TypeError, ValueError):
                pass
        else:
            skipped_reasons[st] = skipped_reasons.get(st, 0) + 1
        try:
            ctx = normalize_entry_context(getattr(tp, "context_json", None))
            if ctx:
                eu = ctx.get("estimated_upside_pct_day")
                if eu is not None:
                    row["estimated_upside_pct_day_at_entry"] = round(float(eu), 3)
                pu = ctx.get("prob_up")
                if pu is not None:
                    row["prob_up_at_entry"] = round(float(pu), 4)
                pfs = ctx.get("price_forecast_5m_summary")
                if isinstance(pfs, str) and pfs.strip():
                    row["price_forecast_5m_summary_excerpt"] = pfs.strip()[:120] + ("…" if len(pfs.strip()) > 120 else "")
        except Exception:
            pass
        per_trade.append(row)

    out: Dict[str, Any] = {
        "mode": "game5m_entry_context",
        "description": (
            "По каждой закрытой сделке GAME_5M: CatBoostClassifier на признаках из context_json на BUY "
            "(как train_game5m_catboost.py, без подмешивания текущей корреляции). Сравнение с фактом realized_pct."
        ),
        "trades_considered": len(per_trade),
        "trades_scored_ok": len(paired),
        "skipped_by_status": skipped_reasons,
        "per_trade": per_trade,
    }

    if len(paired) < 2:
        out["calibration"] = {
            "note": f"Мало пар «скор vs исход» (n={len(paired)}); включите GAME_5M_CATBOOST_ENABLED и наличие .cbm, либо накопите закрытия.",
        }
        return out

    wins_p = [p for p, w in paired if w]
    loss_p = [p for p, w in paired if not w]
    out["calibration"] = {
        "mean_p_given_win": round(float(np.mean(wins_p)), 4) if wins_p else None,
        "mean_p_given_loss": round(float(np.mean(loss_p)), 4) if loss_p else None,
        "win_rate_pct": round(100.0 * sum(1 for _, w in paired if w) / len(paired), 2),
        "buckets": [],
    }
    edges = [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.0001)]
    for lo, hi in edges:
        sub = [(p, w) for p, w in paired if lo <= p < hi]
        if not sub:
            out["calibration"]["buckets"].append(
                {"p_range": f"[{lo:.2f},{hi:.2f})", "n": 0, "win_rate_pct": None}
            )
            continue
        wr = 100.0 * sum(1 for _, w in sub if w) / len(sub)
        out["calibration"]["buckets"].append(
            {"p_range": f"[{lo:.2f},{hi:.2f})", "n": len(sub), "win_rate_pct": round(wr, 2)}
        )

    # Простая связь «прогноз upside на входе» vs результат (только где поле было)
    ups_pairs: List[Tuple[float, float]] = []
    for row in per_trade:
        eu = row.get("estimated_upside_pct_day_at_entry")
        if eu is None:
            continue
        try:
            ups_pairs.append((float(eu), float(row["realized_pct"])))
        except (TypeError, ValueError):
            pass
    if len(ups_pairs) >= 3:
        xs = np.array([a[0] for a in ups_pairs], dtype=float)
        ys = np.array([a[1] for a in ups_pairs], dtype=float)
        corr = float(np.corrcoef(xs, ys)[0, 1]) if np.std(xs) > 1e-9 and np.std(ys) > 1e-9 else None
        out["price_context_at_entry"] = {
            "n_with_estimated_upside_pct_day": len(ups_pairs),
            "corr_estimated_upside_vs_realized_pct": round(corr, 4) if corr is not None and math.isfinite(corr) else None,
            "note": "Корреляция справочная: estimated_upside_pct_day из входа vs итог сделки; не замена CatBoost.",
        }
    return out


def _attach_game5m_param_hypothesis_backtest_optional(
    payload: Dict[str, Any],
    *,
    strategy: str,
    effects: Optional[List[TradeEffect]],
    include_game5m_param_hypothesis_backtest: bool,
) -> None:
    """Офлайн-реплей: висяки (старое окно BUY) и недобор (missed upside) → mergeable_recommendations."""
    if not include_game5m_param_hypothesis_backtest or strategy.upper() != "GAME_5M":
        return
    try:
        from services.game5m_param_hypothesis_backtest import run_game5m_hypothesis_bundle

        payload["game5m_param_hypothesis_backtest"] = run_game5m_hypothesis_bundle(
            engine=get_engine(),
            effects=effects,
        )
    except Exception as exc:
        payload["game5m_param_hypothesis_backtest"] = {"status": "error", "reason": str(exc)}


def analyze_trade_effectiveness(
    days: int = 7,
    strategy: str = "GAME_5M",
    use_llm: bool = False,
    *,
    include_trade_details: bool = False,
    include_game5m_param_hypothesis_backtest: bool = False,
) -> Dict[str, Any]:
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    if not closed:
        empty_payload: Dict[str, Any] = {
            "meta": {
                "days": days,
                "strategy": strategy,
                "trades_analyzed": 0,
                "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
            },
            "summary": {"total": 0},
            "catboost_entry_backtest": _build_catboost_entry_backtest(strategy, [], []),
        }
        _attach_game5m_param_hypothesis_backtest_optional(
            empty_payload,
            strategy=strategy,
            effects=[],
            include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
        )
        return empty_payload

    tickers = [str(t.ticker) for t in closed if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers, days=days)
    effects = _estimate_trade_effects(closed, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    current_rules = _get_current_decision_rule_params()
    prev_state = _load_analyzer_state()
    cur_snap = _extract_game5m_config_snapshot(current_rules)
    prev_snap = None
    if isinstance(prev_state.get("last_run"), dict):
        ps = prev_state["last_run"].get("game_5m_config_snapshot")
        prev_snap = ps if isinstance(ps, dict) else None
    practical = _build_practical_parameter_suggestions(effects, summary, current_rules)
    critical_cases = _build_critical_case_analysis(effects, limit=5)
    game_5m_config_hints = _build_game5m_config_hints(effects, summary)
    entry_review = _build_entry_underperformance_review(effects, limit=8)
    catboost_entry_backtest = _build_catboost_entry_backtest(strategy, closed, effects)
    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "trades_analyzed": len(effects),
            "include_trade_details": bool(include_trade_details),
            "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness",
            "decision_source_expected": "services.recommend_5m.get_decision_5m",
            "current_decision_rule_params": current_rules,
            "previous_run_at_utc": prev_state.get("last_run", {}).get("at_utc")
            if isinstance(prev_state.get("last_run"), dict)
            else None,
            "current_game_5m_config_snapshot": cur_snap,
            "previous_game_5m_config_snapshot": prev_snap,
            "config_delta_from_previous": _diff_flat_config(prev_snap, cur_snap) if prev_snap else [],
            "long_hold_ge_7d_minutes": LONG_HOLD_MINUTES,
            "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
        },
        "summary": summary,
        "top_cases": tops,
        "practical_parameter_suggestions": practical,
        "critical_case_analysis": critical_cases,
        "game_5m_config_hints": game_5m_config_hints,
        "entry_underperformance_review": entry_review,
        "catboost_entry_backtest": catboost_entry_backtest,
    }
    if include_trade_details:
        payload["trade_effects"] = [_trade_effect_detail_dict(e) for e in effects]
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload)
    payload["auto_config_override"] = _build_auto_config_override(payload)
    _attach_game5m_param_hypothesis_backtest_optional(
        payload,
        strategy=strategy,
        effects=effects,
        include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
    )
    _save_analyzer_state(
        {
            "last_run": {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "days": days,
                "strategy": strategy,
                "game_5m_config_snapshot": cur_snap,
            }
        }
    )
    return payload


def analyze_trade_effectiveness_focused(
    days: int = 4,
    strategy: str = "GAME_5M",
    *,
    tickers: Optional[List[str]] = None,
    trade_ids: Optional[List[int]] = None,
    use_llm: bool = False,
    include_trade_details: bool = False,
    include_game5m_param_hypothesis_backtest: bool = False,
) -> Dict[str, Any]:
    """
    Узкий анализ: последние ``days`` дней, опционально только выбранные тикеры и/или trade_id выхода.
    Добавляет ``game_5m_config_hints`` и при ``use_llm`` — LLM с фокусом на ``config_env_proposals`` (GAME_5M_*).
    """
    days = max(1, min(30, int(days)))
    closed = _load_closed_trades(days=days, strategy_name=strategy)
    filtered = _filter_closed_trades_for_focus(closed, tickers=tickers, trade_ids=trade_ids)
    if not filtered:
        empty_focus: Dict[str, Any] = {
            "meta": {
                "days": days,
                "strategy": strategy,
                "focused": True,
                "trades_analyzed": 0,
                "filter": {
                    "tickers": [str(t).strip().upper() for t in (tickers or []) if str(t).strip()],
                    "trade_ids": [int(x) for x in (trade_ids or [])],
                },
                "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness_focused",
                "decision_source_expected": "services.recommend_5m.get_decision_5m",
                "include_trade_details": bool(include_trade_details),
                "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
            },
            "summary": {"total": 0},
            "catboost_entry_backtest": _build_catboost_entry_backtest(strategy, [], []),
        }
        _attach_game5m_param_hypothesis_backtest_optional(
            empty_focus,
            strategy=strategy,
            effects=[],
            include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
        )
        return empty_focus

    tickers_list = [str(t.ticker) for t in filtered if getattr(t, "ticker", None)]
    cache = _prepare_ohlc_cache(tickers=tickers_list, days=days)
    effects = _estimate_trade_effects(filtered, cache)
    summary = _aggregate(effects)
    tops = _top_cases(effects)
    current_rules = _get_current_decision_rule_params()
    practical = _build_practical_parameter_suggestions(effects, summary, current_rules)
    critical_cases = _build_critical_case_analysis(effects, limit=5)
    game_5m_config_hints = _build_game5m_config_hints(effects, summary)
    entry_review = _build_entry_underperformance_review(effects, limit=8)
    catboost_entry_backtest = _build_catboost_entry_backtest(strategy, filtered, effects)

    payload: Dict[str, Any] = {
        "meta": {
            "days": days,
            "strategy": strategy,
            "focused": True,
            "trades_analyzed": len(effects),
            "filter": {
                "tickers": [str(t).strip().upper() for t in (tickers or []) if str(t).strip()],
                "trade_ids": [int(x) for x in (trade_ids or [])],
            },
            "analyzer_source": "services.trade_effectiveness_analyzer.analyze_trade_effectiveness_focused",
            "decision_source_expected": "services.recommend_5m.get_decision_5m",
            "current_decision_rule_params": current_rules,
            "include_trade_details": bool(include_trade_details),
            "long_hold_ge_7d_minutes": LONG_HOLD_MINUTES,
            "metric_definitions": ANALYZER_METRIC_DEFINITIONS,
        },
        "summary": summary,
        "top_cases": tops,
        "practical_parameter_suggestions": practical,
        "critical_case_analysis": critical_cases,
        "game_5m_config_hints": game_5m_config_hints,
        "entry_underperformance_review": entry_review,
        "catboost_entry_backtest": catboost_entry_backtest,
    }
    if include_trade_details:
        payload["trade_effects"] = [_trade_effect_detail_dict(e) for e in effects]
    if use_llm:
        payload["llm"] = _build_llm_recommendations(payload, game_5m_config_focus=True)
    payload["auto_config_override"] = _build_auto_config_override(payload)
    _attach_game5m_param_hypothesis_backtest_optional(
        payload,
        strategy=strategy,
        effects=effects,
        include_game5m_param_hypothesis_backtest=include_game5m_param_hypothesis_backtest,
    )
    return payload


def format_trade_effectiveness_text(report: Dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    meta = report.get("meta") or {}
    if summary.get("total", 0) == 0:
        if meta.get("focused"):
            flt = meta.get("filter") or {}
            parts = []
            if flt.get("tickers"):
                parts.append("тикеры: " + ", ".join(str(x) for x in flt["tickers"]))
            if flt.get("trade_ids"):
                parts.append("trade_id: " + ", ".join(str(x) for x in flt["trade_ids"]))
            extra = (" (" + "; ".join(parts) + ")") if parts else ""
            return f"За выбранный период по узкому фильтру закрытых сделок не найдено{extra}."
        return "За выбранный период закрытых сделок не найдено."
    top = report.get("top_cases") or {}
    title = (
        "📊 Узкий анализ (выбранные сделки / окно)"
        if meta.get("focused")
        else "📊 Анализатор эффективности сделок"
    )
    filter_line = ""
    if meta.get("focused"):
        flt = meta.get("filter") or {}
        if flt.get("tickers") or flt.get("trade_ids"):
            filter_line = (
                f"Фильтр: тикеры={flt.get('tickers') or '—'} | trade_id={flt.get('trade_ids') or '—'}"
            )
    lines = [
        title,
        f"Период: {meta.get('days', '—')} дн. | Стратегия: {meta.get('strategy', '—')}",
    ]
    if filter_line:
        lines.append(filter_line)
    lines.extend(
        [
            f"Сделок: {summary['total']} | Win rate: {summary['win_rate_pct']:.2f}% | Net PnL: ${summary['sum_net_pnl_usd']:+.2f}",
            f"Средний результат: {summary['avg_realized_pct']:+.3f}% | Медиана: {summary['median_realized_pct']:+.3f}%",
            f"Упущенный upside: Σ {summary['sum_missed_upside_pct']:+.3f}% | Избежимый loss: Σ {summary['sum_avoidable_loss_pct']:+.3f}%",
            f"По выигрышным: Σ missed {summary.get('sum_missed_upside_pct_on_wins', 0):+.3f}% | "
            f"сделок с missed≥1%: {summary.get('wins_with_missed_upside_ge_1pct_count', 0)}",
            f"Сигналы риска: выход ниже MFE окна={summary['late_polling_signals']} (см. exit_below_window_mfe_count), "
            f"high-vol losses={summary['high_vol_losses_count']}, weak P(up) losses={summary['weak_prob_up_losses_count']}",
            f"Параметрические причины: losses@ALLOW={summary.get('losses_with_allow_entry_count', 0)}, losses@prob_up>=0.60={summary.get('losses_with_high_prob_up_count', 0)}, losses@RSI>=60={summary.get('losses_with_high_rsi_count', 0)}",
            f"Удержание ≥7 суток: {summary.get('long_hold_ge_7d_count', 0)} сделок, из них слабый результат (≤0.15%): {summary.get('long_hold_ge_7d_poor_outcome_count', 0)}; "
            f"без decision в context_json: {summary.get('trades_missing_entry_decision_count', 0)}",
            "",
            "Top losses:",
        ]
    )
    for r in (top.get("top_losses") or [])[:4]:
        ed = r.get("entry_decision") or "—"
        hm = r.get("hold_minutes")
        hm_s = f", hold {hm:.0f}m" if isinstance(hm, (int, float)) else ""
        lines.append(
            f"• {r['ticker']} #{r['trade_id']}: {r['realized_pct']:+.2f}% (exit={r['exit_signal']}, entry={ed}{hm_s})"
        )
    win_missed = top.get("top_profitable_missed_upside") or []
    if win_missed and any((row.get("missed_upside_pct") or 0) >= 0.5 for row in win_missed[:4]):
        lines.append("")
        lines.append("Выигрышные, но с крупным недобором (ранний выход vs high окна):")
        for r in win_missed[:4]:
            if (r.get("missed_upside_pct") or 0) < 0.25:
                continue
            lines.append(
                f"• {r['ticker']} #{r['trade_id']}: +{r['realized_pct']:.2f}%, missed {r.get('missed_upside_pct', 0):+.2f}% "
                f"(exit={r['exit_signal']})"
            )
    practical = report.get("practical_parameter_suggestions") or []
    if practical:
        lines.append("")
        lines.append("Практические изменения:")
        for p in practical[:4]:
            lines.append(
                f"• {p.get('parameter')}: {p.get('current')} -> {p.get('proposed')} | {p.get('why')}"
            )
    cb = report.get("catboost_entry_backtest") or {}
    if cb.get("mode") == "game5m_entry_context":
        cal = cb.get("calibration") or {}
        lines.append("")
        lines.append("CatBoost (бэктест P(благоприятный исход) на context_json входа):")
        lines.append(
            f"• Сделок со скором: {cb.get('trades_scored_ok', 0)} / учтено в отчёте: {cb.get('trades_considered', 0)}"
        )
        if cal.get("mean_p_given_win") is not None:
            mpl = cal.get("mean_p_given_loss")
            if mpl is not None:
                lines.append(f"• Средний P | win: {cal['mean_p_given_win']:.3f} | Средний P | loss: {mpl:.3f}")
            else:
                lines.append(f"• Средний P | win: {cal['mean_p_given_win']:.3f}")
        for b in cal.get("buckets") or []:
            if b.get("n"):
                lines.append(
                    f"  {b.get('p_range')}: n={b['n']}, win_rate={b.get('win_rate_pct')}%"
                )
        if cal.get("note"):
            lines.append(f"• {cal['note']}")
    elif cb.get("note"):
        lines.append("")
        lines.append(f"CatBoost: {cb.get('note')}")
    entry_rev = report.get("entry_underperformance_review") or []
    if entry_rev:
        lines.append("")
        lines.append("Разбор входа (слабый результат / долгое удержание):")
        for row in entry_rev[:5]:
            tu = row.get("ticker")
            tid = row.get("trade_id")
            rc = row.get("realized_pct")
            ed = row.get("entry_decision") or "—"
            ex_full = str(row.get("entry_reasoning_excerpt") or "").replace("\n", " ").strip()
            ex = ex_full[:140]
            keys = row.get("suggested_config_env_review") or []
            ks = ", ".join(str(k) for k in keys[:5]) if keys else "—"
            lh = "да" if row.get("long_hold_ge_7d") else "нет"
            lines.append(f"• {tu} #{tid}: {rc:+.2f}% | entry={ed} | ≥7d={lh} | env: {ks}")
            if ex:
                suffix = "…" if len(ex_full) > 140 else ""
                lines.append(f"  reasoning: {ex}{suffix}")

    critical = report.get("critical_case_analysis") or []
    if critical:
        lines.append("")
        lines.append("Критичные кейсы:")
        for c in critical[:4]:
            lines.append(
                f"• {c.get('ticker')} #{c.get('trade_id')}: {c.get('diagnosis')} | action: {c.get('action')}"
            )
    hyp = report.get("game5m_param_hypothesis_backtest")
    if isinstance(hyp, dict) and hyp.get("hanger_hypotheses") is not None:
        lines.append("")
        lines.append("GAME_5M param backtest (висяки / недобор, офлайн):")
        if hyp.get("status") == "error":
            lines.append(f"• ошибка: {hyp.get('reason')}")
        else:
            hc = hyp.get("hanger_hypotheses") or []
            uc = hyp.get("underprofit_hypotheses") or []
            mr = hyp.get("mergeable_recommendations") or []
            lines.append(f"• висяки (строк): {len(hc)} | недобор: {len(uc)} | mergeable_hints: {len(mr)}")
    hints = report.get("game_5m_config_hints") or []
    if hints:
        lines.append("")
        lines.append("Эвристики GAME_5M (что пересмотреть в config.env):")
        for h in hints[:8]:
            ek = h.get("env_key", "")
            direction = h.get("direction", "")
            evidence = h.get("evidence", "")
            rationale = h.get("rationale", "")
            lines.append(f"• {ek} ({direction}): {evidence}")
            if rationale:
                lines.append(f"  → {rationale}")
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
