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
        
        # Таблица котировок (open, high, low, close — дневные свечи)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS quotes (
                id SERIAL PRIMARY KEY,
                date TIMESTAMP,
                ticker VARCHAR(10),
                open DECIMAL,
                high DECIMAL,
                low DECIMAL,
                close DECIMAL,
                volume BIGINT,
                sma_5 DECIMAL,
                volatility_5 DECIMAL,
                rsi DECIMAL(5,2),  -- RSI из Finviz (0-100)
                UNIQUE(date, ticker)
            );
        """))
        
        # Добавляем колонки open, high, low если таблица создана раньше (без них)
        for col_name, col_type in [('open', 'DECIMAL'), ('high', 'DECIMAL'), ('low', 'DECIMAL')]:
            result = conn.execute(text("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='quotes' AND column_name=:col_name
            """), {"col_name": col_name})
            if not result.fetchone():
                try:
                    conn.execute(text(f"ALTER TABLE quotes ADD COLUMN {col_name} {col_type}"))
                    print(f"✅ Колонка {col_name} добавлена в таблицу quotes")
                except Exception as e:
                    if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                        print(f"⚠️ Предупреждение при добавлении колонки {col_name}: {e}")
        
        # Добавляем колонки для технических индикаторов если таблица уже существует
        technical_columns = [
            ('rsi', 'DECIMAL(5,2)', 'RSI из Finviz/Alpha Vantage (0-100)'),
            ('macd', 'DECIMAL(10,4)', 'MACD'),
            ('macd_signal', 'DECIMAL(10,4)', 'MACD Signal'),
            ('macd_hist', 'DECIMAL(10,4)', 'MACD Histogram'),
            ('bbands_upper', 'DECIMAL(10,4)', 'Bollinger Bands Upper'),
            ('bbands_middle', 'DECIMAL(10,4)', 'Bollinger Bands Middle'),
            ('bbands_lower', 'DECIMAL(10,4)', 'Bollinger Bands Lower'),
            ('adx', 'DECIMAL(5,2)', 'ADX (Average Directional Index)'),
            ('stoch_k', 'DECIMAL(5,2)', 'Stochastic %K'),
            ('stoch_d', 'DECIMAL(5,2)', 'Stochastic %D')
        ]
        
        for col_name, col_type, col_comment in technical_columns:
            result = conn.execute(text("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='quotes' AND column_name=:col_name
            """), {"col_name": col_name})
            
            if not result.fetchone():
                try:
                    conn.execute(text(f"ALTER TABLE quotes ADD COLUMN {col_name} {col_type}"))
                    print(f"✅ Колонка {col_name} добавлена в таблицу quotes")
                except Exception as e:
                    # Игнорируем ошибку если колонка уже существует
                    if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                        print(f"⚠️ Предупреждение при добавлении колонки {col_name}: {e}")
        
        # Таблица базы знаний для новостей с sentiment анализом (включая embedding и outcome_json — одна таблица)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMP,
                ticker VARCHAR(10),
                source VARCHAR(100),
                content TEXT,
                sentiment_score DECIMAL(3,2),
                insight TEXT,
                event_type VARCHAR(50), -- 'NEWS', 'EARNINGS', 'ECONOMIC_INDICATOR'
                importance VARCHAR(10), -- 'HIGH', 'MEDIUM', 'LOW'
                link TEXT -- URL для новостей
            );
        """))
        
        # Добавляем колонки event_type, importance, link, insight если таблица уже существует
        kb_columns = [
            ('event_type', 'VARCHAR(50)', 'Тип события'),
            ('importance', 'VARCHAR(10)', 'Важность события'),
            ('link', 'TEXT', 'Ссылка на источник'),
            ('insight', 'TEXT', 'Краткий вывод по новости')
        ]
        
        for col_name, col_type, col_comment in kb_columns:
            result = conn.execute(text("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name='knowledge_base' AND column_name=:col_name
            """), {"col_name": col_name})
            
            if not result.fetchone():
                try:
                    conn.execute(text(f"ALTER TABLE knowledge_base ADD COLUMN {col_name} {col_type}"))
                    print(f"✅ Колонка {col_name} добавлена в таблицу knowledge_base")
                except Exception as e:
                    if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                        print(f"⚠️ Предупреждение при добавлении колонки {col_name}: {e}")
        
        # Векторный поиск и исходы событий — в той же таблице (одна сущность «новость/событие»)
        try:
            conn.execute(text("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS embedding vector(768)"))
            conn.execute(text("ALTER TABLE knowledge_base ADD COLUMN IF NOT EXISTS outcome_json JSONB"))
            print("✅ Колонки knowledge_base.embedding и outcome_json добавлены/проверены")
        except Exception as e:
            if 'already exists' not in str(e).lower() and 'duplicate' not in str(e).lower():
                print(f"⚠️ Предупреждение при добавлении embedding/outcome_json: {e}")
        
        # Индекс для векторного поиска по knowledge_base (ivfflat, только строки с embedding)
        try:
            count_result = conn.execute(text("SELECT COUNT(*) FROM knowledge_base WHERE embedding IS NOT NULL"))
            record_count = count_result.fetchone()[0]
            if record_count >= 10:
                conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS kb_embedding_idx
                    ON knowledge_base USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 100)
                    WHERE embedding IS NOT NULL
                """))
                print("✅ Индекс kb_embedding_idx создан")
            else:
                print(f"ℹ️ Индекс kb_embedding_idx будет создан при наличии ≥10 записей с embedding (сейчас {record_count})")
        except Exception as e:
            if 'already exists' not in str(e).lower() and 'does not exist' not in str(e).lower():
                print(f"⚠️ Предупреждение при создании kb_embedding_idx: {e}")
        
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
                strategy_name VARCHAR(50) -- Название стратегии (Momentum, Mean Reversion, GAME_5M)
            );
        """))
        # Миграция: добавить strategy_name если таблица создана по старой схеме
        try:
            conn.execute(text("ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS strategy_name VARCHAR(50);"))
        except Exception:
            pass
        # Миграция: таймзона метки ts (чтобы однозначно интерпретировать при отображении в ET)
        try:
            conn.execute(text("""
                ALTER TABLE trade_history
                ADD COLUMN IF NOT EXISTS ts_timezone VARCHAR(50) DEFAULT 'Europe/Moscow';
            """))
            conn.execute(text("""
                UPDATE trade_history SET ts_timezone = 'Europe/Moscow' WHERE ts_timezone IS NULL;
            """))
        except Exception:
            pass

        # Инициализируем стартовый капитал
        conn.execute(text("""
            INSERT INTO portfolio_state (ticker, quantity) 
            VALUES ('CASH', 100000) 
            ON CONFLICT (ticker) DO UPDATE SET quantity = 100000 WHERE portfolio_state.ticker = 'CASH';
        """))
    print("✅ База данных инициализирована")

# Тикер золота: XAUUSD=X не поддерживается Yahoo Finance → используем GC=F (Gold Futures)
DEFAULT_TICKERS = ["MSFT", "SNDK", "GBPUSD=X", "GC=F", "^VIX", "MU", "LITE", "ALAB", "TER", "AMD"]


def seed_data(tickers=None):
    if tickers is None:
        tickers = DEFAULT_TICKERS
    for ticker in tickers:
        print(f"Загрузка {ticker}...")
        df = yf.download(ticker, period="2y", interval="1d", progress=False)
        
        if df.empty or 'Close' not in df.columns:
            print(f"  ⚠️ {ticker} пропущен (нет данных в Yahoo Finance)")
            continue
        
        # Если MultiIndex колонки, упрощаем структуру
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        # Рассчитываем базовые метрики для "Аналитика"
        df['sma_5'] = df['Close'].rolling(window=5).mean()
        df['volatility_5'] = df['Close'].rolling(window=5).std()
        
        # Удаляем строки с NaN значениями (первые 5 строк, где нет SMA) до reset_index
        df = df.dropna(subset=['sma_5', 'volatility_5'])
        
        if df.empty:
            print(f"  ⚠️ {ticker} пропущен (недостаточно данных для SMA)")
            continue
        
        # Подготовка к вставке
        df = df.reset_index()
        
        # Оптимизированная вставка данных батчами
        with engine.begin() as conn:
            for _, row in df.iterrows():
                conn.execute(text("""
                    INSERT INTO quotes (date, ticker, open, high, low, close, volume, sma_5, volatility_5, rsi)
                    VALUES (:date, :ticker, :open, :high, :low, :close, :volume, :sma_5, :volatility_5, :rsi)
                    ON CONFLICT (date, ticker) DO NOTHING
                """), {
                    "date": row['Date'], "ticker": ticker,
                    "open": float(row['Open']) if pd.notna(row.get('Open')) else None,
                    "high": float(row['High']) if pd.notna(row.get('High')) else None,
                    "low": float(row['Low']) if pd.notna(row.get('Low')) else None,
                    "close": float(row['Close']),
                    "volume": int(row['Volume']) if pd.notna(row['Volume']) else None,
                    "sma_5": float(row['sma_5']) if pd.notna(row['sma_5']) else None,
                    "volatility_5": float(row['volatility_5']) if pd.notna(row['volatility_5']) else None,
                    "rsi": None  # RSI обновляется отдельно через update_finviz_data.py
                })
        print(f"  ✅ {ticker} загружен ({len(df)} записей)")
    print("✅ Данные загружены")

if __name__ == "__main__":
    init_db()
    seed_data()

