"""
Движок для бэктестинга на исторических данных
Позволяет симулировать торговлю на прошлых данных
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional
import pandas as pd
from sqlalchemy import create_engine, text

from analyst_agent import AnalystAgent
from execution_agent import ExecutionAgent
from config_loader import get_database_url

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Движок для бэктестинга торговых стратегий на исторических данных
    """
    
    def __init__(self, initial_cash: float = 100_000.0):
        """
        Args:
            initial_cash: Начальный капитал для бэктестинга
        """
        self.db_url = get_database_url()
        self.engine = create_engine(self.db_url)
        self.initial_cash = initial_cash
        self.current_date: Optional[datetime] = None
        
        logger.info(f"✅ BacktestEngine инициализирован (начальный капитал: ${initial_cash:,.2f})")
    
    def get_price_at_date(self, ticker: str, date: datetime) -> Optional[float]:
        """
        Получает цену закрытия для тикера на конкретную дату
        
        Args:
            ticker: Тикер инструмента
            date: Дата для получения цены
            
        Returns:
            Цена закрытия или None если данных нет
        """
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT close
                    FROM quotes
                    WHERE ticker = :ticker AND date <= :date
                    ORDER BY date DESC
                    LIMIT 1
                """),
                {"ticker": ticker, "date": date}
            ).fetchone()
            
            if result:
                return float(result[0])
            return None
    
    def get_available_dates(self, ticker: str, start_date: datetime, end_date: datetime) -> List[datetime]:
        """
        Получает список доступных дат для тикера в диапазоне
        
        Args:
            ticker: Тикер инструмента
            start_date: Начальная дата
            end_date: Конечная дата
            
        Returns:
            Список дат с данными
        """
        with self.engine.connect() as conn:
            result = conn.execute(
                text("""
                    SELECT DISTINCT date
                    FROM quotes
                    WHERE ticker = :ticker 
                      AND date >= :start_date 
                      AND date <= :end_date
                    ORDER BY date ASC
                """),
                {"ticker": ticker, "start_date": start_date, "end_date": end_date}
            )
            dates = [row[0] for row in result.fetchall()]
            return dates
    
    def reset_portfolio(self):
        """Сбрасывает портфель к начальному состоянию"""
        with self.engine.begin() as conn:
            # Очищаем все позиции кроме CASH
            conn.execute(text("DELETE FROM portfolio_state WHERE ticker != 'CASH'"))
            
            # Сбрасываем CASH к начальному капиталу
            conn.execute(
                text("""
                    UPDATE portfolio_state 
                    SET quantity = :cash, last_updated = CURRENT_TIMESTAMP
                    WHERE ticker = 'CASH'
                """),
                {"cash": self.initial_cash}
            )
            
            # Если CASH нет, создаем
            conn.execute(
                text("""
                    INSERT INTO portfolio_state (ticker, quantity, last_updated)
                    VALUES ('CASH', :cash, CURRENT_TIMESTAMP)
                    ON CONFLICT (ticker) DO NOTHING
                """),
                {"cash": self.initial_cash}
            )
        
        logger.info(f"✅ Портфель сброшен к начальному капиталу: ${self.initial_cash:,.2f}")
    
    def run_backtest(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        use_llm: bool = False,
        reset_before: bool = True
    ) -> dict:
        """
        Запускает бэктестинг на исторических данных
        
        Args:
            tickers: Список тикеров для тестирования
            start_date: Начальная дата бэктестинга
            end_date: Конечная дата бэктестинга
            use_llm: Использовать LLM анализ (по умолчанию False для скорости)
            reset_before: Сбросить портфель перед началом (по умолчанию True)
            
        Returns:
            Словарь с результатами бэктестинга
        """
        logger.info("=" * 60)
        logger.info("🚀 Запуск бэктестинга")
        logger.info(f"   Тикеры: {', '.join(tickers)}")
        logger.info(f"   Период: {start_date.date()} - {end_date.date()}")
        logger.info(f"   LLM: {'включен' if use_llm else 'выключен'}")
        logger.info("=" * 60)
        
        if reset_before:
            self.reset_portfolio()
        
        # Получаем все доступные даты для первого тикера (предполагаем, что даты совпадают)
        if not tickers:
            logger.error("❌ Не указаны тикеры для бэктестинга")
            return {}
        
        dates = self.get_available_dates(tickers[0], start_date, end_date)
        if not dates:
            logger.error(f"❌ Нет данных для тикера {tickers[0]} в указанном диапазоне")
            return {}
        
        logger.info(f"📅 Найдено {len(dates)} торговых дней")
        
        # Создаем агентов
        analyst = AnalystAgent(use_llm=use_llm, use_strategy_factory=True)
        executor = ExecutionAgent()
        
        # Модифицируем executor для работы с историческими данными
        original_get_price = executor._get_current_price
        
        def get_price_for_date(ticker: str) -> Optional[float]:
            """Временная функция для получения цены на текущую дату бэктестинга"""
            if self.current_date:
                return self.get_price_at_date(ticker, self.current_date)
            return original_get_price(ticker)
        
        # Заменяем метод получения цены
        executor._get_current_price = get_price_for_date
        
        # Модифицируем analyst для работы с историческими данными
        original_get_quotes = analyst.get_last_5_days_quotes
        original_get_volatility = analyst.get_average_volatility_20_days
        
        def get_quotes_for_date(ticker: str) -> pd.DataFrame:
            """Получает последние 5 дней котировок ДО текущей даты бэктестинга"""
            if self.current_date:
                with analyst.engine.connect() as conn:
                    query = text("""
                        SELECT date, ticker, close, volume, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker AND date <= :date
                        ORDER BY date DESC
                        LIMIT 5
                    """)
                    df = pd.read_sql(query, conn, params={"ticker": ticker, "date": self.current_date})
                    return df
            return original_get_quotes(ticker)
        
        def get_volatility_for_date(ticker: str) -> float:
            """Получает среднюю волатильность за 20 дней ДО текущей даты бэктестинга"""
            if self.current_date:
                with analyst.engine.connect() as conn:
                    query = text("""
                        SELECT AVG(volatility_5) as avg_volatility
                        FROM (
                            SELECT volatility_5
                            FROM quotes
                            WHERE ticker = :ticker AND date <= :date
                            ORDER BY date DESC
                            LIMIT 20
                        ) as last_20
                    """)
                    result = conn.execute(query, {"ticker": ticker, "date": self.current_date})
                    row = result.fetchone()
                    if row and row[0] is not None:
                        return float(row[0])
                    return 0.0
            return original_get_volatility(ticker)
        
        # Заменяем методы analyst
        analyst.get_last_5_days_quotes = get_quotes_for_date
        analyst.get_average_volatility_20_days = get_volatility_for_date
        
        trades_count = 0
        decisions_count = 0
        
        # Проходим по каждой дате
        for i, date in enumerate(dates):
            self.current_date = date
            
            # Пропускаем первые 5 дней (нужны для расчета SMA)
            if i < 5:
                continue
            
            logger.info(f"\n📅 Дата: {date.date()} ({i+1}/{len(dates)})")
            
            # Для каждого тикера получаем решение и исполняем
            for ticker in tickers:
                try:
                    # Получаем решение от AnalystAgent
                    if use_llm:
                        result = analyst.get_decision_with_llm(ticker)
                        decision = result.get('decision', 'HOLD')
                    else:
                        decision_result = analyst.get_decision(ticker)
                        if isinstance(decision_result, dict):
                            decision = decision_result.get('decision', 'HOLD')
                        else:
                            decision = decision_result
                    
                    decisions_count += 1
                    
                    # Исполняем решение
                    if decision in ("BUY", "STRONG_BUY"):
                        executor._execute_buy(
                            ticker, 
                            decision,
                            result.get('selected_strategy') if use_llm and isinstance(result, dict) else None
                        )
                        trades_count += 1
                    
                    # Проверяем стоп-лоссы
                    executor.check_stop_losses()
                    
                except Exception as e:
                    logger.warning(f"⚠️ Ошибка при обработке {ticker} на {date.date()}: {e}")
                    continue
        
        # Восстанавливаем оригинальные методы
        executor._get_current_price = original_get_price
        analyst.get_last_5_days_quotes = original_get_quotes
        analyst.get_average_volatility_20_days = original_get_volatility
        
        # Получаем финальные результаты
        results = self._calculate_backtest_results()
        results['trades_count'] = trades_count
        results['decisions_count'] = decisions_count
        results['dates_processed'] = len(dates)
        
        logger.info("\n" + "=" * 60)
        logger.info("✅ Бэктестинг завершен")
        logger.info(f"   Обработано дат: {len(dates)}")
        logger.info(f"   Принято решений: {decisions_count}")
        logger.info(f"   Выполнено сделок: {trades_count}")
        logger.info(f"   Финальный баланс: ${results.get('final_cash', 0):,.2f}")
        logger.info(f"   PnL: ${results.get('total_pnl', 0):,.2f} ({results.get('pnl_percent', 0):.2f}%)")
        logger.info("=" * 60)
        
        return results
    
    def _calculate_backtest_results(self) -> dict:
        """Рассчитывает результаты бэктестинга"""
        from report_generator import load_trade_history, compute_closed_trade_pnls
        
        # Получаем текущий баланс
        with self.engine.connect() as conn:
            cash_result = conn.execute(
                text("SELECT quantity FROM portfolio_state WHERE ticker = 'CASH'")
            ).fetchone()
            final_cash = float(cash_result[0]) if cash_result else self.initial_cash
            
            # Получаем стоимость открытых позиций
            positions_df = pd.read_sql(
                text("""
                    SELECT ticker, quantity, avg_entry_price
                    FROM portfolio_state
                    WHERE ticker != 'CASH' AND quantity > 0
                """),
                conn
            )
        
        # Рассчитываем стоимость открытых позиций по последней цене
        open_positions_value = 0.0
        if not positions_df.empty:
            for _, pos in positions_df.iterrows():
                ticker = pos['ticker']
                quantity = float(pos['quantity'])
                # Используем последнюю доступную цену
                price = self.get_price_at_date(ticker, self.current_date) if self.current_date else None
                if price:
                    open_positions_value += quantity * price
        
        # Загружаем историю сделок и рассчитываем PnL
        all_trades = load_trade_history(self.engine)
        trade_pnls = compute_closed_trade_pnls(all_trades)
        
        closed_pnl = sum(t.net_pnl for t in trade_pnls) if trade_pnls else 0.0
        win_rate = (sum(1 for t in trade_pnls if t.net_pnl > 0) / len(trade_pnls) * 100) if trade_pnls else 0.0
        
        total_value = final_cash + open_positions_value
        total_pnl = total_value - self.initial_cash
        pnl_percent = (total_pnl / self.initial_cash) * 100 if self.initial_cash > 0 else 0.0
        
        return {
            'initial_cash': self.initial_cash,
            'final_cash': final_cash,
            'open_positions_value': open_positions_value,
            'total_value': total_value,
            'total_pnl': total_pnl,
            'pnl_percent': pnl_percent,
            'closed_pnl': closed_pnl,
            'win_rate': win_rate,
            'closed_trades_count': len(trade_pnls)
        }


if __name__ == "__main__":
    # Пример использования
    from datetime import datetime, timedelta
    
    engine = BacktestEngine(initial_cash=100_000.0)
    
    # Бэктестинг за последние 3 месяца
    end_date = datetime.now()
    start_date = end_date - timedelta(days=90)
    
    results = engine.run_backtest(
        tickers=["MSFT", "SNDK"],
        start_date=start_date,
        end_date=end_date,
        use_llm=False,  # Отключаем LLM для скорости
        reset_before=True
    )
    
    print("\n📊 Результаты бэктестинга:")
    print(f"   Начальный капитал: ${results.get('initial_cash', 0):,.2f}")
    print(f"   Финальный баланс: ${results.get('final_cash', 0):,.2f}")
    print(f"   Стоимость открытых позиций: ${results.get('open_positions_value', 0):,.2f}")
    print(f"   Общая стоимость: ${results.get('total_value', 0):,.2f}")
    print(f"   PnL: ${results.get('total_pnl', 0):,.2f} ({results.get('pnl_percent', 0):.2f}%)")
    print(f"   Win Rate: {results.get('win_rate', 0):.2f}%")
    print(f"   Закрытых сделок: {results.get('closed_trades_count', 0)}")

