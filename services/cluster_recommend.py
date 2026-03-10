"""
Кластерные рекомендации: загрузка данных по всем тикерам кластера, корреляция, единый контекст.

- Игра 5m: тикеры из GAME_5M_TICKERS; решения по каждому с учётом корреляции и общих данных.
- Портфель (медленные/средние): тикеры из TRADING_CYCLE_TICKERS; рекомендации по каждому с кластерным контекстом.

Используется в /recommend (без тикера), /recommend5m (без тикера) и в кронах для входов/закрытий.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_correlation_matrix(
    tickers: List[str],
    days: int = 30,
    min_tickers_per_row: Optional[int] = None,
) -> Optional[Dict[str, Dict[str, float]]]:
    """
    Матрица корреляций по тикерам (дневные доходности из quotes).
    Возвращает dict[ticker1][ticker2] = correlation или None при ошибке.
    min_tickers_per_row=2 — мягче (оставлять строки с данными хотя бы по 2 тикерам), чтобы матрица строилась чаще.
    """
    if len(tickers) < 2:
        return None
    try:
        from services.cluster_manager import ClusterManager
        cm = ClusterManager()
        corr_df, _ = cm.get_correlation_and_beta_matrix(
            tickers, days=days, min_tickers_per_row=min_tickers_per_row
        )
        if corr_df is None or corr_df.empty:
            return None
        return corr_df.to_dict()
    except Exception as e:
        logger.debug("Корреляция по кластеру: %s", e)
        return None


def get_cluster_decisions_5m(
    tickers: List[str],
    days: int = 5,
    use_llm_news: bool = True,
) -> Dict[str, Any]:
    """
    Решения по 5m для всех тикеров кластера + корреляция.
    Возвращает: {"decisions": {ticker: get_decision_5m(ticker)}, "correlation": {...}, "tickers": [...]}.
    """
    from services.recommend_5m import get_decision_5m

    decisions = {}
    for t in tickers:
        try:
            d = get_decision_5m(t, days=days, use_llm_news=use_llm_news)
            if d is not None:
                decisions[t] = d
        except Exception as e:
            logger.warning("5m решение для %s: %s", t, e)

    correlation = get_correlation_matrix(tickers, days=min(30, days * 7))
    return {
        "decisions": decisions,
        "correlation": correlation,
        "tickers": tickers,
    }


def get_cluster_recommendations_portfolio(
    analyst,
    tickers: List[str],
) -> Dict[str, Any]:
    """
    Рекомендации по портфельному кластеру (медленные/средние игры): решение по каждому тикеру + корреляция.
    analyst — экземпляр AnalystAgent (get_decision_with_llm).
    Возвращает: {"recommendations": {ticker: rec_dict}, "correlation": {...}, "tickers": [...]}.
    """
    recommendations = {}
    for t in tickers:
        try:
            result = analyst.get_decision_with_llm(t)
            if result.get("decision") == "NO_DATA":
                continue
            recommendations[t] = result
        except Exception as e:
            logger.warning("Рекомендация портфель для %s: %s", t, e)

    correlation = get_correlation_matrix(tickers, days=30)
    return {
        "recommendations": recommendations,
        "correlation": correlation,
        "tickers": tickers,
    }


def format_correlation_summary(correlation: Optional[Dict[str, Dict[str, float]]], tickers: List[str]) -> str:
    """Краткая сводка корреляций для вывода в чат (пары с |corr| > 0.5)."""
    if not correlation or len(tickers) < 2:
        return ""
    pairs = []
    seen = set()
    for i, t1 in enumerate(tickers):
        for t2 in tickers[i + 1:]:
            if t1 not in correlation or t2 not in correlation.get(t1, {}):
                continue
            c = correlation[t1].get(t2)
            if c is None:
                continue
            try:
                v = float(c)
                if abs(v) >= 0.5:
                    pairs.append(f"{t1}–{t2} {v:+.2f}")
            except (TypeError, ValueError):
                continue
    if not pairs:
        return ""
    return "Корреляция (30 дн.): " + ", ".join(pairs[:10]) + (" …" if len(pairs) > 10 else "")
