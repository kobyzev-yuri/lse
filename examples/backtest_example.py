"""
Пример использования бэктестинга на исторических данных
"""

import pandas as pd
from datetime import datetime, timedelta
from backtest_engine import BacktestEngine

def main():
    """Пример бэктестинга за последние 6 месяцев"""
    
    # Создаем движок бэктестинга
    engine = BacktestEngine(initial_cash=100_000.0)
    
    # Определяем период бэктестинга
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)  # 6 месяцев
    
    print("=" * 60)
    print("🚀 Запуск бэктестинга на исторических данных")
    print(f"   Период: {start_date.date()} - {end_date.date()}")
    print(f"   Тикеры: MSFT, SNDK")
    print("=" * 60)
    
    # Запускаем бэктестинг
    results = engine.run_backtest(
        tickers=["MSFT", "SNDK"],
        start_date=start_date,
        end_date=end_date,
        use_llm=False,  # Отключаем LLM для скорости (можно включить для более точного анализа)
        reset_before=True
    )
    
    # Выводим результаты
    print("\n" + "=" * 60)
    print("📊 РЕЗУЛЬТАТЫ БЭКТЕСТИНГА")
    print("=" * 60)
    print(f"Начальный капитал:     ${results.get('initial_cash', 0):>12,.2f}")
    print(f"Финальный баланс:      ${results.get('final_cash', 0):>12,.2f}")
    print(f"Открытые позиции:      ${results.get('open_positions_value', 0):>12,.2f}")
    print(f"Общая стоимость:       ${results.get('total_value', 0):>12,.2f}")
    print("-" * 60)
    print(f"Общий PnL:             ${results.get('total_pnl', 0):>12,.2f}")
    print(f"PnL в процентах:       {results.get('pnl_percent', 0):>12.2f}%")
    print(f"Закрытый PnL:          ${results.get('closed_pnl', 0):>12,.2f}")
    print(f"Win Rate:              {results.get('win_rate', 0):>12.2f}%")
    print("-" * 60)
    print(f"Обработано дат:         {results.get('dates_processed', 0):>12}")
    print(f"Принято решений:       {results.get('decisions_count', 0):>12}")
    print(f"Выполнено сделок:      {results.get('trades_count', 0):>12}")
    print(f"Закрытых сделок:       {results.get('closed_trades_count', 0):>12}")
    print("=" * 60)
    
    # Анализ эффективности стратегий
    print("\n📋 Анализ эффективности стратегий:")
    from sqlalchemy import create_engine, text
    from config_loader import get_database_url
    
    db_url = get_database_url()
    engine_db = create_engine(db_url)
    
    with engine_db.connect() as conn:
        strategy_stats = pd.read_sql(
            text("""
                SELECT 
                    strategy_name,
                    COUNT(*) as trade_count,
                    SUM(CASE WHEN side = 'BUY' THEN -total_value ELSE total_value END) as net_pnl,
                    AVG(sentiment_at_trade) as avg_sentiment
                FROM trade_history
                WHERE strategy_name IS NOT NULL
                GROUP BY strategy_name
                ORDER BY net_pnl DESC
            """),
            conn
        )
        
        if not strategy_stats.empty:
            print(strategy_stats.to_string(index=False))
        else:
            print("   Нет данных по стратегиям")


if __name__ == "__main__":
    import pandas as pd
    main()

