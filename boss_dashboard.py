"""
Boss Dashboard – основной терминал для принятия решений.

Функционал:
  - Подключение к БД lse_trading через config_loader.get_database_url.
  - Использует AnalystAgent и StrategyManager для выбора стратегий/сигналов.
  - Проходит по списку тикеров наблюдения:
        ['SNDK', 'MU', 'LITE', 'ALAB', 'TER', 'MSFT']
  - Для каждого тикера рассчитывает:
        * текущую (последнюю) цену
        * режим рынка по VIX (LOW_FEAR / NEUTRAL / HIGH_PANIC)
        * скользящую корреляцию за последние 14 дней с MU
        * волатильность (volatility_5 из БД)
        * выбранную стратегию (через StrategyManager, если доступен)
        * рекомендацию: STRONG_BUY / HOLD / LIMIT_ORDER
        * текстовое обоснование (Reasoning Engine)
        * интеграцию с текущим портфелем (status/tactics, если позиция уже открыта)

Вывод оформляется аккуратным текстом, напоминающим терминал Bloomberg.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from config_loader import get_database_url
from analyst_agent import AnalystAgent
from strategy_manager import get_strategy_manager, StrategyManager
from services.vector_kb import VectorKB
from services.news_impact_analyzer import NewsImpactAnalyzer


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


WATCHLIST = ["SNDK", "MU", "LITE", "ALAB", "TER", "MSFT"]


@dataclass
class NewsImpactTrace:
    """Структура для отслеживания влияния новостей на решение"""
    base_recommendation: str  # Рекомендация до учета новостей
    final_recommendation: str  # Финальная рекомендация после учета новостей
    recommendation_changed: bool  # Была ли изменена рекомендация
    change_reason: Optional[str] = None  # Причина изменения
    news_count: int = 0
    similar_events_count: int = 0
    impact_pattern: Optional[str] = None
    impact_confidence: Optional[float] = None
    historical_avg_change: Optional[float] = None
    sentiment_score: Optional[float] = None  # Взвешенный sentiment из AnalystAgent


@dataclass
class TickerContext:
    ticker: str
    price: float
    vix_value: Optional[float]
    vix_mode: str
    corr_with_mu: Optional[float]
    corr_label: str
    volatility_5: Optional[float]
    rsi: Optional[float] = None  # RSI (0-100) для определения перекупленности/перепроданности
    strategy_name: Optional[str]
    recommendation: str
    reasoning: str
    portfolio_status: Optional[str]
    portfolio_tactics: Optional[str]
    # Новые поля для Vector KB и анализа новостей
    recent_news_count: int = 0
    similar_events_count: int = 0
    news_impact_pattern: Optional[str] = None  # 'POSITIVE', 'NEGATIVE', 'NEUTRAL', None
    news_impact_confidence: Optional[float] = None  # 0.0-1.0
    historical_avg_change: Optional[float] = None  # Среднее изменение цены после похожих событий (%)
    news_impact_trace: Optional[NewsImpactTrace] = None  # Детальная трассировка влияния новостей


def get_engine():
    db_url = get_database_url()
    return create_engine(db_url)


# Глобальные экземпляры Vector KB и анализатора (ленивая инициализация)
_vector_kb: Optional[VectorKB] = None
_news_analyzer: Optional[NewsImpactAnalyzer] = None


def get_vector_kb() -> VectorKB:
    """Ленивая инициализация VectorKB"""
    global _vector_kb
    if _vector_kb is None:
        _vector_kb = VectorKB()
    return _vector_kb


def get_news_analyzer() -> NewsImpactAnalyzer:
    """Ленивая инициализация NewsImpactAnalyzer"""
    global _news_analyzer
    if _news_analyzer is None:
        _news_analyzer = NewsImpactAnalyzer()
    return _news_analyzer


def get_last_price(engine, ticker: str) -> Optional[float]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT close
                FROM quotes
                WHERE ticker = :ticker
                ORDER BY date DESC
                LIMIT 1
                """
            ),
            {"ticker": ticker},
        ).fetchone()
    return float(row[0]) if row else None


