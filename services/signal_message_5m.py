# -*- coding: utf-8 -*-
"""
Формирование текста сигнала 5m для Telegram, cron и веба.
Единый модуль — все форматы сообщений 5m; данные из get_decision_5m() / get_5m_card_payload().

Использование:
- cron (send_sndk_signal_cron): build_5m_entry_signal_text(d5, ticker, mentions) — полный алерт «ВХОД 5m».
- бот /signal5m, /signal по тикеру 5m: build_5m_technical_short_text(tech, ticker) — короткая карточка.
- при необходимости тот же полный алерт в боте/вебе — вызывать build_5m_entry_signal_text().
"""
from __future__ import annotations

from typing import Any, Dict

from config_loader import get_config_value, get_dynamic_config_value


def build_5m_technical_short_text(tech: Dict[str, Any], ticker: str) -> str:
    """
    Короткий технический сигнал 5m (одна карточка в чат).
    tech — результат get_5m_technical_signal() или get_5m_card_payload(); один источник для бота и cron.
    """
    decision = tech.get("decision") or "—"
    price = tech.get("price")
    rsi = tech.get("rsi_5m")
    mom = tech.get("momentum_2h_pct")
    vol = tech.get("volatility_5m_pct")
    period = tech.get("period_str")
    entry = tech.get("entry_advice") or "—"
    reasoning = (tech.get("reasoning") or "")[:300]
    kb = tech.get("kb_news_impact") or "—"
    lines = [
        f"5m · {ticker} — {decision}",
        f"Цена: ${price:.2f}" if price is not None else "Цена: —",
        f"RSI(5m): {rsi:.1f}" if rsi is not None else "RSI: —",
        f"Импульс 2ч: {mom:+.2f}%" if mom is not None else "",
        f"Волатильность: {vol:.2f}%" if vol is not None else "",
        f"Вход: {entry}",
        f"Новости (KB): {kb}",
    ]
    if period:
        lines.append(f"Период: {period}")
    if reasoning:
        lines.append(f"Обоснование: {reasoning}")
    summ = tech.get("price_forecast_5m_summary")
    if summ:
        lines.append(f"Прогноз цены: {summ}")
    return "\n".join(s for s in lines if s)


