import pandas as pd
from sqlalchemy import create_engine, text
import numpy as np
import re
from pathlib import Path
from datetime import datetime, timedelta
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config():
    """Загружает конфигурацию из ../brats/config.env"""
    config_path = Path(__file__).parent.parent / "brats" / "config.env"
    
    if not config_path.exists():
        raise FileNotFoundError(f"Конфигурационный файл не найден: {config_path}")
    
    config = {}
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
    
    # Извлекаем параметры из DATABASE_URL
    db_url = config.get('DATABASE_URL', 'postgresql://postgres:1234@localhost:5432/brats')
    
    # Парсим DATABASE_URL: postgresql://user:password@host:port/database
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', db_url)
    if match:
        user, password, host, port, _ = match.groups()
        # Используем базу данных lse_trading
        db_url_lse = f"postgresql://{user}:{password}@{host}:{port}/lse_trading"
        return db_url_lse
    else:
        raise ValueError(f"Неверный формат DATABASE_URL: {db_url}")


class AnalystAgent:
    """Агент для анализа торговых сигналов на основе технических индикаторов и базы знаний"""
    
    def __init__(self):
        """Инициализация подключения к базе данных"""
        self.db_url = load_config()
        self.engine = create_engine(self.db_url)
        logger.info("✅ AnalystAgent инициализирован, подключение к БД установлено")
    
    def get_last_5_days_quotes(self, ticker: str) -> pd.DataFrame:
        """Выгружает последние 5 дней котировок для указанного тикера"""
        logger.info(f"📊 Загрузка последних 5 дней котировок для {ticker}")
        
        with self.engine.connect() as conn:
            query = text("""
                SELECT date, ticker, close, volume, sma_5, volatility_5
                FROM quotes
                WHERE ticker = :ticker
                ORDER BY date DESC
                LIMIT 5
            """)
            df = pd.read_sql(query, conn, params={"ticker": ticker})
        
        if df.empty:
            logger.warning(f"⚠️  Нет данных для тикера {ticker}")
            return df
        
        logger.info(f"✅ Загружено {len(df)} записей для {ticker}")
        return df
    
    def get_average_volatility_20_days(self, ticker: str) -> float:
        """Вычисляет среднюю волатильность за последние 20 дней"""
        logger.info(f"📈 Расчет средней волатильности за 20 дней для {ticker}")
        
        with self.engine.connect() as conn:
            query = text("""
                SELECT AVG(volatility_5) as avg_volatility
                FROM (
                    SELECT volatility_5
                    FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 20
                ) as last_20
            """)
            result = conn.execute(query, {"ticker": ticker})
            row = result.fetchone()
        
        if row and row[0] is not None:
            avg_vol = float(row[0])
            logger.info(f"✅ Средняя волатильность за 20 дней: {avg_vol:.4f}")
            return avg_vol
        else:
            logger.warning(f"⚠️  Не удалось вычислить среднюю волатильность для {ticker}")
            return 0.0
    
    def check_technical_signal(self, ticker: str) -> str:
        """Проверяет технический сигнал: close > sma_5 и volatility_5 < средняя волатильность за 20 дней"""
        logger.info(f"🔍 Проверка технического сигнала для {ticker}")
        
        df = self.get_last_5_days_quotes(ticker)
        if df.empty:
            logger.warning(f"⚠️  Нет данных для анализа технического сигнала")
            return "NO_DATA"
        
        # Берем последнюю запись
        latest = df.iloc[0]
        close = float(latest['close'])
        sma_5 = float(latest['sma_5'])
        volatility_5 = float(latest['volatility_5'])
        
        avg_volatility_20 = self.get_average_volatility_20_days(ticker)
        
        logger.info(f"📊 Параметры последней котировки:")
        logger.info(f"   Close: {close:.2f}")
        logger.info(f"   SMA_5: {sma_5:.2f}")
        logger.info(f"   Volatility_5: {volatility_5:.4f}")
        logger.info(f"   Avg Volatility 20: {avg_volatility_20:.4f}")
        
        # Проверка условий
        condition1 = close > sma_5
        condition2 = volatility_5 < avg_volatility_20 if avg_volatility_20 > 0 else False
        
        logger.info(f"🔍 Условия технического сигнала:")
        logger.info(f"   Close > SMA_5: {condition1} ({close:.2f} > {sma_5:.2f})")
        logger.info(f"   Volatility_5 < Avg_Vol_20: {condition2} ({volatility_5:.4f} < {avg_volatility_20:.4f})")
        
        if condition1 and condition2:
            signal = "BUY"
            logger.info(f"✅ Технический сигнал: {signal}")
        else:
            signal = "HOLD"
            logger.info(f"⚠️  Технический сигнал: {signal}")
        
        return signal
    
    def get_recent_news(self, ticker: str, hours: int = None) -> pd.DataFrame:
        """
        Получает новости для тикера с учетом временного лага в зависимости от типа события.
        Для макро-событий (MACRO/US_MACRO) использует 72 часа (3 дня), для обычных новостей - 24 часа.
        """
        # Определяем временной лаг в зависимости от типа события
        # Сначала загружаем все новости за последние 3 дня, затем определим тип
        if hours is None:
            # Используем максимальный период для макро-событий
            hours = 72
        
        logger.info(f"📰 Поиск новостей за последние {hours} часов для {ticker} или MACRO/US_MACRO")
        
        cutoff_time = datetime.now() - timedelta(hours=hours)
        
        with self.engine.connect() as conn:
            # Ищем в knowledge_base (там есть sentiment_score)
            query = text("""
                SELECT id, ts, ticker, source, content, sentiment_score
                FROM knowledge_base
                WHERE (ticker = :ticker OR ticker = 'MACRO' OR ticker = 'US_MACRO')
                  AND ts >= :cutoff_time
                ORDER BY ts DESC
            """)
            df = pd.read_sql(query, conn, params={
                "ticker": ticker,
                "cutoff_time": cutoff_time
            })
        
        if df.empty:
            logger.info(f"ℹ️  Новостей за последние {hours} часов не найдено")
        else:
            logger.info(f"✅ Найдено {len(df)} новостей")
            # Определяем тип событий и применяем фильтрацию по времени
            macro_news = df[df['ticker'].isin(['MACRO', 'US_MACRO'])]
            ticker_news = df[df['ticker'] == ticker]
            
            # Для макро-событий используем 72 часа, для обычных - 24 часа
            macro_cutoff = datetime.now() - timedelta(hours=72)
            ticker_cutoff = datetime.now() - timedelta(hours=24)
            
            macro_filtered = macro_news[macro_news['ts'] >= macro_cutoff] if not macro_news.empty else pd.DataFrame()
            ticker_filtered = ticker_news[ticker_news['ts'] >= ticker_cutoff] if not ticker_news.empty else pd.DataFrame()
            
            # Объединяем отфильтрованные результаты
            df = pd.concat([macro_filtered, ticker_filtered]).drop_duplicates(subset=['id']).reset_index(drop=True)
            
            logger.info(f"   После фильтрации по типу события: {len(df)} новостей")
            logger.info(f"   - Макро-новости (72ч): {len(macro_filtered)}")
            logger.info(f"   - Новости тикера (24ч): {len(ticker_filtered)}")
            
            for idx, row in df.iterrows():
                event_type = "MACRO" if row['ticker'] in ['MACRO', 'US_MACRO'] else "TICKER"
                logger.info(f"   [{row['ts']}] {event_type} ({row['ticker']}): {row['content'][:50]}... (sentiment: {row['sentiment_score']})")
        
        return df
    
    def calculate_weighted_sentiment(self, news_df: pd.DataFrame, ticker: str) -> float:
        """
        Вычисляет взвешенный sentiment score.
        Новости с упоминанием конкретного тикера получают больший вес (weight=2.0),
        макро-новости получают стандартный вес (weight=1.0).
        """
        if news_df.empty:
            return 0.0
        
        # Проверяем, упоминается ли тикер в контенте
        def calculate_weight(row):
            ticker_in_content = ticker.upper() in str(row['content']).upper()
            is_ticker_news = row['ticker'] == ticker
            
            if is_ticker_news or ticker_in_content:
                # Новости с упоминанием тикера получают больший вес
                return 2.0
            else:
                # Макро-новости получают стандартный вес
                return 1.0
        
        # Добавляем веса к новостям
        news_df = news_df.copy()
        news_df['weight'] = news_df.apply(calculate_weight, axis=1)
        
        # Вычисляем взвешенный средний sentiment
        weighted_sum = (news_df['sentiment_score'] * news_df['weight']).sum()
        total_weight = news_df['weight'].sum()
        weighted_sentiment = weighted_sum / total_weight if total_weight > 0 else 0.0
        
        logger.info(f"📊 Взвешенный sentiment анализ:")
        logger.info(f"   Всего новостей: {len(news_df)}")
        logger.info(f"   Новостей с упоминанием тикера (weight=2.0): {len(news_df[news_df['weight'] == 2.0])}")
        logger.info(f"   Макро-новостей (weight=1.0): {len(news_df[news_df['weight'] == 1.0])}")
        
        for idx, row in news_df.iterrows():
            ticker_mentioned = ticker.upper() in str(row['content']).upper() or row['ticker'] == ticker
            logger.info(f"   [{row['ts']}] Weight={row['weight']:.1f}, Sentiment={row['sentiment_score']:.2f}, "
                       f"Ticker mentioned: {ticker_mentioned}")
        
        logger.info(f"   Взвешенный средний sentiment: {weighted_sentiment:.3f}")
        
        return weighted_sentiment
    
    def get_decision(self, ticker: str) -> str:
        """Основной метод принятия решения на основе технического анализа и базы знаний"""
        logger.info(f"=" * 60)
        logger.info(f"🎯 Анализ для тикера: {ticker}")
        logger.info(f"=" * 60)
        
        # Шаг 1: Проверка технического сигнала
        logger.info("\n📊 ШАГ 1: Анализ технических индикаторов")
        technical_signal = self.check_technical_signal(ticker)
        
        if technical_signal == "NO_DATA":
            logger.warning("⚠️  Недостаточно данных для принятия решения")
            return "NO_DATA"
        
        # Шаг 2: Проверка новостей и sentiment с учетом временного лага и весов
        logger.info("\n📰 ШАГ 2: Анализ новостей и sentiment (с учетом временного лага и весов)")
        news_df = self.get_recent_news(ticker)  # Использует автоматический выбор времени
        
        # Вычисляем взвешенный sentiment
        sentiment_positive = False
        weighted_sentiment = 0.0
        
        if not news_df.empty:
            # Используем взвешенный sentiment (новости с упоминанием тикера имеют больший вес)
            weighted_sentiment = self.calculate_weighted_sentiment(news_df, ticker)
            
            # Также показываем простые метрики для сравнения
            avg_sentiment = news_df['sentiment_score'].mean()
            max_sentiment = news_df['sentiment_score'].max()
            
            logger.info(f"📊 Сравнение метрик sentiment:")
            logger.info(f"   Простой средний sentiment: {avg_sentiment:.3f}")
            logger.info(f"   Максимальный sentiment: {max_sentiment:.3f}")
            logger.info(f"   Взвешенный sentiment: {weighted_sentiment:.3f}")
            
            # Используем взвешенный sentiment для принятия решения
            sentiment_positive = weighted_sentiment > 0.5
            logger.info(f"   Взвешенный sentiment > 0.5: {sentiment_positive}")
        else:
            logger.info("ℹ️  Новостей не найдено, sentiment анализ пропущен")
        
        # Шаг 3: Финальное решение
        logger.info("\n🎯 ШАГ 3: Принятие финального решения")
        
        if technical_signal == "BUY" and sentiment_positive:
            decision = "STRONG_BUY"
            logger.info(f"✅ РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал BUY + Положительный sentiment новостей")
        elif technical_signal == "BUY":
            decision = "BUY"
            logger.info(f"✅ РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал BUY, но sentiment нейтральный или отсутствует")
        else:
            decision = "HOLD"
            logger.info(f"⚠️  РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал не BUY")
        
        logger.info(f"=" * 60)
        return decision


if __name__ == "__main__":
    # Пример использования
    agent = AnalystAgent()
    
    # Тестируем на разных тикерах
    test_tickers = ["MSFT", "SNDK"]
    
    for ticker in test_tickers:
        decision = agent.get_decision(ticker)
        print(f"\n🎯 Финальное решение для {ticker}: {decision}\n")