def get_latest_quotes_window(engine, ticker: str, days: int = 30) -> pd.DataFrame:
    """Загружает последние N календарных дней котировок для тикера."""
    end = datetime.now()
    start = end - timedelta(days=days)
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                """
                SELECT date, close
                FROM quotes
                WHERE ticker = :ticker
                  AND date >= :start
                  AND date <= :end
                ORDER BY date ASC
                """
            ),
            conn,
            params={"ticker": ticker, "start": start, "end": end},
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def compute_rolling_corr_with_mu(engine, ticker: str, window_days: int = 14) -> Optional[float]:
    """
    Скользящая корреляция лог‑доходностей ticker vs MU за последние window_days.
    Для дашборда берём последнее доступное значение rolling‑corr.
    """
    if ticker == "MU":
        return 1.0

    end = datetime.now()
    start = end - timedelta(days=window_days + 30)  # небольшой буфер

    snd = get_latest_quotes_window(engine, ticker)
    mu = get_latest_quotes_window(engine, "MU")
    if snd.empty or mu.empty:
        return None

    joined = snd.join(mu, how="inner", lsuffix="_THIS", rsuffix="_MU")
    if joined.shape[0] < window_days + 1:
        return None

    prices = joined[["close_THIS", "close_MU"]]
    log_ret = np.log(prices / prices.shift(1)).dropna()
    if log_ret.empty or log_ret.shape[0] < window_days:
        return None

    rolling_corr = (
        log_ret["close_THIS"]
        .rolling(window=window_days)
        .corr(log_ret["close_MU"])
    )
    last_corr = rolling_corr.dropna().iloc[-1] if not rolling_corr.dropna().empty else None
    return float(last_corr) if last_corr is not None else None