def build_5m_entry_signal_text(
    d5: Dict[str, Any],
    ticker: str,
    mentions: str = "",
) -> str:
    """
    Собирает текст сообщения «Сигнал на вход 5m» для Telegram.
    d5 — результат get_decision_5m(ticker); может быть обновлён LLM (decision, reasoning, llm_key_factors).
    """
    decision = d5.get("decision", "HOLD")
    price = d5.get("price")
    rsi = d5.get("rsi_5m")
    mom = d5.get("momentum_2h_pct")
    vol = d5.get("volatility_5m_pct")
    period = (d5.get("period_str") or "").strip()
    reasoning = (d5.get("reasoning") or "").strip()[:200]

    try:
        from report_generator import get_engine
        _eng = get_engine()
        _sl_raw = (get_dynamic_config_value("PORTFOLIO_STOP_LOSS_ENABLED", "true", engine=_eng) or "true").strip().lower()
    except Exception:
        _sl_raw = (get_config_value("PORTFOLIO_STOP_LOSS_ENABLED", "true") or "true").strip().lower()
    portfolio_stop_disabled = _sl_raw in ("0", "false", "no")

    try:
        from services.game_5m import _effective_take_profit_pct, _effective_stop_loss_pct, _game_5m_stop_loss_enabled
        game5m_stop_enabled = _game_5m_stop_loss_enabled()
        take_pct_msg = _effective_take_profit_pct(mom, ticker=ticker)
    except Exception:
        game5m_stop_enabled = True
        take_pct_msg = 5.0
        _effective_stop_loss_pct = lambda _m, ticker=None: 2.5

    if game5m_stop_enabled:
        try:
            stop_pct = _effective_stop_loss_pct(mom, ticker=ticker)
        except Exception:
            stop_pct = 2.5
        params_line = "Параметры (интрадей): стоп −%.1f%%, тейк +%.1f%% (стоп < тейк, оба от импульса 2ч)." % (stop_pct, take_pct_msg)
    else:
        params_line = "Параметры (интрадей): тейк +%.1f%% (стоп 5m выкл. — закрытие только по тейку/TIME_EXIT/SELL)." % take_pct_msg

    price_str = f"${price:.2f}" if price is not None else "—"
    headline = f"🎯 ВХОД 5m: {ticker} — {decision} · {price_str}"
    lines = [
        headline,
        "",
        f"Решение: {decision}",
        f"Цена: ${price:.2f}" if price is not None else "",
        f"RSI(5m): {rsi:.1f}" if rsi is not None else "",
        f"Импульс 2ч: {mom:+.2f}%" if mom is not None else "",
        f"Волатильность 5m: {vol:.2f}%" if vol is not None else "",
        f"_Период данных: {period}_" if period else "",
    ]
    summ = d5.get("price_forecast_5m_summary")
    if summ:
        lines.append(f"📈 Прогноз цены (p10–p50–p90): {summ}")
    lines.extend(
        [
        "",
        params_line,
    ]
    )
    if portfolio_stop_disabled:
        lines.append("⚠️ Стоп портфеля отключён (PORTFOLIO_STOP_LOSS_ENABLED=false): по портфельным позициям закрытие только по тейку.")
    if not game5m_stop_enabled:
        lines.append("⚠️ Стоп 5m отключён (GAME_5M_STOP_LOSS_ENABLED=false) — не рекомендуй стоп-лосс по 5m.")
    lines.append("")
    lines.append(f"Подробнее: /recommend5m {ticker}")
    if reasoning:
        lines.insert(-2, f"💭 {reasoning}")

    p_cb = d5.get("catboost_entry_proba_good")
    st_cb = d5.get("catboost_signal_status")
    if p_cb is not None and st_cb == "ok":
        lines.append("")
        lines.append(f"🤖 **CatBoost** P(благоприятный исход): {float(p_cb):.2f}")
    core_d = d5.get("technical_decision_core")
    eff_d = d5.get("technical_decision_effective")
    if core_d and eff_d and str(core_d) != str(eff_d):
        fn = d5.get("catboost_fusion_note") or ""
        lines.append(f"⚙️ Тех. итог: {eff_d} (правила: {core_d}){(' — ' + fn) if fn else ''}")

    news_impact = d5.get("kb_news_impact") or "нейтрально"
    lines.append("")
    lines.append(f"📰 **Учёт новостей:** {news_impact}")

    kb_news = d5.get("kb_news") or []
    if kb_news:
        recent = list(kb_news)[:3]
        parts = []
        for n in recent:
            sent = n.get("sentiment_score")
            sent_str = f" (тон {sent:.2f})" if sent is not None else ""
            content = (n.get("content") or "").strip()[:80]
            if content:
                parts.append(f"• {content}{sent_str}")
        if parts:
            lines.append("")
            lines.append("📰 **Новости из базы (за период 5m):**")
            lines.extend(parts)

    llm_insight = d5.get("llm_insight")
    llm_content = (d5.get("llm_news_content") or "").strip()[:400]
    if llm_insight:
        lines.append("")
        lines.append(f"📰 **LLM (свежие новости):** {llm_insight}")
    elif llm_content:
        lines.append("")
        lines.append(f"📰 **LLM:** {llm_content}…")

    if ticker.upper() == "SNDK" and price is not None:
        try:
            from services.alex_rule import get_alex_rule_status
            alex = get_alex_rule_status(ticker, price)
            if alex and alex.get("message"):
                lines.append("")
                lines.append(f"📋 {alex['message']}")
        except Exception:
            pass

    text = "\n".join([s for s in lines if s])
    if mentions:
        text = mentions + "\n\n" + text
    return text.strip()
