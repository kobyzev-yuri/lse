# -*- coding: utf-8 -*-
"""
Общий пайплайн команд «игр» (портфель daily, GAME_5m): тикеры, KB-сигнал, решения, HTML-данные.
Разница игр — только в источнике тех. параметров и в post-обработке (LLM-корреляция 5m и т.д.);
обработчики бота остаются тонкими (без дублирования циклов в main).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_days_arg(args: Optional[List[str]], index: int = 1, default: int = 5, lo: int = 1, hi: int = 7) -> int:
    """Дни для 5m-команд из context.args[index]."""
    if not args or len(args) <= index:
        return default
    try:
        return max(lo, min(hi, int(args[index].strip())))
    except (ValueError, TypeError):
        return default


def kb_news_plain_for_ticker(analyst: Any, ticker: str) -> str:
    """Тот же KB-пайплайн, что /news и portfolio recommend (compute_kb_news_bias_metrics + plaintext)."""
    from report_generator import get_engine
    from services.kb_news_report import (
        kb_news_lookback_hours,
        compute_kb_news_bias_metrics,
        build_kb_news_signal_plaintext,
    )

    eng = get_engine()
    news_df = analyst.get_recent_news(ticker)
    if news_df is None or getattr(news_df, "empty", True):
        return "📰 Новости (KB): нет записей в окне (см. KB_NEWS_LOOKBACK_HOURS)."
    h = kb_news_lookback_hours()
    metrics = compute_kb_news_bias_metrics(news_df, ticker, analyst, h, engine=eng)
    return build_kb_news_signal_plaintext(ticker, metrics)


def portfolio_cluster_tickers_and_corr() -> Tuple[List[str], List[str], Optional[Dict[str, Any]], str]:
    """
    full_list — все тикеры портфельной игры (для заголовка HTML);
    tickers_to_run — без индикаторов (для расчёта рекомендаций).
    """
    from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only
    from services.cluster_recommend import get_correlation_matrix

    full_list = list(get_tickers_for_portfolio_game() or [])
    indicator_only = set(get_tickers_indicator_only())
    tickers_to_run = [x for x in full_list if x not in indicator_only]
    cluster_ctx = None
    if len(full_list) >= 2:
        corr = get_correlation_matrix(full_list, days=30)
        if corr:
            cluster_ctx = {"tickers": full_list, "correlation": corr, "other_signals": {}}
    note = "Корреляция по кластеру за 30 дн. (контекст общий). Вход/выход — портфельная игра."
    return full_list, tickers_to_run, cluster_ctx, note


def portfolio_single_cluster_context(ticker: str) -> Optional[Dict[str, Any]]:
    """Кластер для одного тикера портфеля (как get_portfolio_cluster_context: только торгуемые, если нет индикаторов)."""
    from services.ticker_groups import get_tickers_for_portfolio_game, get_tickers_indicator_only
    from services.cluster_recommend import get_correlation_matrix

    full_list = list(get_tickers_for_portfolio_game() or [])
    indicator_only = set(get_tickers_indicator_only())
    list_for_corr = full_list if indicator_only else [t for t in full_list if t not in indicator_only]
    if ticker not in list_for_corr:
        list_for_corr = [ticker] + list_for_corr
    if len(list_for_corr) >= 2:
        corr = get_correlation_matrix(list_for_corr, days=30)
        if corr:
            return {"tickers": list_for_corr, "correlation": corr, "other_signals": {}}
    return None


def run_portfolio_cluster_recommend_payloads(analyst: Any) -> Tuple[List[str], str, List[Dict[str, Any]]]:
    """
    Полный цикл рекомендаций по кластеру портфеля для HTML _build_prompt_entry_all_html.
    Возвращает (full_list_for_html_title, correlation_note, per_ticker_payloads).
    """
    full_list, tickers_to_run, cluster_ctx, correlation_note = portfolio_cluster_tickers_and_corr()
    per_ticker_payloads: List[Dict[str, Any]] = []
    other_signals: Dict[str, str] = {}
    for tkr in tickers_to_run:
        ctx = {**cluster_ctx, "other_signals": dict(other_signals)} if cluster_ctx else None
        result = analyst.get_decision_with_llm(tkr, cluster_context=ctx)
        dec = result.get("decision", "HOLD")
        other_signals[tkr] = dec
        try:
            kb_sig = kb_news_plain_for_ticker(analyst, tkr)
        except Exception as e:
            logger.debug("recommend portfolio cluster KB %s: %s", tkr, e)
            kb_sig = f"📰 KB: ошибка ({e!s})"
        per_ticker_payloads.append({
            "ticker": tkr,
            "user_prompt": result.get("prompt_user"),
            "llm_response": result.get("llm_response_raw"),
            "decision": dec,
            "note": None,
            "kb_news_signal_plain": kb_sig,
        })
    return full_list, correlation_note, per_ticker_payloads


def run_portfolio_single_recommend(analyst: Any, ticker: str) -> Tuple[Dict[str, Any], str]:
    """Один тикер портфеля: decision_result + kb plaintext."""
    ctx = portfolio_single_cluster_context(ticker)
    decision_result = analyst.get_decision_with_llm(ticker, cluster_context=ctx)
    try:
        kb_sig = kb_news_plain_for_ticker(analyst, ticker)
    except Exception as e:
        logger.debug("recommend portfolio KB %s: %s", ticker, e)
        kb_sig = f"📰 KB: ошибка ({e!s})"
    return decision_result, kb_sig


def game5m_cluster_list(single_ticker: Optional[str]) -> List[str]:
    from services.ticker_groups import get_tickers_game_5m

    if single_ticker:
        return [single_ticker]
    return list(get_tickers_game_5m() or [])


def run_game5m_recommend_card_rows(
    days: int,
    single_ticker: Optional[str] = None,
    *,
    apply_llm_correlation: bool,
) -> Tuple[List[str], Optional[str], List[Dict[str, Any]]]:
    """
    Строки карточек 5m для /recommend5m (и при необходимости — LLM по корреляции).
    Возвращает (cluster_5m, correlation_note_or_none, per_ticker_results).
    """
    from config_loader import get_use_llm_for_analyst
    from services.cluster_recommend import (
        load_game5m_llm_correlation,
        GAME5M_LLM_CORRELATION_NOTE,
        get_avg_volatility_20_pct_from_quotes,
    )
    from services.recommend_5m import get_decision_5m, get_5m_card_payload
    from services.cluster_recommend import build_cluster_note_for_5m_llm as _build_cluster_note_for_5m_llm

    cluster_5m = game5m_cluster_list(single_ticker)
    if not cluster_5m:
        return [], None, []
    corr_matrix, corr_tickers_used, _ = load_game5m_llm_correlation(days=30)
    correlation_note = GAME5M_LLM_CORRELATION_NOTE if corr_matrix else None
    per_ticker_results: List[Dict[str, Any]] = []
    for tkr in cluster_5m:
        d5 = get_decision_5m(tkr, days=days, use_llm_news=True)
        item = get_5m_card_payload(d5, tkr)
        if d5:
            kb_news = d5.get("kb_news") or []
            kb_summary = ""
            if kb_news:
                titles = [
                    n.get("title") or (n.get("content", "")[:80] if n.get("content") else "")
                    for n in kb_news[:5]
                ]
                kb_summary = "; ".join((t[:100] + ("…" if len(t) > 100 else "")) for t in titles if t)
            item["kb_news_summary"] = kb_summary
            item["llm_news_content"] = d5.get("llm_news_content")
            item["llm_sentiment"] = d5.get("llm_sentiment")
        per_ticker_results.append(item)

    extra_tech: Dict[str, Dict[str, Any]] = {}
    if corr_tickers_used:
        cluster_set = set(cluster_5m)
        for t in corr_tickers_used:
            if t in cluster_set:
                continue
            d5 = get_decision_5m(t, days=days, use_llm_news=False)
            if d5 and (d5.get("price") is not None or d5.get("rsi_5m") is not None):
                extra_tech[t] = {"price": d5.get("price"), "rsi": d5.get("rsi_5m")}

    if apply_llm_correlation and get_use_llm_for_analyst() and corr_matrix and (corr_tickers_used or cluster_5m):
        tech_by_ticker_5m = {
            r.get("ticker"): {"price": r.get("price"), "rsi": r.get("rsi_5m")}
            for r in per_ticker_results
            if r.get("ticker")
        }
        tech_by_ticker_5m.update(extra_tech)
        for r in per_ticker_results:
            if r.get("decision") == "NO_DATA" or not cluster_5m:
                continue
            cluster_note = _build_cluster_note_for_5m_llm(
                r["ticker"], cluster_5m, corr_matrix, tech_by_ticker_5m,
            )
            if not cluster_note:
                continue
            try:
                from services.llm_service import get_llm_service

                llm = get_llm_service()
                if not getattr(llm, "client", None):
                    continue
                technical_data = {
                    "close": r.get("price"),
                    "rsi": r.get("rsi_5m"),
                    "volatility_5": r.get("volatility_5m_pct"),
                    "avg_volatility_20": get_avg_volatility_20_pct_from_quotes(r["ticker"]),
                    "technical_signal": r.get("decision"),
                    "cluster_note": cluster_note,
                    "momentum_2h_pct": r.get("momentum_2h_pct"),
                    "take_profit_pct": r.get("take_profit_pct"),
                    "stop_loss_pct": r.get("stop_loss_pct"),
                    "estimated_upside_pct_day": r.get("estimated_upside_pct_day"),
                    "price_forecast_5m": r.get("price_forecast_5m"),
                    "price_forecast_5m_summary": r.get("price_forecast_5m_summary"),
                }
                news_list = []
                if r.get("kb_news_summary") or r.get("kb_news_impact"):
                    news_list = [
                        {
                            "source": "KB",
                            "content": (r.get("kb_news_summary") or r.get("kb_news_impact") or "")[:500],
                            "sentiment_score": 0.5,
                        }
                    ]
                sentiment = 0.5
                if r.get("kb_news_impact") == "негативно":
                    sentiment = 0.35
                elif r.get("kb_news_impact") == "позитивно":
                    sentiment = 0.65
                result = llm.analyze_trading_situation(
                    r["ticker"],
                    technical_data,
                    news_list,
                    sentiment,
                    strategy_name="GAME_5M",
                    strategy_signal=r.get("decision"),
                )
                if result and result.get("llm_analysis"):
                    ana = result["llm_analysis"]
                    r["llm_correlation_reasoning"] = ana.get("reasoning") or ""
                    r["llm_key_factors"] = ana.get("key_factors") or []
            except Exception as e:
                logger.debug("LLM с корреляцией для 5m %s: %s", r.get("ticker"), e)

    return cluster_5m, correlation_note, per_ticker_results


def run_game5m_technical_rows(tickers: List[str], days: int) -> List[Dict[str, Any]]:
    """Технический сигнал 5m без LLM (как /signal5m и ветка /signal для тикера в игре)."""
    from services.recommend_5m import get_5m_technical_signal

    out: List[Dict[str, Any]] = []
    for tkr in tickers:
        tech = get_5m_technical_signal(tkr, days=days, use_llm_news=False)
        if tech:
            tech["ticker"] = tkr
            out.append(tech)
        else:
            out.append({"ticker": tkr, "decision": "NO_DATA"})
    return out


def ticker_in_game_5m(ticker: str) -> bool:
    from services.ticker_groups import get_tickers_game_5m

    return ticker in set(get_tickers_game_5m() or [])


def game5m_cluster_or_error(single_ticker: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """
    Если single_ticker задан и не в игре — (error_message, []).
    Иначе (None, cluster_list).
    """
    from services.ticker_groups import get_tickers_game_5m

    cluster_5m = list(get_tickers_game_5m() or [])
    if not cluster_5m:
        return "❌ Тикеры игры 5m не заданы (GAME_5M_TICKERS / TICKERS_FAST).", []
    if single_ticker and single_ticker not in cluster_5m:
        return f"❌ {single_ticker} не в игре 5m. Кластер: {', '.join(cluster_5m)}", []
    return None, cluster_5m
