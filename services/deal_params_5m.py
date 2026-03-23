# -*- coding: utf-8 -*-
"""
Параметры сделки 5m: полный дамп для новых записей, упрощённый для старых.
Подмешивание в будущий контекст — через normalize_entry_context(), работает с обоими форматами.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Union

from services.cluster_recommend import CORRELATION_CB_FEATURE_KEYS

# Версия формата полного дампа (новые сделки)
DEAL_PARAMS_VERSION = 1
REASONING_MAX_LEN = 500

# Поля полного дампа (из get_decision_5m), без длинных текстов
FULL_ENTRY_KEYS = (
    "decision", "reasoning", "price",
    "momentum_2h_pct", "premarket_intraday_momentum_pct", "rsi_5m", "volatility_5m_pct", "session_high", "period_str",
    "stop_loss_enabled", "stop_loss_pct", "take_profit_pct",
    "entry_advice", "entry_advice_reason",
    "high_5d", "low_5d", "pullback_from_high_pct",
    "last_bar_high", "last_bar_low", "recent_bars_high_max", "recent_bars_low_min",
    "bars_count", "kb_news_impact",
    "estimated_upside_pct_day", "suggested_take_profit_price",
    "estimated_downside_pct_day", "prob_up", "prob_down",
    "premarket_gap_pct", "minutes_until_open", "prev_close",
    "llm_insight", "llm_sentiment",
    "atr_5m_pct", "volume_5m_last", "volume_vs_avg_pct",
    "decision_rule_version", "decision_rule_params",
    # Прогноз цены 30/60/120 мин (лог-норм. по 5m) — для LLM и истории входа
    "price_forecast_5m", "price_forecast_5m_summary",
) + CORRELATION_CB_FEATURE_KEYS
# session_phase берём из market_session
SESSION_PHASE_KEY = "session_phase"
# Доп. поля при LLM-входе — сохраняются в том же JSON, единая схема для обоих стратегий
LLM_EXTRA_KEYS = ("llm_key_factors",)


def build_full_entry_context(
    d5: Dict[str, Any],
    *,
    correlation_entry_features: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Строит полный дамп параметров на момент входа (как в prompt_entry game_5m).
    Единая схема для технического и LLM-входа: один и тот же набор полей;
    при entry_strategy=llm добавляются только entry_strategy и опционально llm_key_factors.

    correlation_entry_features: агрегаты корреляции (как у LLM), те же ключи что в
    cluster_recommend.CORRELATION_CB_FEATURE_KEYS — для CatBoost и воспроизводимости сделки.
    """
    if not d5:
        return {}
    out: Dict[str, Any] = {"deal_params_version": DEAL_PARAMS_VERSION}
    for k in FULL_ENTRY_KEYS:
        v = d5.get(k)
        if v is None:
            continue
        if k == "reasoning" and isinstance(v, str) and len(v) > REASONING_MAX_LEN:
            v = v[:REASONING_MAX_LEN] + "…"
        out[k] = v
    # Импульс при входе — дублируем под именем entry_impulse_pct для единообразного чтения
    mom = d5.get("momentum_2h_pct")
    if mom is not None:
        out["entry_impulse_pct"] = float(mom)
    # Фаза сессии (в get_decision_5m — market_session.session_phase)
    ms = d5.get("market_session")
    if isinstance(ms, dict):
        sp = ms.get("session_phase") or ms.get("phase")
        if sp is not None:
            out[SESSION_PHASE_KEY] = sp
    elif isinstance(ms, str):
        out[SESSION_PHASE_KEY] = ms
    # Стратегия входа — всегда в JSON (technical | llm), чтобы формат не отличался
    out["entry_strategy"] = (d5.get("entry_strategy") or "technical").strip().lower()
    # При LLM-входе — доп. поля в том же JSON (единая схема)
    for k in LLM_EXTRA_KEYS:
        v = d5.get(k)
        if v is not None:
            out[k] = v
    # Корреляция (кластерный прогон крона / тот же расчёт, что для LLM)
    if correlation_entry_features:
        for k in CORRELATION_CB_FEATURE_KEYS:
            v = correlation_entry_features.get(k)
            if v is not None and isinstance(v, (int, float)):
                out[k] = float(v)
    return {k: v for k, v in out.items() if v is not None}


def normalize_entry_context(
    ctx: Optional[Union[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    Приводит context_json к единому виду для подмешивания в будущий контекст.
    Поддерживает:
    - полный дамп (deal_params_version >= 1) — возвращаем как есть, плюс гарантируем entry_impulse_pct;
    - упрощённый/старый (без version) — возвращаем как есть, entry_impulse_pct = momentum_2h_pct.
    Возвращаемый dict можно безопасно использовать: .get("entry_impulse_pct"), .get("rsi_5m") и т.д.
    """
    if ctx is None:
        return {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx) if ctx.strip() else {}
        except Exception:
            return {}
    if not isinstance(ctx, dict) or not ctx:
        return {}
    normalized = dict(ctx)
    # Единая точка доступа к импульсу при входе
    if normalized.get("entry_impulse_pct") is None and normalized.get("momentum_2h_pct") is not None:
        try:
            normalized["entry_impulse_pct"] = float(normalized["momentum_2h_pct"])
        except (TypeError, ValueError):
            pass
    return normalized


def get_entry_impulse_pct(ctx: Optional[Union[str, Dict[str, Any]]]) -> Optional[float]:
    """Извлекает импульс при входе из любого формата context_json."""
    n = normalize_entry_context(ctx)
    v = n.get("entry_impulse_pct") or n.get("momentum_2h_pct")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_entry_price_forecast_summary(ctx: Optional[Union[str, Dict[str, Any]]]) -> Optional[str]:
    """Краткая строка прогноза цены (30/60/120 мин) на момент входа, если сохранена в context_json."""
    n = normalize_entry_context(ctx)
    s = n.get("price_forecast_5m_summary")
    if s and isinstance(s, str) and s.strip():
        return s.strip()[:2000]
    return None
