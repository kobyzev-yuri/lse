import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, text
import numpy as np
import re
from pathlib import Path

def load_config():
    """Загружает конфигурацию из локального config.env или ../brats/config.env"""
    from config_loader import load_config as load_config_base, get_database_url
    import re
    
    config = load_config_base()
    db_url_lse = get_database_url(config)
    
    # Парсим для получения параметров подключения
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', db_url_lse)
    if match:
        user, password, host, port, _ = match.groups()
        return db_url_lse, user, password, host, port
    else:
        raise ValueError(f"Неверный формат DATABASE_URL: {db_url_lse}")

def create_database_if_not_exists():
    """Создает базу данных lse_trading если её нет"""
    db_url_lse, user, password, host, port = load_config()
    
    # Подключаемся к базе postgres для создания новой базы
    admin_url = f"postgresql://{user}:{password}@{host}:{port}/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    
    with admin_engine.connect() as conn:
        # Проверяем существование базы данных
        result = conn.execute(text("""
            SELECT 1 FROM pg_database WHERE datname = 'lse_trading'
        """))
        
        if not result.fetchone():
            print("Создание базы данных lse_trading...")
            conn.execute(text("CREATE DATABASE lse_trading"))
            print("✅ База данных lse_trading создана")
        else:
            print("✅ База данных lse_trading уже существует")
    
    admin_engine.dispose()
    return db_url_lse

# Загружаем конфигурацию и создаем базу данных если нужно
DB_URL = create_database_if_not_exists()
engine = create_engine(DB_URL)

def init_db():
    with engine.begin() as conn:
        # Включаем расширение для векторов
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        
        # Таблица котировок
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS quotes (
                id SERIAL PRIMARY KEY,
                date TIMESTAMP,
                ticker VARCHAR(10),
                close DECIMAL,
                volume BIGINT,
                sma_5 DECIMAL,
                volatility_5 DECIMAL,
                UNIQUE(date, ticker)
            );
        """))
        
        # Таблица базы знаний (Knowledge Base) с векторными embeddings
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trade_kb (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP,
                ticker VARCHAR(10),
                event_type VARCHAR(50), -- 'NEWS', 'TRADE_SIGNAL'
                content TEXT,
                embedding vector(1536) -- Для OpenAI embeddings
            );
        """))
        
        # Таблица базы знаний для новостей с sentiment анализом
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP,
                ticker VARCHAR(10),
                source VARCHAR(100),
                content TEXT,
                sentiment_score DECIMAL(3,2),
                insight TEXT
            );
        """))
        
        # Хранение текущего портфеля
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS portfolio_state (
                id SERIAL PRIMARY KEY,
                ticker VARCHAR(20) UNIQUE, -- 'CASH' для баланса
                quantity DECIMAL DEFAULT 0,
                avg_entry_price DECIMAL DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        
        # История сделок
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ticker VARCHAR(20),
                side VARCHAR(10), -- 'BUY' или 'SELL'
                quantity DECIMAL,
                price DECIMAL,
                commission DECIMAL,
                signal_type VARCHAR(20), -- 'STRONG_BUY', 'STOP_LOSS' и т.д.
                total_value DECIMAL,
                sentiment_at_trade DECIMAL, -- Сохраняем sentiment для анализа ошибок
                strategy_name VARCHAR(50) -- Название стратегии (Momentum, Mean Reversion, Volatile Gap)
            );
        """))
        
        # Инициализируем стартовый капитал
        conn.execute(text("""
            INSERT INTO portfolio_state (ticker, quantity) 
            VALUES ('CASH', 100000) 
            ON CONFLICT (ticker) DO UPDATE SET quantity = 100000 WHERE portfolio_state.ticker = 'CASH';
        """))
    print("✅ База данных инициализирована")

def seed_data(tickers=["MSFT", "SNDK", "GBPUSD=X"]):
    for ticker in tickers:
        print(f"Загрузка {ticker}...")
        df = yf.download(ticker, period="2y", interval="1d")
        
        # Если MultiIndex колонки, упрощаем структуру
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        # Рассчитываем базовые метрики для "Аналитика"
        df['sma_5'] = df['Close'].rolling(window=5).mean()
        df['volatility_5'] = df['Close'].rolling(window=5).std()
        
        # Удаляем строки с NaN значениями (первые 5 строк, где нет SMA) до reset_index
        df = df.dropna(subset=['sma_5', 'volatility_5'])
        
        # Подготовка к вставке
        df = df.reset_index()
        
        # Оптимизированная вставка данных батчами
        with engine.begin() as conn:
            for _, row in df.iterrows():
                conn.execute(text("""
                    INSERT INTO quotes (date, ticker, close, volume, sma_5, volatility_5)
                    VALUES (:date, :ticker, :close, :volume, :sma_5, :volatility_5)
                    ON CONFLICT (date, ticker) DO NOTHING
                """), {
                    "date": row['Date'], "ticker": ticker, "close": float(row['Close']),
                    "volume": int(row['Volume']) if pd.notna(row['Volume']) else None,
                    "sma_5": float(row['sma_5']) if pd.notna(row['sma_5']) else None,
                    "volatility_5": float(row['volatility_5']) if pd.notna(row['volatility_5']) else None
                })
        print(f"  ✅ {ticker} загружен ({len(df)} записей)")
    print("✅ Данные загружены")

if __name__ == "__main__":
    init_db()
    seed_data()

