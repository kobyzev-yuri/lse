"""
Кластерные рекомендации: загрузка данных по всем тикерам кластера, корреляция, единый контекст.

- Игра 5m: тикеры из GAME_5M_TICKERS; решения по каждому с учётом корреляции и общих данных.
- Портфель (медленные/средние): тикеры из TRADING_CYCLE_TICKERS; рекомендации по каждому с кластерным контекстом.

Используется в /recommend (без тикера), /recommend5m (без тикера) и в кронах для входов/закрытий.
"""

from __future__ import annotations

import math
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def get_avg_volatility_20_pct_from_quotes(ticker: str) -> Optional[float]:
    """
    Средняя дневная volatility_5 за 20 последних дат в `quotes`, в процентах от последнего close.

    Та же формула, что в web_app для LLM-карточки game5m: AVG(volatility_5) / last_close * 100.
    Нужна для поля avg_volatility_20 в промпте analyze_trading_situation (GAME_5M).

    Returns:
        Число в %% или None, если в БД нет строк, нет close, или расчёт не удался.
        (Отличается от volatility_5m_pct в recommend_5m — там интрадей-логвола по 5m барам.)
    """
    t = (ticker or "").strip().upper()
    if not t:
        return None
    try:
        from sqlalchemy import text
        from report_generator import get_engine

        eng = get_engine()
        with eng.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT (SELECT AVG(volatility_5) FROM (
                        SELECT volatility_5 FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 20
                    ) s) AS avg_vol,
                           (SELECT close FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1) AS last_close
                """),
                {"ticker": t},
            ).fetchone()
        if row and row[0] is not None and row[1] is not None and float(row[1]) > 0:
            return round(float(row[0]) / float(row[1]) * 100, 2)
    except Exception as e:
        logger.debug("avg_volatility_20 из quotes для %s: %s", t, e)
    return None


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


def build_cluster_note_for_5m_llm(
    ticker: str,
    full_list: List[str],
    correlation_matrix: Optional[Dict[str, Dict[str, float]]],
    tech_by_ticker: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Собирает текст «Кластер и корреляция» для промпта LLM в game_5m. Общий для крона и бота."""
    if not correlation_matrix or not full_list:
        return None
    others = [x for x in full_list if x != ticker]
    if not others:
        return None
    lines = [f"Кластер тикеров: {', '.join(full_list)}. Анализируемый тикер: {ticker}."]
    corr_pairs = []
    for o in others:
        c = correlation_matrix.get(ticker, {}).get(o) or correlation_matrix.get(o, {}).get(ticker)
        if c is not None and math.isfinite(c):
            try:
                corr_pairs.append(f"{o} {float(c):+.2f}")
            except (TypeError, ValueError):
                pass
    if corr_pairs:
        lines.append(f"Корреляция с другими (30 дн.): {', '.join(corr_pairs[:15])}.")
    other_rows: List[Tuple[str, Optional[float], Optional[float], float]] = []
    for t in full_list:
        if t == ticker:
            continue
        tech = tech_by_ticker.get(t) or {}
        pr = tech.get("price")
        rsi_o = tech.get("rsi")
        c_val = None
        raw = correlation_matrix.get(ticker, {}).get(t) or correlation_matrix.get(t, {}).get(ticker)
        if raw is not None:
            try:
                f_c = float(raw)
                if math.isfinite(f_c):
                    c_val = f_c
            except (TypeError, ValueError):
                pass
        if c_val is not None:
            other_rows.append((t, pr, rsi_o, c_val))
    other_rows.sort(key=lambda r: r[3], reverse=True)
    if other_rows:
        ctx_lines = []
        for t, pr, rsi_o, c_val in other_rows:
            seg = [t]
            if pr is not None:
                seg.append(f"${pr:.2f}")
            if rsi_o is not None:
                seg.append(f"RSI {rsi_o:.1f}")
            seg.append(f"corr {c_val:+.2f}")
            ctx_lines.append(" ".join(seg))
        lines.append("По тикерам с известной корреляцией (по убыванию корр.): " + "; ".join(ctx_lines) + ".")
    lines.append("Учти: при высокой корреляции с другим активом они часто движутся вместе; при расхождении сигналов — осторожность.")
    return "\n".join(lines)


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
