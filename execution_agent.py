import logging
import json
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


def _get_initial_cash_usd() -> float:
    """Начальный кэш = капитал из local/risk_limits.json (risk_capacity.total_capital_usd). Один источник правды."""
    return get_risk_manager().get_total_capital()
COMMISSION_RATE = 0.0  # 0% — оплаты брокеру нет
STOP_LOSS_LEVEL = 0.95   # 5% падение от цены входа

# Таймзона меток ts в trade_history (храним явно, конвертируем в ET при отображении)
TRADE_HISTORY_TZ = "Europe/Moscow"


def _get_slippage_sell_pct(engine=None) -> float:
    """Проскальзывание при продаже (%), 0 = отключено. Учитывает, что реальная цена исполнения может быть хуже последней котировки."""
    try:
        from config_loader import get_dynamic_config_value
        val = get_dynamic_config_value("SANDBOX_SLIPPAGE_SELL_PCT", "0", engine=engine)
        return max(0.0, min(5.0, float(str(val).strip() or "0")))
    except (Exception):
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
        from config_loader import get_use_llm_for_analyst
        use_llm = get_use_llm_for_analyst(engine=self.engine)
        self.analyst = AnalystAgent(use_llm=use_llm)
        self.risk_manager = get_risk_manager(engine=self.engine)
        self._trades_done_this_run: list[dict] = []  # сделки этого запуска для уведомлений в Telegram
        self._stop_loss_disabled_warned = False  # предупреждение «стоп отключён» один раз за запуск

        from config_loader import get_dynamic_config_value
        # Читаем глобальные параметры один раз за запуск (БД strategy_parameters или config.env)
        self.commission_rate = float(get_dynamic_config_value("COMMISSION_RATE", "0.0", engine=self.engine))
        self.stop_loss_level = float(get_dynamic_config_value("STOP_LOSS_LEVEL", "0.95", engine=self.engine))
        # Стоп-лосс портфеля: true = проверять по STOP_LOSS_LEVEL; false = отключён (только тейк). Из config или БД (strategy_parameters GLOBAL).
        _sl_raw = (get_dynamic_config_value("PORTFOLIO_STOP_LOSS_ENABLED", "true", engine=self.engine) or "true").strip().lower()
        self.stop_loss_enabled = _sl_raw in ("1", "true", "yes")
        _exit_take_raw = (get_dynamic_config_value("PORTFOLIO_EXIT_ONLY_TAKE", "false", engine=self.engine) or "false").strip().lower()
        self.exit_only_take = _exit_take_raw in ("1", "true", "yes")

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
                initial = _get_initial_cash_usd()
                conn.execute(
                    text("""
                        INSERT INTO portfolio_state (ticker, quantity, avg_entry_price, last_updated)
                        VALUES ('CASH', :cash, 0, CURRENT_TIMESTAMP)
                    """),
                    {"cash": initial},
                )
                logger.info(
                    "✅ Портфель инициализирован: cash=%.2f USD", initial
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
            return _get_initial_cash_usd()

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
        """Проверяет наличие открытой позиции по тикеру (только quantity > 0).

        Раньше считалась любая строка в portfolio_state — при «нулевой» строке после частичных
        правок cron логировал BUY от аналитика, но _execute_buy выходил сразу, без trade_history.
        """
        with self.engine.connect() as conn:
            cnt = conn.execute(
                text(
                    "SELECT COUNT(*) FROM portfolio_state WHERE ticker = :ticker "
                    "AND ticker != 'CASH' AND quantity > 0"
                ),
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
            qty = float(result[1])
            if qty <= 0:
                return None
            return Position(
                ticker=result[0],
                quantity=qty,
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

    def _get_last_take_profit(self, ticker: str) -> float | None:
        """Возвращает take_profit (%) из последней сделки BUY по тикеру (портфельная игра)."""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT take_profit FROM trade_history
                        WHERE ticker = :ticker AND side = 'BUY'
                        ORDER BY ts DESC, id DESC LIMIT 1
                    """),
                    {"ticker": ticker},
                ).fetchone()
            if result and result[0] is not None:
                return float(result[0])
        except Exception as e:
            logger.debug("Не удалось получить take_profit для %s: %s", ticker, e)
        return None

    # ---------- Торговые операции ----------

    def _execute_buy(self, ticker: str, decision: str, strategy_name: str = None,
                     stop_loss: float = None, take_profit: float = None, context_json: dict = None) -> bool:
        """Имитация покупки по сигналу BUY/STRONG_BUY. True — сделка записана в БД."""
        if self._has_open_position(ticker):
            logger.info(
                "ℹ️ Позиция по %s уже открыта, покупка пропущена", ticker
            )
            return False

        current_price = self._get_current_price(ticker)
        if current_price is None:
            logger.warning("⚠️ Нет котировок для %s, покупка невозможна", ticker)
            return False

        # Проверка торговых часов NYSE (если настроено)
        if not self.risk_manager.is_trading_hours():
            logger.warning("⚠️ Вне торговых часов NYSE, покупка %s пропущена", ticker)
            return False

        cash = self._get_cash()
        
        # Получаем максимальный размер позиции из risk limits
        max_position_size = self.risk_manager.get_max_position_size(ticker)
        
        # Размер позиции: минимум из 10% кэша и максимального лимита
        allocation_percent = min(0.10, self.risk_manager.get_max_single_ticker_exposure() / 100.0)
        allocation = min(cash * allocation_percent, max_position_size)
        
        if allocation <= 0:
            logger.warning("⚠️ Нет свободного кэша для покупки %s", ticker)
            return False

        quantity = floor(allocation / current_price)
        if quantity <= 0:
            logger.warning(
                "⚠️ Слишком маленький размер аллокации (%.2f) для покупки %s по цене %.2f",
                allocation,
                ticker,
                current_price,
            )
            return False

        notional = quantity * current_price
        commission = notional * self.commission_rate
        total_cost = notional + commission

        # Проверка risk limits перед покупкой
        is_valid, error_msg = self.risk_manager.check_position_size(notional, ticker)
        if not is_valid:
            logger.warning(f"⚠️ Risk limit нарушен для {ticker}: {error_msg}")
            return False
        
        # Проверка экспозиции портфеля
        current_exposure = self._get_current_portfolio_exposure()
        is_valid_exposure, exposure_error = self.risk_manager.check_portfolio_exposure(
            current_exposure, notional
        )
        if not is_valid_exposure:
            logger.warning(f"⚠️ Экспозиция портфеля превышена: {exposure_error}")
            return False

        if total_cost > cash:
            logger.warning(
                "⚠️ Недостаточно кэша (%.2f) для покупки %s на сумму %.2f",
                cash,
                ticker,
                total_cost,
            )
            return False

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

            # Записываем сделку в trade_history (strategy_name не должен быть NULL)
            strategy_name = (strategy_name or "").strip() or "Portfolio"
            conn.execute(
                text("""
                    INSERT INTO trade_history (
                        ts, ticker, side, quantity, price, commission,
                        signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone,
                        take_profit, stop_loss, context_json
                    )
                    VALUES (
                        CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission,
                        :signal, :total_value, :sentiment, :strategy_name, :ts_tz,
                        :take_profit, :stop_loss, :context_json
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
                    "ts_tz": TRADE_HISTORY_TZ,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                    "context_json": json.dumps(context_json) if context_json else None,
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
        self._trades_done_this_run.append({
            "ts": datetime.now(),
            "ticker": ticker,
            "side": "BUY",
            "quantity": quantity,
            "price": current_price,
            "signal_type": decision,
            "strategy_name": (strategy_name or "").strip() or "Portfolio",
        })
        return True

    def _execute_sell(self, ticker: str, position: Position, reason: str, strategy_name: str = None) -> None:
        """Закрытие позиции по текущей цене (например, по стоп‑лоссу). При SANDBOX_SLIPPAGE_SELL_PCT > 0 цена исполнения занижается (консервативная оценка)."""
        current_price = self._get_current_price(ticker)
        if current_price is None:
            logger.warning(
                "⚠️ Нет котировок для %s, закрытие позиции невозможна", ticker
            )
            return
        slippage_pct = _get_slippage_sell_pct(self.engine)
        if slippage_pct > 0:
            current_price = current_price * (1 - slippage_pct / 100.0)
            logger.debug("Продажа %s: учтено проскальзывание %.2f%%, цена исполнения %.2f", ticker, slippage_pct, current_price)

        quantity = float(position.quantity)
        notional = quantity * current_price
        commission = notional * self.commission_rate
        total_proceeds = notional - commission

        # Лог‑доходность по позиции
        log_ret = float(np.log(current_price / position.entry_price))

        # Расчет MFE/MAE (максимальной прибыли/убытка)
        mfe, mae = None, None
        try:
            with self.engine.connect() as conn:
                mfe_mae_result = conn.execute(text("""
                    SELECT MAX(high), MIN(low) FROM quotes 
                    WHERE ticker = :ticker AND date >= :entry_ts
                """), {"ticker": ticker, "entry_ts": position.entry_ts}).fetchone()
                
                if mfe_mae_result and mfe_mae_result[0] and mfe_mae_result[1]:
                    max_price = float(mfe_mae_result[0])
                    min_price = float(mfe_mae_result[1])
                    mfe = (max_price - position.entry_price) / position.entry_price * 100.0
                    mae = (min_price - position.entry_price) / position.entry_price * 100.0
        except Exception as e:
            logger.warning(f"⚠️ Ошибка при расчете MFE/MAE для {ticker}: {e}")

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

            # Записываем сделку в trade_history (strategy_name не должен быть NULL)
            strategy_name = (strategy_name or "").strip() or "Portfolio"
            signal_type = "TAKE_PROFIT" if "Take-profit" in reason else ("STOP_LOSS" if "Stop-loss" in reason else "SELL")
            conn.execute(
                text("""
                    INSERT INTO trade_history (
                        ts, ticker, side, quantity, price, commission,
                        signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone,
                        mfe, mae
                    )
                    VALUES (
                        CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission,
                        :signal, :total_value, :sentiment, :strategy_name, :ts_tz,
                        :mfe, :mae
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
                    "ts_tz": TRADE_HISTORY_TZ,
                    "mfe": mfe,
                    "mae": mae,
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
        self._trades_done_this_run.append({
            "ts": datetime.now(),
            "ticker": ticker,
            "side": "SELL",
            "quantity": quantity,
            "price": current_price,
            "signal_type": signal_type,
            "strategy_name": (strategy_name or "").strip() or "Portfolio",
        })

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
        commission = notional * self.commission_rate
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
                    INSERT INTO trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'BUY', :qty, :price, :commission, 'MANUAL', :total_value, :sentiment, 'Manual', :ts_tz)
                """),
                {"ticker": ticker, "qty": float(quantity), "price": price, "commission": commission, "total_value": total_cost, "sentiment": sentiment, "ts_tz": TRADE_HISTORY_TZ},
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
        commission = notional * self.commission_rate
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
                if new_qty <= 0:
                    conn.execute(text("DELETE FROM portfolio_state WHERE ticker = :ticker"), {"ticker": ticker})
                else:
                    conn.execute(
                        text(
                            "UPDATE portfolio_state SET quantity = :qty, last_updated = CURRENT_TIMESTAMP "
                            "WHERE ticker = :ticker"
                        ),
                        {"qty": new_qty, "ticker": ticker},
                    )
            conn.execute(
                text("""
                    INSERT INTO trade_history (ts, ticker, side, quantity, price, commission, signal_type, total_value, sentiment_at_trade, strategy_name, ts_timezone)
                    VALUES (CURRENT_TIMESTAMP, :ticker, 'SELL', :qty, :price, :commission, 'MANUAL', :total_value, :sentiment, 'Manual', :ts_tz)
                """),
                {"ticker": ticker, "qty": qty, "price": price, "commission": commission, "total_value": proceeds, "sentiment": sentiment, "ts_tz": TRADE_HISTORY_TZ},
            )
        logger.info("🔴 MANUAL SELL %s x %.0f @ %.2f P&L=%.2f", ticker, qty, price, pnl)
        return True, f"Продано {qty:.0f} {ticker} @ ${price:.2f}. P&L: ${pnl:.2f} ({pnl_pct:+.2f}%)"

    def get_portfolio_summary(self) -> dict:
        """Сводка виртуального портфеля для бота: cash, позиции с текущей оценкой и P&L, суммарная доходность."""
        cash = self._get_cash()
        initial_cash = _get_initial_cash_usd()
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
        total_return_pct = (
            (total_equity - initial_cash) / initial_cash * 100.0
            if initial_cash and initial_cash > 0
            else None
        )
        return {
            "cash": cash,
            "positions": lines,
            "total_equity": total_equity,
            "initial_cash": initial_cash,
            "total_return_pct": total_return_pct,
        }

    def get_trade_history(
        self,
        limit: int = 20,
        ticker: str | None = None,
        strategy_name: str | None = None,
    ) -> list[dict]:
        """Последние сделки для бота. ticker/strategy_name — опциональные фильтры."""
        params: dict = {"limit": limit}
        conditions = []
        if ticker:
            conditions.append("ticker = :ticker")
            params["ticker"] = ticker
        if strategy_name:
            conditions.append("strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        query_with_tz = f"""
            SELECT ts, ticker, side, quantity, price, signal_type, total_value, strategy_name,
                   COALESCE(ts_timezone, 'Europe/Moscow') AS ts_timezone
            FROM trade_history
            {where_clause}
            ORDER BY ts DESC LIMIT :limit
        """
        query_without_tz = f"""
            SELECT ts, ticker, side, quantity, price, signal_type, total_value, strategy_name
            FROM trade_history
            {where_clause}
            ORDER BY ts DESC LIMIT :limit
        """
        with self.engine.connect() as conn:
            try:
                rows = conn.execute(text(query_with_tz), params).fetchall()
                has_ts_tz = True
            except Exception as e:
                if "ts_timezone" in str(e) or "undefined_column" in str(e).lower():
                    rows = conn.execute(text(query_without_tz), params).fetchall()
                    has_ts_tz = False
                else:
                    raise
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
                "ts_timezone": r[8] if has_ts_tz and len(r) > 8 else TRADE_HISTORY_TZ,
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

    def set_open_position_strategy(self, ticker: str, strategy_name: str) -> bool:
        """
        Меняет стратегию у открытой позиции: обновляет strategy_name у последнего BUY по тикеру.
        Используйте для позиций «вне игры» (например GC=F после вывода из GAME_5M): переназначьте на Manual или Portfolio.
        Возвращает True, если обновлена одна строка.
        """
        strategy_name = (strategy_name or "").strip() or "Manual"
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    UPDATE trade_history SET strategy_name = :strategy_name
                    WHERE id = (
                        SELECT id FROM trade_history
                        WHERE ticker = :ticker AND side = 'BUY'
                        ORDER BY ts DESC, id DESC LIMIT 1
                    )
                """),
                {"ticker": ticker, "strategy_name": strategy_name},
            )
            conn.commit()
        return result.rowcount == 1

    # ---------- Публичные методы ----------

    def run_for_tickers(
        self,
        tickers: list[str],
        use_llm: bool = True,
        cluster_tickers: list[str] | None = None,
    ) -> None:
        """
        Запускает цикл анализа и исполнения по списку тикеров с учётом кластера:
        - загружает корреляцию по cluster_tickers (если задан) или по tickers
        - по каждому тикеру из tickers получает сигнал, передавая кластерный контекст (корреляция с полным списком)
        - открывает позиции только по tickers (индикаторы в cluster_tickers не торгуем)
        
        Args:
            tickers: Тикеры, по которым принимаем решения и открываем позиции
            use_llm: Использовать LLM анализ
            cluster_tickers: Полный список для матрицы корреляций (включая индикаторы ^VIX и т.д.). Если None — равен tickers.
        """
        logger.info("=" * 60)
        logger.info("🚀 Запуск ExecutionAgent для тикеров: %s", ", ".join(tickers))
        logger.info("=" * 60)
        self._trades_done_this_run = []

        # Кластер: корреляция по полному списку (включая индикаторы) — LLM видит связи с VIX и др.
        list_for_corr = cluster_tickers if cluster_tickers is not None else tickers
        cluster_context = None
        if len(list_for_corr) >= 2:
            try:
                from services.cluster_recommend import get_correlation_matrix
                correlation = get_correlation_matrix(list_for_corr, days=30)
                if correlation:
                    cluster_context = {"tickers": list_for_corr, "correlation": correlation}
                    logger.info("Кластер портфеля: корреляция для %s", list_for_corr)
            except Exception as e:
                logger.debug("Кластер портфеля (продолжаем без корреляции): %s", e)

        other_signals: dict[str, str] = {}  # по мере прохода добавляем решения — следующий тикер видит предыдущие
        for ticker in tickers:
            result = None
            decision = "HOLD"
            strategy_name = None
            stop_loss = None
            take_profit = None
            context_json = None
            
            if use_llm and hasattr(self.analyst, 'get_decision_with_llm'):
                try:
                    ctx = cluster_context.copy() if cluster_context else None
                    if ctx and other_signals:
                        ctx = {**ctx, "other_signals": dict(other_signals)}
                    result = self.analyst.get_decision_with_llm(ticker, cluster_context=ctx)
                    decision = result.get('decision', 'HOLD')
                    strategy_name = result.get('selected_strategy')  # Получаем название стратегии
                    
                    strategy_result_dict = result.get('strategy_result')
                    if strategy_result_dict:
                        stop_loss = strategy_result_dict.get('stop_loss')
                        take_profit = strategy_result_dict.get('take_profit')
                        
                    context_json = {
                        "technical_data": result.get("technical_data", {}),
                        "sentiment": result.get("sentiment_normalized", 0.0),
                        "base_decision": result.get("base_decision")
                    }
                    # Снимок кластера при решении — для последующего анализа: как корреляция и other_signals повлияли на исход сделки
                    if cluster_context:
                        corr = cluster_context.get("correlation") or {}
                        correlation_this = {}
                        for o in (cluster_context.get("tickers") or []):
                            if o == ticker:
                                continue
                            c = corr.get(ticker, {}).get(o) or corr.get(o, {}).get(ticker)
                            if c is not None:
                                try:
                                    correlation_this[o] = round(float(c), 4)
                                except (TypeError, ValueError):
                                    pass
                        context_json["cluster"] = {
                            "tickers": list(cluster_context.get("tickers") or []),
                            "correlation_this_ticker": correlation_this,
                            "other_signals_at_decision": dict(other_signals),
                        }
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
                executed = self._execute_buy(
                    ticker, decision, strategy_name, stop_loss, take_profit, context_json
                )
                if not executed:
                    logger.warning(
                        "⚠️ %s: аналитик дал %s, но сделка BUY в БД не записана "
                        "(см. предупреждения выше: кэш, котировки, лимиты риска, экспозиция).",
                        ticker,
                        decision,
                    )
            else:
                logger.info("ℹ️ Сигнал %s для %s, покупка не выполняется", decision, ticker)

            if cluster_context is not None:
                other_signals[ticker] = decision

        # После обработки всех тикеров проверяем стоп‑лоссы
        self.check_stop_losses()

    def check_stop_losses(self) -> None:
        """
        Проходит по открытым позициям и закрывает их по стопу (если включён) или по тейку.
        Список позиций берём из trade_history (как в /pending).
        """
        if not self.stop_loss_enabled and not self._stop_loss_disabled_warned:
            self._stop_loss_disabled_warned = True
            logger.warning(
                "⚠️ Стоп-лосс отключён в настройках (PORTFOLIO_STOP_LOSS_ENABLED=false). "
                "Закрытие по стопу не выполняется, проверяется только тейк-профит."
            )
        if self.exit_only_take:
            logger.info("ℹ️ Режим PORTFOLIO_EXIT_ONLY_TAKE=true: автозакрытие только по тейк-профиту.")
        logger.info("🛡  Проверка стоп‑лоссов по открытым позициям")

        try:
            from report_generator import load_trade_history, compute_open_positions
            trades = load_trade_history(self.engine)
            open_from_history = {p.ticker: p for p in compute_open_positions(trades)}
        except Exception as e:
            logger.debug("Не удалось загрузить открытые позиции из trade_history: %s", e)
            open_from_history = {}

        # Если по trade_history позиций нет — пробуем portfolio_state (обратная совместимость)
        if not open_from_history:
            positions_df = self._get_open_positions()
            if positions_df.empty:
                logger.info("ℹ️ Открытых позиций нет, стоп‑лоссы не проверяются")
                return
            for _, pos_row in positions_df.iterrows():
                ticker = pos_row["ticker"]
                open_from_history[ticker] = type("OpenPos", (), {
                    "entry_price": float(pos_row["entry_price"]),
                    "entry_ts": pos_row["entry_ts"],
                    "quantity": float(pos_row["quantity"]),
                    "strategy_name": self._get_last_strategy_name(ticker) or "Portfolio",
                })()

        stop_log_threshold = float(np.log(self.stop_loss_level)) if self.stop_loss_enabled else 0.0  # при отключённом стопе не срабатывает

        for ticker, p_open in open_from_history.items():
            entry_price = float(p_open.entry_price)
            entry_ts = p_open.entry_ts
            quantity = float(p_open.quantity)
            strategy_name = (getattr(p_open, "strategy_name", None) or "").strip() or "Portfolio"

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

            if (not self.exit_only_take) and self.stop_loss_enabled and log_ret <= stop_log_threshold:
                reason = (
                    f"Stop-loss triggered: log_return={log_ret:.4f} "
                    f"(entry={entry_price:.2f}, current={current_price:.2f})"
                )
                position = Position(
                    ticker=ticker,
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_ts=entry_ts,
                )
                self._execute_sell(ticker, position, reason, strategy_name)
                continue

            # Тейк‑профит: при входе задан take_profit (%) или порог из конфига PORTFOLIO_TAKE_PROFIT_PCT (для стратегий без тейка, напр. Neutral). GAME_5M не трогаем — закрывает send_sndk_signal_cron.
            if strategy_name == "GAME_5M":
                logger.info("✅ %s — GAME_5M, тейк/стоп проверяет send_sndk_signal_cron", ticker)
                continue
            take_pct = self._get_last_take_profit(ticker)
            from_config = False
            if take_pct is None:
                try:
                    take_pct = float(get_config_value("PORTFOLIO_TAKE_PROFIT_PCT", "0").strip() or "0")
                    from_config = True
                except (ValueError, TypeError):
                    take_pct = 0.0
            pnl_pct = (current_price - entry_price) / entry_price * 100.0
            if take_pct is not None and take_pct > 0:
                if from_config:
                    logger.info("📈 Тейк для %s: из конфига PORTFOLIO_TAKE_PROFIT_PCT=%.1f%%, pnl=%.2f%%", ticker, take_pct, pnl_pct)
                if pnl_pct >= take_pct:
                    reason = (
                        f"Take-profit triggered: pnl={pnl_pct:.2f}% >= {take_pct}% "
                        f"(entry={entry_price:.2f}, current={current_price:.2f})"
                    )
                    position = Position(
                        ticker=ticker,
                        quantity=quantity,
                        entry_price=entry_price,
                        entry_ts=entry_ts,
                    )
                    self._execute_sell(ticker, position, reason, strategy_name)
                    continue
                logger.info(
                    "📈 Тейк‑профит для %s: порог=%.1f%%, pnl=%.2f%% — не достигнут",
                    ticker, take_pct, pnl_pct,
                )
            else:
                logger.info(
                    "📈 Тейк‑профит для %s не задан (задайте PORTFOLIO_TAKE_PROFIT_PCT в config.env, напр. 3.0)",
                    ticker,
                )
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