def get_latest_volatility_5(engine, ticker: str) -> Optional[float]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT volatility_5
                FROM quotes
                WHERE ticker = :ticker
                ORDER BY date DESC
                LIMIT 1
                """
            ),
            {"ticker": ticker},
        ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def get_latest_rsi(engine, ticker: str) -> Optional[float]:
    """Получает последнее значение RSI для тикера"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT rsi
                FROM quotes
                WHERE ticker = :ticker
                  AND rsi IS NOT NULL
                ORDER BY date DESC
                LIMIT 1
                """
            ),
            {"ticker": ticker},
        ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def interpret_rsi(rsi: Optional[float]) -> tuple[str, str]:
    """
    Интерпретирует значение RSI
    
    Returns:
        (emoji, status_text)
    """
    if rsi is None:
        return "⚪", "N/A"
    
    if rsi >= 70:
        return "🔴", "перекупленность"
    elif rsi <= 30:
        return "🟢", "перепроданность"
    elif rsi >= 60:
        return "🟡", "близко к перекупленности"
    elif rsi <= 40:
        return "🟡", "близко к перепроданности"
    else:
        return "⚪", "нейтральная зона"


def get_portfolio_info(engine, ticker: str) -> Dict[str, Any]:
    """
    Возвращает информацию по открытой позиции в portfolio_state (если есть).
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT quantity, avg_entry_price
                FROM portfolio_state
                WHERE ticker = :ticker AND ticker != 'CASH' AND quantity > 0
                """
            ),
            {"ticker": ticker},
        ).fetchone()
    if not row:
        return {}
    return {"quantity": float(row[0]), "entry_price": float(row[1])}


def classify_correlation(corr: Optional[float]) -> str:
    if corr is None:
        return "Unknown"
    if abs(corr) < 0.3:
        return "Independent"
    return "In-Sync"


def map_vix_regime(regime: str) -> str:
    if regime == "LOW_FEAR":
        return "LOW_FEAR"
    if regime == "HIGH_PANIC":
        return "HIGH_PANIC"
    if regime == "NEUTRAL":
        return "NEUTRAL"
    return "NO_DATA"


def select_recommendation(
    ticker: str,
    price: float,
    vix_mode: str,
    corr_label: str,
    strategy_name: Optional[str],
    latest_prices: pd.DataFrame,
    news_impact_pattern: Optional[str] = None,
    news_impact_confidence: Optional[float] = None,
    historical_avg_change: Optional[float] = None,
    sentiment_score: Optional[float] = None,
) -> tuple[str, str, NewsImpactTrace]:
    """
    Возвращает (recommendation, reasoning_text) на основе заданных правил.
    Возможные рекомендации:
        - STRONG_BUY
        - HOLD
        - LIMIT_ORDER
    """
    # Определим "хай" за последний месяц как контекст
    recent_high = float(latest_prices["close"].max()) if not latest_prices.empty else price
    at_highs = price >= recent_high * 0.99  # текущая цена в пределах 1% от локальных максимумов

    reasoning_parts: List[str] = []

    # VIX режим
    if vix_mode == "LOW_FEAR":
        if at_highs:
            recommendation = "STRONG_BUY"
            reasoning_parts.append(
                "Рынок в эйфории (LOW_FEAR), цена торгуется около локальных максимумов — Chasing разрешен, "
                "вход на пробое оправдан."
            )
        else:
            recommendation = "STRONG_BUY"
            reasoning_parts.append(
                "VIX низкий (LOW_FEAR), рынок спокоен — можно агрессивно докупать на пробое ближайших сопротивлений."
            )
    elif vix_mode == "NEUTRAL":
        if corr_label == "Independent":
            recommendation = "STRONG_BUY"
            reasoning_parts.append(
                "VIX нейтральный, корреляция с сектором низкая — бумага идет на собственных драйверах, "
                "предпочтителен вход на 2‑й день подтвержденного отскока."
            )
        else:
            recommendation = "HOLD"
            reasoning_parts.append(
                "VIX нейтральный, бумага движется синхронно с сектором — ждём дополнительного подтверждения "
                "по объему и новостям."
            )
    elif vix_mode == "HIGH_PANIC":
        recommendation = "LIMIT_ORDER"
        reasoning_parts.append(
            "Рыночная паника (HIGH_PANIC): высокие риски гэпов и проскальзывания. "
            "Рекомендуются только глубокие лимитные ордера на 1–2 ATR/volatility_5 ниже текущей цены."
        )
    else:
        recommendation = "HOLD"
        reasoning_parts.append(
            "Режим VIX не определен, консервативный режим — удерживаем позиции или ждём ясности."
        )

    if strategy_name:
        reasoning_parts.append(f"Выбрана стратегия: {strategy_name}, что задаёт базовый контур риск‑менеджмента.")
    
    # Сохраняем базовую рекомендацию до учета новостей
    base_recommendation = recommendation
    recommendation_changed = False
    change_reason = None
    
    # Интеграция анализа новостей и исторических паттернов
    news_influence_applied = False
    
    if news_impact_pattern and news_impact_confidence and news_impact_confidence > 0.5:
        if news_impact_pattern == "POSITIVE" and historical_avg_change and historical_avg_change > 2.0:
            # Исторически похожие новости приводили к росту
            if recommendation == "HOLD":
                recommendation = "STRONG_BUY"
                recommendation_changed = True
                change_reason = f"Исторический анализ показал рост {historical_avg_change:.1f}% после похожих событий"
            reasoning_parts.append(
                f"📈 Исторический анализ: похожие события приводили к росту {historical_avg_change:.1f}% "
                f"(уверенность {news_impact_confidence:.0%}, выборка {int(1/news_impact_confidence) if news_impact_confidence > 0 else 0} событий)."
            )
            news_influence_applied = True
        elif news_impact_pattern == "NEGATIVE" and historical_avg_change and historical_avg_change < -2.0:
            # Исторически похожие новости приводили к падению
            if recommendation == "STRONG_BUY":
                recommendation = "LIMIT_ORDER"
                recommendation_changed = True
                change_reason = f"Исторический анализ показал падение {abs(historical_avg_change):.1f}% после похожих событий"
            reasoning_parts.append(
                f"📉 Исторический анализ: похожие события приводили к падению {abs(historical_avg_change):.1f}% "
                f"(уверенность {news_impact_confidence:.0%}). Рекомендуется осторожность."
            )
            news_influence_applied = True
        elif news_impact_pattern == "NEUTRAL":
            reasoning_parts.append(
                f"➡️ Исторический анализ: похожие события не оказывали значимого влияния "
                f"(уверенность {news_impact_confidence:.0%})."
            )
            news_influence_applied = True
    
    # Учет sentiment из AnalystAgent
    if sentiment_score is not None:
        if sentiment_score > 0.3 and not news_influence_applied:
            # Сильный положительный sentiment может усилить рекомендацию
            if recommendation == "HOLD" and abs(sentiment_score) > 0.5:
                recommendation = "STRONG_BUY"
                recommendation_changed = True
                change_reason = f"Сильный положительный sentiment ({sentiment_score:.2f})"
            reasoning_parts.append(f"Sentiment анализ: {sentiment_score:.2f} ({'положительный' if sentiment_score > 0 else 'отрицательный' if sentiment_score < 0 else 'нейтральный'})")
        elif sentiment_score < -0.3 and not news_influence_applied:
            # Сильный отрицательный sentiment может ослабить рекомендацию
            if recommendation == "STRONG_BUY" and abs(sentiment_score) > 0.5:
                recommendation = "LIMIT_ORDER"
                recommendation_changed = True
                change_reason = f"Сильный отрицательный sentiment ({sentiment_score:.2f})"
            reasoning_parts.append(f"Sentiment анализ: {sentiment_score:.2f} ({'положительный' if sentiment_score > 0 else 'отрицательный' if sentiment_score < 0 else 'нейтральный'})")

    reasoning = " ".join(reasoning_parts)
    
    # Создаем трассировку влияния новостей
    trace = NewsImpactTrace(
        base_recommendation=base_recommendation,
        final_recommendation=recommendation,
        recommendation_changed=recommendation_changed,
        change_reason=change_reason,
        impact_pattern=news_impact_pattern,
        impact_confidence=news_impact_confidence,
        historical_avg_change=historical_avg_change,
        sentiment_score=sentiment_score,
    )
    
    return recommendation, reasoning, trace


def get_recent_news_for_ticker(engine, ticker: str, days: int = 7) -> int:
    """
    Возвращает количество новостей для тикера за последние N дней.
    """
    try:
        cutoff = datetime.now() - timedelta(days=days)
        with engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT COUNT(*)
                    FROM knowledge_base
                    WHERE ticker = :ticker
                      AND COALESCE(ingested_at, ts) >= :cutoff
                      AND content IS NOT NULL
                      AND LENGTH(content) > 10
                """),
                {"ticker": ticker, "cutoff": cutoff}
            )
            count = result.fetchone()[0]
            return int(count) if count else 0
    except Exception as e:
        logger.debug(f"Ошибка получения новостей для {ticker}: {e}")
        return 0


