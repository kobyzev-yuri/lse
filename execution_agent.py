import logging
from dataclasses import dataclass
from datetime import datetime
from math import floor

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from analyst_agent import AnalystAgent
from config_loader import get_database_url


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


INITIAL_CASH_USD = 100_000.0
COMMISSION_RATE = 0.001  # 0.1%
STOP_LOSS_LEVEL = 0.95   # 5% падение от цены входа


@dataclass
class Position:
    ticker: str
    quantity: float
    entry_price: float
    entry_ts: datetime


class ExecutionAgent:
    """
    Агент исполнения сделок:
    - использует AnalystAgent для получения сигналов
    - хранит виртуальный портфель и сделки в БД lse_trading
    - управляет открытыми позициями и стоп‑лоссами
    """

    def __init__(self):
        self.db_url = get_database_url()
        self.engine = create_engine(self.db_url)
        self.analyst = AnalystAgent()

        logger.info("✅ ExecutionAgent инициализирован, подключение к БД установлено")
        self._ensure_portfolio_initialized()

    # ---------- Инициализация БД ----------

    def _ensure_portfolio_initialized(self) -> None:
        """Проверяет наличие записи CASH в portfolio_state, создает если нет."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text("SELECT COUNT(*) FROM portfolio_state WHERE ticker = 'CASH'")
            ).scalar()

            if result == 0:
                conn.execute(
                    text("""
                        INSERT INTO portfolio_state (ticker, quantity, avg_entry_price, last_updated)
                        VALUES ('CASH', :cash, 0, CURRENT_TIMESTAMP)
                    """),
                    {"cash": INITIAL_CASH_USD},
                )
                logger.info(
                    "✅ Портфель инициализирован: cash=%.2f USD", INITIAL_CASH_USD
                )
            else:
                logger.info("✅ Портфель уже инициализирован")

    # ---------- Вспомогательные методы ----------

    def _get_cash(self) -> float:
        """Получает текущий баланс кэша из portfolio_state."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
            ).fetchone()
            if result:
                return float(result[0])
            return INITIAL_CASH_USD

    def _update_cash(self, new_cash: float) -> None:
        """Обновляет баланс кэша в portfolio_state."""
        with self.engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE portfolio_state
                    SET quantity = :cash, last_updated = CURRENT_TIMESTAMP
                    WHERE ticker = 'CASH'
                """),
                {"cash": new_cash},
            )

    def _has_open_position(self, ticker: str) -> bool:
        """Проверяет наличие открытой позиции по тикеру."""
        with self.engine.connect() as conn:
            cnt = conn.execute(
                text("SELECT COUNT(*) FROM portfolio_state WHERE ticker = :ticker AND ticker != 'CASH'"),
                {"ticker": ticker},
            ).scalar()
        return cnt > 0

    def _get_open_positions(self) -> pd.DataFrame:
        """Получает все открытые позиции (исключая CASH)."""
        with self.engine.connect() as conn:
            df = pd.read_sql(
                text("""
                    SELECT ticker, quantity, avg_entry_price as entry_price, last_updated as entry_ts
                    FROM portfolio_state
                    WHERE ticker != 'CASH' AND quantity > 0
                """),
                conn,
            )
        return df

    def _get_position(self, ticker: str) -> Position | None:
        """Получает информацию о позиции по тикеру."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT ticker, quantity, avg_entry_price, last_updated
                    FROM portfolio_state
                    WHERE ticker = :ticker AND ticker != 'CASH'
                """),
                {"ticker": ticker},
            ).fetchone()
        
        if result:
            return Position(
                ticker=result[0],
                quantity=float(result[1]),
                entry_price=float(result[2]),
                entry_ts=result[3],
            )
        return None

    def _get_current_price(self, ticker: str) -> float | None:
        """Получает последнюю цену закрытия для тикера."""
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT close
                    FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 1
                """),
                {"ticker": ticker},
            ).fetchone()
        
        if result:
            return float(result[0])
        return None

    def _get_weighted_sentiment(self, ticker: str) -> float:
        """Получает взвешенный sentiment для тикера (для записи в trade_history)."""
        try:
            news_df = self.analyst.get_recent_news(ticker)
            if not news_df.empty:
                return float(self.analyst.calculate_weighted_sentiment(news_df, ticker))
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить sentiment для {ticker}: {e}")
        return 0.0
    
    def _get_last_strategy_name(self, ticker: str) -> str:
        """Получает название стратегии из последней сделки BUY для тикера."""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT strategy_name
                        FROM trade_history
                        WHERE ticker = :ticker AND side = 'BUY'
                        ORDER BY ts DESC
                        LIMIT 1
                    """),
                    {"ticker": ticker}
                ).fetchone()
                if result and result[0]:
                    return str(result[0])
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить strategy_name для {ticker}: {e}")
        return None

    # ---------- Торговые операции ----------

    def _execute_buy(self, ticker: str, decision: str, strategy_name: str = None) -> None:
        """Имитация покупки по сигналу BUY/STRONG_BUY."""
        if self._has_open_position(ticker):
            logger.info(
                "ℹ️ Позиция по %s уже открыта, покупка пропущена", ticker
            )
            return

        current_price = self._get_current_price(ticker)
        if current_price is None:
            logger.warning("⚠️ Нет котировок для %s, покупка невозможна", ticker)
            return

        # Размер позиции: 10% от доступного кэша
        cash = self._get_cash()
        allocation = cash * 0.10
        if allocation <= 0:
            logger.warning("⚠️ Нет свободного кэша для покупки %s", ticker)
            return

        quantity = floor(allocation / current_price)
        if quantity <= 0:
            logger.warning(
                "⚠️ Слишком маленький размер аллокации (%.2f) для покупки %s по цене %.2f",
                allocation,
                ticker,
                current_price,
            )
            return

        notional = quantity * current_price
        commission = notional * COMMISSION_RATE
        total_cost = notional + commission

        if total_cost > cash:
            logger.warning(
                "⚠️ Недостаточно кэша (%.2f) для покупки %s на сумму %.2f",
                cash,
                ticker,
                total_cost,
            )
            return

        # Получаем sentiment для записи в историю
        sentiment = self._get_weighted_sentiment(ticker)

        with self.engine.begin() as conn:
            # Обновляем кэш
            self._update_cash(cash - total_cost)

            # Добавляем позицию в portfolio_state
            conn.execute(
                text("""
                    INSERT INTO portfolio_state (ticker, quantity, avg_entry_price, last_updated)
                    VALUES (:ticker, :quantity, :price, CURRENT_TIMESTAMP)
                    ON CONFLICT (ticker) DO UPDATE SET
                        quantity = portfolio_state.quantity + :quantity,
                        avg_entry_price = (
                            (portfolio_state.quantity * portfolio_state.avg_entry_price + :quantity * :price) /
                            (portfolio_state.quantity + :quantity)
                        ),
                        last_updated = CURRENT_TIMESTAMP
                """),
                {
                    "ticker": ticker,
                    "quantity": float(quantity),
                    "price": current_price,
                },
            )

            # Записываем сделку в trade_history
            conn.execute(
                text("""
                    INSERT INTO trade_history (
                        ts, ticker, side, quantity, price, commission,
                        signal_type, total_value, sentiment_at_trade, strategy_name
                    )
                    VALUES (
                        CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission,
                        :signal, :total_value, :sentiment, :strategy_name
                    )
                """),
                {
                    "ticker": ticker,
                    "qty": float(quantity),
                    "price": current_price,
                    "commission": commission,
                    "signal": decision,
                    "total_value": total_cost,
                    "sentiment": sentiment,
                    "strategy_name": strategy_name,
                },
            )

        logger.info(
            "🟢 BUY %s x %.0f @ %.2f, notional=%.2f, fee=%.2f, sentiment=%.3f (signal=%s, strategy=%s)",
            ticker,
            quantity,
            current_price,
            notional,
            commission,
            sentiment,
            decision,
            strategy_name or "N/A",
        )

    def _execute_sell(self, ticker: str, position: Position, reason: str, strategy_name: str = None) -> None:
        """Закрытие позиции по текущей цене (например, по стоп‑лоссу)."""
        current_price = self._get_current_price(ticker)
        if current_price is None:
            logger.warning(
                "⚠️ Нет котировок для %s, закрытие позиции невозможно", ticker
            )
            return

        quantity = float(position.quantity)
        notional = quantity * current_price
        commission = notional * COMMISSION_RATE
        total_proceeds = notional - commission

        # Лог‑доходность по позиции
        log_ret = float(np.log(current_price / position.entry_price))

        cash = self._get_cash()
        sentiment = self._get_weighted_sentiment(ticker)

        with self.engine.begin() as conn:
            # Обновляем кэш
            self._update_cash(cash + total_proceeds)

            # Удаляем позицию из portfolio_state
            conn.execute(
                text("DELETE FROM portfolio_state WHERE ticker = :ticker"),
                {"ticker": ticker},
            )

            # Записываем сделку в trade_history
            signal_type = "STOP_LOSS" if "Stop-loss" in reason else "SELL"
            conn.execute(
                text("""
                    INSERT INTO trade_history (
                        ts, ticker, side, quantity, price, commission,
                        signal_type, total_value, sentiment_at_trade, strategy_name
                    )
                    VALUES (
                        CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission,
                        :signal, :total_value, :sentiment, :strategy_name
                    )
                """),
                {
                    "ticker": ticker,
                    "qty": quantity,
                    "price": current_price,
                    "commission": commission,
                    "signal": signal_type,
                    "total_value": total_proceeds,
                    "sentiment": sentiment,
                    "strategy_name": strategy_name,
                },
            )

        logger.info(
            "🔴 SELL %s x %.0f @ %.2f, notional=%.2f, fee=%.2f, log_return=%.4f, sentiment=%.3f (%s, strategy=%s)",
            ticker,
            quantity,
            current_price,
            notional,
            commission,
            log_ret,
            sentiment,
            reason,
            strategy_name or "N/A",
        )

    # ---------- Публичные методы ----------

    def run_for_tickers(self, tickers: list[str], use_llm: bool = True) -> None:
        """
        Запускает цикл анализа и исполнения по списку тикеров:
        - получает сигнал от AnalystAgent (с LLM или без)
        - открывает позиции по BUY / STRONG_BUY, если их ещё нет
        - проверяет стоп‑лоссы
        
        Args:
            tickers: Список тикеров для анализа
            use_llm: Использовать LLM анализ (по умолчанию True)
        """
        logger.info("=" * 60)
        logger.info("🚀 Запуск ExecutionAgent для тикеров: %s", ", ".join(tickers))
        logger.info("=" * 60)

        for ticker in tickers:
            result = None
            decision = "HOLD"
            strategy_name = None
            
            if use_llm and hasattr(self.analyst, 'get_decision_with_llm'):
                try:
                    result = self.analyst.get_decision_with_llm(ticker)
                    decision = result.get('decision', 'HOLD')
                    strategy_name = result.get('selected_strategy')  # Получаем название стратегии
                    logger.info("🎯 Сигнал AnalystAgent (с LLM) для %s: %s", ticker, decision)
                    if strategy_name:
                        logger.info("   Стратегия: %s", strategy_name)
                    if result.get('llm_analysis'):
                        logger.info("   LLM рекомендация: %s (уверенность: %.1f%%)", 
                                  result['llm_analysis'].get('decision', 'N/A'),
                                  result['llm_analysis'].get('confidence', 0) * 100)
                except Exception as e:
                    logger.warning("⚠️ Ошибка LLM анализа для %s, используем базовый анализ: %s", ticker, e)
                    result = self.analyst.get_decision(ticker)
                    decision = result if isinstance(result, str) else result.get('decision', 'HOLD')
                    strategy_name = result.get('selected_strategy') if isinstance(result, dict) else None
                    logger.info("🎯 Сигнал AnalystAgent (базовый) для %s: %s", ticker, decision)
            else:
                result = self.analyst.get_decision(ticker)
                if isinstance(result, dict):
                    decision = result.get('decision', 'HOLD')
                    strategy_name = result.get('selected_strategy')
                else:
                    decision = result
                logger.info("🎯 Сигнал AnalystAgent для %s: %s", ticker, decision)
                if strategy_name:
                    logger.info("   Стратегия: %s", strategy_name)

            if decision in ("BUY", "STRONG_BUY"):
                self._execute_buy(ticker, decision, strategy_name)
            else:
                logger.info("ℹ️ Сигнал %s для %s, покупка не выполняется", decision, ticker)

        # После обработки всех тикеров проверяем стоп‑лоссы
        self.check_stop_losses()

    def check_stop_losses(self) -> None:
        """
        Проходит по открытым позициям и закрывает их,
        если цена упала на 5% от цены входа (используем лог‑доходность).
        """
        logger.info("🛡  Проверка стоп‑лоссов по открытым позициям")

        positions_df = self._get_open_positions()
        if positions_df.empty:
            logger.info("ℹ️ Открытых позиций нет, стоп‑лоссы не проверяются")
            return

        stop_log_threshold = float(np.log(STOP_LOSS_LEVEL))  # ~ -0.0513

        for _, pos_row in positions_df.iterrows():
            ticker = pos_row["ticker"]
            entry_price = float(pos_row["entry_price"])
            entry_ts = pos_row["entry_ts"]

            current_price = self._get_current_price(ticker)
            if current_price is None:
                logger.warning(
                    "⚠️ Нет текущей цены для %s, пропускаем проверку стоп‑лосса",
                    ticker,
                )
                continue

            log_ret = float(np.log(current_price / entry_price))

            logger.info(
                "📉 Проверка стоп‑лосса для %s: entry=%.2f, current=%.2f, log_ret=%.4f, threshold=%.4f",
                ticker,
                entry_price,
                current_price,
                log_ret,
                stop_log_threshold,
            )

            if log_ret <= stop_log_threshold:
                reason = (
                    f"Stop-loss triggered: log_return={log_ret:.4f} "
                    f"(entry={entry_price:.2f}, current={current_price:.2f})"
                )
                position = Position(
                    ticker=ticker,
                    quantity=float(pos_row["quantity"]),
                    entry_price=entry_price,
                    entry_ts=entry_ts,
                )
                # Получаем strategy_name из последней сделки BUY для этого тикера
                strategy_name = self._get_last_strategy_name(ticker)
                self._execute_sell(ticker, position, reason, strategy_name)
            else:
                logger.info(
                    "✅ Стоп‑лосс для %s не сработал (log_ret=%.4f > %.4f)",
                    ticker,
                    log_ret,
                    stop_log_threshold,
                )


if __name__ == "__main__":
    agent = ExecutionAgent()
    test_tickers = ["MSFT", "SNDK"]
    agent.run_for_tickers(test_tickers)
