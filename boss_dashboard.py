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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


WATCHLIST = ["SNDK", "MU", "LITE", "ALAB", "TER", "MSFT"]


@dataclass
class TickerContext:
    ticker: str
    price: float
    vix_value: Optional[float]
    vix_mode: str
    corr_with_mu: Optional[float]
    corr_label: str
    volatility_5: Optional[float]
    strategy_name: Optional[str]
    recommendation: str
    reasoning: str
    portfolio_status: Optional[str]
    portfolio_tactics: Optional[str]


def get_engine():
    db_url = get_database_url()
    return create_engine(db_url)


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
) -> (str, str):
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

    reasoning = " ".join(reasoning_parts)
    return recommendation, reasoning


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
    header = (
        f"{ctx.ticker:<6} | Price: ${ctx.price:8.2f} | "
        f"VIX: {ctx.vix_value:5.2f} ({ctx.vix_mode}) | "
        f"Corr vs MU: {ctx.corr_with_mu if ctx.corr_with_mu is not None else float('nan'):5.2f} "
        f"({ctx.corr_label})"
    )

    strategy_line = f"Strategy Selected: {ctx.strategy_name or 'N/A'}"
    recommendation_line = f"Recommendation: {ctx.recommendation}"
    reasoning_line = f"Reasoning: {ctx.reasoning}"

    print("-" * 100)
    print(header)
    print(strategy_line)
    print(recommendation_line)
    print(reasoning_line)

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

        # Получаем решение/стратегию от AnalystAgent/StrategyManager
        decision_result = analyst.get_decision_with_llm(ticker)
        selected_strategy_name = decision_result.get("selected_strategy")

        # Последние цены для оценки "хайлов"
        latest_prices = get_latest_quotes_window(engine, ticker)

        recommendation, reasoning = select_recommendation(
            ticker=ticker,
            price=price,
            vix_mode=vix_mode,
            corr_label=corr_label,
            strategy_name=selected_strategy_name,
            latest_prices=latest_prices,
        )

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
            strategy_name=selected_strategy_name,
            recommendation=recommendation,
            reasoning=reasoning,
            portfolio_status=portfolio_status,
            portfolio_tactics=portfolio_tactics,
        )

        render_ticker_line(ctx)

    print("-" * 100)
    print("End of Boss Dashboard snapshot.")


if __name__ == "__main__":
    run_boss_dashboard()