def analyze_news_impact_pattern(
    ticker: str,
    current_context: str = None
) -> tuple[Optional[str], Optional[float], Optional[float], int]:
    """
    Анализирует влияние новостей на основе исторических паттернов.
    
    Args:
        ticker: Тикер инструмента
        current_context: Текущий контекст/новость (если есть)
    
    Returns:
        (pattern_type, confidence, avg_change, similar_events_count)
        pattern_type: 'POSITIVE', 'NEGATIVE', 'NEUTRAL', None
        confidence: 0.0-1.0
        avg_change: Среднее изменение цены (%)
        similar_events_count: Количество похожих событий
    """
    try:
        vector_kb = get_vector_kb()
        analyzer = get_news_analyzer()
        
        # Формируем запрос для поиска похожих событий
        # Если есть текущий контекст, используем его, иначе ищем по тикеру
        if current_context:
            query = f"{ticker} {current_context}"
        else:
            # Получаем последнюю новость для тикера как контекст
            engine = get_engine()
            with engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT content
                        FROM knowledge_base
                        WHERE ticker = :ticker
                          AND content IS NOT NULL
                          AND LENGTH(content) > 10
                        ORDER BY ts DESC
                        LIMIT 1
                    """),
                    {"ticker": ticker}
                )
                row = result.fetchone()
                if row:
                    query = f"{ticker} {row[0][:200]}"  # Первые 200 символов
                else:
                    query = ticker
        
        # Ищем похожие исторические события
        similar_events = vector_kb.search_similar(
            query=query,
            ticker=ticker,
            limit=10,
            min_similarity=0.4,
            time_window_days=365
        )
        
        if similar_events.empty:
            return None, None, None, 0
        
        # Анализируем паттерны
        patterns = analyzer.aggregate_patterns(similar_events)
        
        pattern_type = patterns.get('typical_outcome')
        confidence = patterns.get('confidence', 0.0)
        avg_change = patterns.get('avg_price_change', 0.0)
        sample_size = patterns.get('sample_size', 0)
        
        return pattern_type, confidence, avg_change, sample_size
        
    except Exception as e:
        logger.debug(f"Ошибка анализа паттернов новостей для {ticker}: {e}")
        return None, None, None, 0


def format_portfolio_block(
    engine,
    ticker: str,
    price: float,
) -> (Optional[str], Optional[str]):
    """
    Формирует блок статуса портфеля:
        Status: In Profit / In Loss
        Tactics: «Синица в руках» / «Ожидание цели»
    """
    info = get_portfolio_info(engine, ticker)
    if not info:
        return None, None

    entry_price = info["entry_price"]
    pnl_pct = (price / entry_price - 1.0) * 100 if entry_price > 0 else 0.0

    status = "In Profit" if pnl_pct > 0 else "In Loss" if pnl_pct < 0 else "Flat"

    # Эвристика по тактике: если прибыль > 3% — предполагаем, что partial TP мог быть реализован
    if pnl_pct > 3.0:
        tactics = "«Синица в руках»: часть прибыли зафиксирована, остаток защищен стопом."
    else:
        tactics = "«Ожидание цели»: позиция держится до ключевых уровней/сигналов."

    return status, tactics


def render_ticker_line(ctx: TickerContext) -> None:
    """
    Печатает блок в стиле терминала Bloomberg.
    """
    # Форматируем RSI
    rsi_text = ""
    if ctx.rsi is not None:
        rsi_emoji, rsi_status = interpret_rsi(ctx.rsi)
        rsi_text = f" | RSI: {ctx.rsi:5.1f} {rsi_emoji} ({rsi_status})"
    
    header = (
        f"{ctx.ticker:<6} | Price: ${ctx.price:8.2f} | "
        f"VIX: {ctx.vix_value:5.2f} ({ctx.vix_mode}) | "
        f"Corr vs MU: {ctx.corr_with_mu if ctx.corr_with_mu is not None else float('nan'):5.2f} "
        f"({ctx.corr_label}){rsi_text}"
    )

    strategy_line = f"Strategy Selected: {ctx.strategy_name or 'N/A'}"
    recommendation_line = f"Recommendation: {ctx.recommendation}"
    reasoning_line = f"Reasoning: {ctx.reasoning}"

    print("-" * 100)
    print(header)
    print(strategy_line)
    print(recommendation_line)
    print(reasoning_line)

    # Блок новостей и исторических паттернов с детальной трассировкой влияния
    if ctx.news_impact_trace:
        trace = ctx.news_impact_trace
        print(f"📰 News Impact Analysis:")
        print(f"   News count (7d): {trace.news_count}")
        print(f"   Similar historical events: {trace.similar_events_count}")
        
        if trace.sentiment_score is not None:
            sentiment_label = "positive" if trace.sentiment_score > 0.3 else "negative" if trace.sentiment_score < -0.3 else "neutral"
            print(f"   Sentiment score: {trace.sentiment_score:.3f} ({sentiment_label})")
        
        if trace.impact_pattern and trace.impact_confidence:
            pattern_emoji = "📈" if trace.impact_pattern == "POSITIVE" else "📉" if trace.impact_pattern == "NEGATIVE" else "➡️"
            print(f"   Historical pattern: {pattern_emoji} {trace.impact_pattern} (confidence: {trace.impact_confidence:.0%})")
            if trace.historical_avg_change is not None:
                change_sign = "+" if trace.historical_avg_change >= 0 else ""
                print(f"   Historical avg price change: {change_sign}{trace.historical_avg_change:.1f}%")
        
        # Детальная информация о влиянии на решение
        print(f"   Base recommendation (before news): {trace.base_recommendation}")
        print(f"   Final recommendation (after news): {trace.final_recommendation}")
        
        if trace.recommendation_changed:
            print(f"   ⚠️  RECOMMENDATION CHANGED due to news analysis!")
            if trace.change_reason:
                print(f"   Reason: {trace.change_reason}")
        else:
            print(f"   ✓ Recommendation unchanged (news analysis confirmed base decision)")
    elif ctx.recent_news_count > 0 or ctx.similar_events_count > 0:
        # Fallback для старого формата (если trace не создан)
        news_info = []
        if ctx.recent_news_count > 0:
            news_info.append(f"News (7d): {ctx.recent_news_count}")
        if ctx.similar_events_count > 0:
            news_info.append(f"Similar events: {ctx.similar_events_count}")
        if ctx.news_impact_pattern and ctx.news_impact_confidence:
            pattern_emoji = "📈" if ctx.news_impact_pattern == "POSITIVE" else "📉" if ctx.news_impact_pattern == "NEGATIVE" else "➡️"
            news_info.append(
                f"Impact: {pattern_emoji} {ctx.news_impact_pattern} "
                f"({ctx.news_impact_confidence:.0%})"
            )
        if ctx.historical_avg_change is not None:
            change_sign = "+" if ctx.historical_avg_change >= 0 else ""
            news_info.append(f"Avg change: {change_sign}{ctx.historical_avg_change:.1f}%")
        
        if news_info:
            print(f"News Analysis: {' | '.join(news_info)}")

    if ctx.portfolio_status and ctx.portfolio_tactics:
        print(f"Status: {ctx.portfolio_status}")
        print(f"Tactics: {ctx.portfolio_tactics}")


def run_boss_dashboard() -> None:
    """
    Основной вход: строит сводный отчёт по всем тикерам WATCHLIST.
    """
    engine = get_engine()
    analyst = AnalystAgent(use_llm=False, use_strategy_factory=True)
    strategy_manager: StrategyManager = get_strategy_manager()

    # Текущее состояние VIX
    vix_info = analyst.get_vix_regime()
    vix_value = vix_info.get("vix_value")
    vix_mode = map_vix_regime(vix_info.get("regime"))

    print("=" * 100)
    print(
        f" Boss Dashboard | VIX: {vix_value if vix_value is not None else float('nan'):5.2f} "
        f"({vix_mode}) | Time: {datetime.now().isoformat(timespec='seconds')}"
    )
    print("=" * 100)

    for ticker in WATCHLIST:
        price = get_last_price(engine, ticker)
        if price is None:
            logger.warning("Нет котировок для %s, пропускаем", ticker)
            continue

        # Корреляция с MU
        corr = compute_rolling_corr_with_mu(engine, ticker, window_days=14)
        corr_label = classify_correlation(corr)

        # Волатильность
        vol_5 = get_latest_volatility_5(engine, ticker)
        
        # RSI для определения перекупленности/перепроданности
        rsi = get_latest_rsi(engine, ticker)

        # Получаем решение/стратегию от AnalystAgent/StrategyManager
        decision_result = analyst.get_decision_with_llm(ticker)
        selected_strategy_name = decision_result.get("selected_strategy")
        # Получаем sentiment_score из результата (уже нормализован в -1.0 до 1.0)
        sentiment_score = decision_result.get("sentiment_normalized") or decision_result.get("sentiment_score")
        if sentiment_score is not None and isinstance(sentiment_score, (int, float)):
            # Если sentiment в шкале 0.0-1.0, конвертируем в -1.0-1.0
            if 0.0 <= sentiment_score <= 1.0:
                sentiment_score = (sentiment_score - 0.5) * 2.0

        # Последние цены для оценки "хайлов"
        latest_prices = get_latest_quotes_window(engine, ticker)

        # Анализ новостей и исторических паттернов через Vector KB
        recent_news_count = get_recent_news_for_ticker(engine, ticker, days=7)
        news_pattern, news_confidence, avg_change, similar_count = analyze_news_impact_pattern(ticker)

        recommendation, reasoning, news_trace = select_recommendation(
            ticker=ticker,
            price=price,
            vix_mode=vix_mode,
            corr_label=corr_label,
            strategy_name=selected_strategy_name,
            latest_prices=latest_prices,
            news_impact_pattern=news_pattern,
            news_impact_confidence=news_confidence,
            historical_avg_change=avg_change,
            sentiment_score=sentiment_score,
        )
        
        # Обновляем trace с дополнительной информацией
        news_trace.news_count = recent_news_count
        news_trace.similar_events_count = similar_count

        # Интеграция с портфелем
        portfolio_status, portfolio_tactics = format_portfolio_block(
            engine,
            ticker,
            price,
        )

        ctx = TickerContext(
            ticker=ticker,
            price=price,
            vix_value=vix_value,
            vix_mode=vix_mode,
            corr_with_mu=corr,
            corr_label=corr_label,
            volatility_5=vol_5,
            rsi=rsi,
            strategy_name=selected_strategy_name,
            recommendation=recommendation,
            reasoning=reasoning,
            portfolio_status=portfolio_status,
            portfolio_tactics=portfolio_tactics,
            recent_news_count=recent_news_count,
            similar_events_count=similar_count,
            news_impact_pattern=news_pattern,
            news_impact_confidence=news_confidence,
            historical_avg_change=avg_change,
            news_impact_trace=news_trace,
        )

        render_ticker_line(ctx)

    print("-" * 100)
    print("End of Boss Dashboard snapshot.")


if __name__ == "__main__":
    run_boss_dashboard()

