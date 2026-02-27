import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import floor

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from analyst_agent import AnalystAgent
from config_loader import get_database_url, get_config_value
from utils.risk_manager import get_risk_manager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


INITIAL_CASH_USD = 100_000.0
COMMISSION_RATE = 0.0  # 0% — оплаты брокеру нет
STOP_LOSS_LEVEL = 0.95   # 5% падение от цены входа


def _get_slippage_sell_pct() -> float:
    """Проскальзывание при продаже (%), 0 = отключено. Учитывает, что реальная цена исполнения может быть хуже последней котировки."""
    try:
        return max(0.0, min(5.0, float(get_config_value("SANDBOX_SLIPPAGE_SELL_PCT", "0").strip() or "0")))
    except (ValueError, TypeError):
        return 0.0


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
        self.risk_manager = get_risk_manager()

        logger.info("✅ ExecutionAgent инициализирован, подключение к БД установлено")
        logger.info(f"   Risk Manager: загружены лимиты из {self.risk_manager.config_path}")
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
    
    def _get_current_portfolio_exposure(self) -> float:
        """
        Вычисляет текущую экспозицию портфеля в USD
        
        Returns:
            Текущая экспозиция в USD
        """
        positions = self._get_open_positions()
        if positions.empty:
            return 0.0
        
        total_exposure = 0.0
        for _, pos in positions.iterrows():
            current_price = self._get_current_price(pos['ticker'])
            if current_price:
                total_exposure += pos['quantity'] * current_price
        
        return total_exposure

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

        # Проверка торговых часов NYSE (если настроено)
        if not self.risk_manager.is_trading_hours():
            logger.warning("⚠️ Вне торговых часов NYSE, покупка %s пропущена", ticker)
            return

        cash = self._get_cash()
        
        # Получаем максимальный размер позиции из risk limits
        max_position_size = self.risk_manager.get_max_position_size(ticker)
        
        # Размер позиции: минимум из 10% кэша и максимального лимита
        allocation_percent = min(0.10, self.risk_manager.get_max_single_ticker_exposure() / 100.0)
        allocation = min(cash * allocation_percent, max_position_size)
        
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

        # Проверка risk limits перед покупкой
        is_valid, error_msg = self.risk_manager.check_position_size(notional, ticker)
        if not is_valid:
            logger.warning(f"⚠️ Risk limit нарушен для {ticker}: {error_msg}")
            return
        
        # Проверка экспозиции портфеля
        current_exposure = self._get_current_portfolio_exposure()
        is_valid_exposure, exposure_error = self.risk_manager.check_portfolio_exposure(
            current_exposure, notional
        )
        if not is_valid_exposure:
            logger.warning(f"⚠️ Экспозиция портфеля превышена: {exposure_error}")
            return

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
        """Закрытие позиции по текущей цене (например, по стоп‑лоссу). При SANDBOX_SLIPPAGE_SELL_PCT > 0 цена исполнения занижается (консервативная оценка)."""
        current_price = self._get_current_price(ticker)
        if current_price is None:
            logger.warning(
                "⚠️ Нет котировок для %s, закрытие позиции невозможна", ticker
            )
            return
        slippage_pct = _get_slippage_sell_pct()
        if slippage_pct > 0:
            current_price = current_price * (1 - slippage_pct / 100.0)
            logger.debug("Продажа %s: учтено проскальзывание %.2f%%, цена исполнения %.2f", ticker, slippage_pct, current_price)

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

    # ---------- Ручная торговля (песочница / Telegram) ----------

    def execute_manual_buy(self, ticker: str, quantity: float, skip_trading_hours: bool = True) -> tuple[bool, str]:
        """
        Ручная покупка по последней цене из quotes (для песочницы в Telegram).
        Returns: (success, message)
        """
        if self._has_open_position(ticker):
            return False, f"По {ticker} уже есть открытая позиция. Закройте её через /sell."
        price = self._get_current_price(ticker)
        if price is None:
            return False, f"Нет котировок для {ticker}. Дождитесь обновления цен (cron)."
        if not skip_trading_hours and not self.risk_manager.is_trading_hours():
            return False, "Вне торговых часов (для песочницы можно отключить проверку)."
        quantity = floor(float(quantity))
        if quantity <= 0:
            return False, "Укажите количество > 0."
        cash = self._get_cash()
        notional = quantity * price
        commission = notional * COMMISSION_RATE
        total_cost = notional + commission
        is_valid, err = self.risk_manager.check_position_size(notional, ticker)
        if not is_valid:
            return False, f"Лимит риска: {err}"
        current_exposure = self._get_current_portfolio_exposure()
        is_ok, err = self.risk_manager.check_portfolio_exposure(current_exposure, notional)
        if not is_ok:
            return False, f"Экспозиция портфеля: {err}"
        if total_cost > cash:
            return False, f"Недостаточно средств: нужно {total_cost:.2f} USD, доступно {cash:.2f} USD."
        sentiment = self._get_weighted_sentiment(ticker)
        with self.engine.begin() as conn:
            self._update_cash(cash - total_cost)
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
                {"ticker": ticker, "quantity": float(quantity), "price": price},
            )
            conn.execute(
                text("""
                    INSERT INTO trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission, 'MANUAL', :total_value, :sentiment, 'Manual')
                """),
                {"ticker": ticker, "qty": float(quantity), "price": price, "commission": commission, "total_value": total_cost, "sentiment": sentiment},
            )
        logger.info("🟢 MANUAL BUY %s x %.0f @ %.2f", ticker, quantity, price)
        return True, f"Куплено {quantity:.0f} {ticker} @ ${price:.2f} (комиссия ${commission:.2f}). Сумма: ${total_cost:.2f}"

    def execute_manual_sell(self, ticker: str, quantity: float | None = None, skip_trading_hours: bool = True) -> tuple[bool, str]:
        """
        Ручная продажа по последней цене. quantity=None — закрыть всю позицию.
        Returns: (success, message)
        """
        position = self._get_position(ticker)
        if not position:
            return False, f"Нет открытой позиции по {ticker}."
        price = self._get_current_price(ticker)
        if price is None:
            return False, f"Нет котировок для {ticker}."
        slippage_pct = _get_slippage_sell_pct()
        if slippage_pct > 0:
            price = price * (1 - slippage_pct / 100.0)
        if not skip_trading_hours and not self.risk_manager.is_trading_hours():
            return False, "Вне торговых часов."
        qty = floor(float(quantity)) if quantity is not None else float(position.quantity)
        qty = min(qty, float(position.quantity))
        if qty <= 0:
            return False, "Укажите количество > 0."
        notional = qty * price
        commission = notional * COMMISSION_RATE
        proceeds = notional - commission
        entry_value = qty * position.entry_price
        pnl = proceeds - entry_value
        pnl_pct = 100.0 * (price - position.entry_price) / position.entry_price
        cash = self._get_cash()
        sentiment = self._get_weighted_sentiment(ticker)
        with self.engine.begin() as conn:
            self._update_cash(cash + proceeds)
            if qty >= position.quantity:
                conn.execute(text("DELETE FROM portfolio_state WHERE ticker = :ticker"), {"ticker": ticker})
            else:
                new_qty = float(position.quantity) - qty
                conn.execute(
                    text("UPDATE portfolio_state SET quantity = :qty, last_updated = CURRENT_TIMESTAMP WHERE ticker = :ticker"),
                    {"qty": new_qty, "ticker": ticker},
                )
            conn.execute(
                text("""
                    INSERT INTO trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission, 'MANUAL', :total_value, :sentiment, 'Manual')
                """),
                {"ticker": ticker, "qty": qty, "price": price, "commission": commission, "total_value": proceeds, "sentiment": sentiment},
            )
        logger.info("🔴 MANUAL SELL %s x %.0f @ %.2f P&L=%.2f", ticker, qty, price, pnl)
        return True, f"Продано {qty:.0f} {ticker} @ ${price:.2f}. P&L: ${pnl:.2f} ({pnl_pct:+.2f}%)"

    def get_portfolio_summary(self) -> dict:
        """Сводка виртуального портфеля для бота: cash, позиции с текущей оценкой и P&L."""
        cash = self._get_cash()
        positions = self._get_open_positions()
        lines = []
        total_equity = cash
        for _, pos in positions.iterrows():
            ticker = pos["ticker"]
            qty = float(pos["quantity"])
            entry = float(pos["entry_price"])
            current = self._get_current_price(ticker)
            if current is None:
                current = entry
            value = qty * current
            total_equity += value
            pnl = (current - entry) * qty
            pnl_pct = 100.0 * (current - entry) / entry
            lines.append({
                "ticker": ticker,
                "quantity": qty,
                "entry_price": entry,
                "current_price": current,
                "value": value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            })
        return {"cash": cash, "positions": lines, "total_equity": total_equity}

    def get_trade_history(
        self,
        limit: int = 20,
        ticker: str | None = None,
        strategy_name: str | None = None,
    ) -> list[dict]:
        """Последние сделки для бота. ticker/strategy_name — опциональные фильтры."""
        query = """
            SELECT ts, ticker, side, quantity, price, signal_type, total_value, strategy_name
            FROM trade_history
        """
        params: dict = {"limit": limit}
        conditions = []
        if ticker:
            conditions.append("ticker = :ticker")
            params["ticker"] = ticker
        if strategy_name:
            conditions.append("strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY ts DESC LIMIT :limit"
        with self.engine.connect() as conn:
            rows = conn.execute(text(query), params).fetchall()
        return [
            {
                "ts": r[0],
                "ticker": r[1],
                "side": r[2],
                "quantity": float(r[3]),
                "price": float(r[4]),
                "signal_type": r[5],
                "total_value": float(r[6]),
                "strategy_name": r[7] or "—",
            }
            for r in rows
        ]

    def get_recent_trades(
        self,
        minutes_ago: int = 5,
        exclude_strategy_name: str | None = "GAME_5M",
    ) -> list[dict]:
        """Сделки за последние N минут, опционально исключая стратегию (например GAME_5M). Для уведомлений в Telegram по портфельной игре."""
        since = datetime.now() - timedelta(minutes=minutes_ago)
        query = """
            SELECT ts, ticker, side, quantity, price, signal_type, total_value, strategy_name
            FROM trade_history
            WHERE ts >= :since
        """
        params: dict = {"since": since, "limit": 100}
        if exclude_strategy_name:
            query += " AND (strategy_name IS NULL OR strategy_name != :exclude)"
            params["exclude"] = exclude_strategy_name
        query += " ORDER BY ts DESC LIMIT :limit"
        with self.engine.connect() as conn:
            rows = conn.execute(text(query), params).fetchall()
        return [
            {
                "ts": r[0],
                "ticker": r[1],
                "side": r[2],
                "quantity": float(r[3]),
                "price": float(r[4]),
                "signal_type": r[5],
                "total_value": float(r[6]),
                "strategy_name": r[7] or "—",
            }
            for r in rows
        ]

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
