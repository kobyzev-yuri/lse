"""
Утилита для добавления новостей в базу знаний.
Поддерживает:
- Ручное добавление новостей через CLI
- Импорт из файла (CSV/JSON)
- Интеграцию с новостными API (в будущем)
"""

import pandas as pd
from sqlalchemy import create_engine, text
from datetime import datetime, timedelta
import logging
import json
import csv
import sys
from pathlib import Path

from config_loader import load_config, get_database_url, get_config_value

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def add_news(engine, ticker, source, content, sentiment_score=None, insight=None, ts=None):
    """
    Добавляет новость в базу знаний.
    
    Args:
        engine: SQLAlchemy engine
        ticker: Тикер инструмента или 'MACRO'/'US_MACRO' для макро-новостей
        source: Источник новости (например, 'BLS Release', 'Reuters', 'Bloomberg')
        content: Текст новости
        sentiment_score: Оценка настроения (0.0-1.0), если None - можно будет рассчитать позже
        insight: Ключевой финансовый факт из новости (например, "рост 163%")
        ts: Временная метка (если None - используется текущее время)
    """
    if ts is None:
        ts = datetime.now()
    
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight)
            VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight)
        """), {
            "ts": ts,
            "ticker": ticker,
            "source": source,
            "content": content,
            "sentiment_score": sentiment_score,
            "insight": insight
        })
    
    logger.info(f"✅ Новость добавлена: {ticker} от {source} (sentiment={sentiment_score}, insight={insight})")


def add_news_interactive():
    """Интерактивное добавление новости через CLI."""
    print("\n" + "="*60)
    print("📰 Добавление новости в базу знаний")
    print("="*60)
    
    ticker = input("Тикер (или MACRO/US_MACRO для макро-новостей): ").strip().upper()
    if not ticker:
        print("❌ Тикер обязателен")
        return
    
    source = input("Источник (например, BLS Release, Reuters): ").strip()
    if not source:
        source = "Manual Entry"
    
    print("\nВведите текст новости (завершите пустой строкой):")
    content_lines = []
    while True:
        line = input()
        if not line:
            break
        content_lines.append(line)
    
    content = "\n".join(content_lines)
    if not content:
        print("❌ Текст новости обязателен")
        return
    
    sentiment_input = input("Sentiment score (0.0-1.0, Enter для пропуска): ").strip()
    sentiment_score = None
    if sentiment_input:
        try:
            sentiment_score = float(sentiment_input)
            if not (0.0 <= sentiment_score <= 1.0):
                print("⚠️ Sentiment должен быть от 0.0 до 1.0, будет установлен None")
                sentiment_score = None
        except ValueError:
            print("⚠️ Неверный формат, sentiment будет None")
    
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    # Автоматический расчет sentiment и insight, если не указан и включен в конфиге
    insight = None
    if sentiment_score is None:
        auto_calculate = get_config_value('SENTIMENT_AUTO_CALCULATE', 'false').lower() == 'true'
        if auto_calculate:
            try:
                from services.sentiment_analyzer import calculate_sentiment
                sentiment_score, insight = calculate_sentiment(content)
                logger.info(f"✅ Sentiment рассчитан автоматически: {sentiment_score:.3f}")
                if insight:
                    logger.info(f"   Insight: {insight}")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось рассчитать sentiment автоматически: {e}")
    
    add_news(engine, ticker, source, content, sentiment_score, insight)
    engine.dispose()
    
    print("✅ Новость успешно добавлена!")


def import_from_csv(file_path):
    """Импортирует новости из CSV файла.
    
    Ожидаемые колонки: ticker, source, content, sentiment_score (опционально), ts (опционально)
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    # Проверяем, нужно ли автоматически рассчитывать sentiment
    auto_calculate = get_config_value('SENTIMENT_AUTO_CALCULATE', 'false').lower() == 'true'
    
    df = pd.read_csv(file_path)
    
    required_cols = ['ticker', 'source', 'content']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        logger.error(f"❌ Отсутствуют обязательные колонки: {missing_cols}")
        return
    
    imported = 0
    for _, row in df.iterrows():
        try:
            ticker = str(row['ticker']).upper()
            source = str(row['source'])
            content = str(row['content'])
            
            sentiment_score = None
            if 'sentiment_score' in df.columns and pd.notna(row['sentiment_score']):
                sentiment_score = float(row['sentiment_score'])
            elif auto_calculate:
                # Автоматический расчет sentiment
                try:
                    from services.sentiment_analyzer import calculate_sentiment
                    sentiment_score = calculate_sentiment(content)
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось рассчитать sentiment для строки {_ + 1}: {e}")
            
            ts = None
            if 'ts' in df.columns and pd.notna(row['ts']):
                try:
                    ts = pd.to_datetime(row['ts'])
                except:
                    pass
            
            add_news(engine, ticker, source, content, sentiment_score, ts)
            imported += 1
        except Exception as e:
            logger.error(f"❌ Ошибка при импорте строки {_ + 1}: {e}")
    
    logger.info(f"✅ Импортировано {imported} новостей из {file_path}")
    engine.dispose()


def import_from_json(file_path):
    """Импортирует новости из JSON файла.
    
    Ожидаемый формат: список объектов с полями ticker, source, content, sentiment_score (опционально), ts (опционально)
    """
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    # Проверяем, нужно ли автоматически рассчитывать sentiment
    auto_calculate = get_config_value('SENTIMENT_AUTO_CALCULATE', 'false').lower() == 'true'
    
    with open(file_path, 'r', encoding='utf-8') as f:
        news_list = json.load(f)
    
    imported = 0
    for news in news_list:
        try:
            ticker = str(news['ticker']).upper()
            source = str(news['source'])
            content = str(news['content'])
            
            sentiment_score = news.get('sentiment_score')
            if sentiment_score is not None:
                sentiment_score = float(sentiment_score)
            elif auto_calculate:
                # Автоматический расчет sentiment
                try:
                    from services.sentiment_analyzer import calculate_sentiment
                    sentiment_score = calculate_sentiment(content)
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось рассчитать sentiment: {e}")
            
            ts = None
            if 'ts' in news and news['ts']:
                try:
                    ts = pd.to_datetime(news['ts'])
                except:
                    pass
            
            add_news(engine, ticker, source, content, sentiment_score, ts)
            imported += 1
        except Exception as e:
            logger.error(f"❌ Ошибка при импорте новости: {e}")
    
    logger.info(f"✅ Импортировано {imported} новостей из {file_path}")
    engine.dispose()


def get_news_sources_stats(engine, days=14):
    """
    Возвращает список каналов (источников) новостей и количество записей за последние days дней.
    Returns: list of dicts [{"source": str, "count": int}, ...], sorted by count descending.
    """
    cutoff = datetime.now() - timedelta(days=int(days))
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT COALESCE(source, '—') AS source, COUNT(*) AS cnt
                FROM knowledge_base
                WHERE ts >= :cutoff
                GROUP BY source
                ORDER BY cnt DESC
            """),
            conn,
            params={"cutoff": cutoff},
        )
    if df.empty:
        return []
    return [{"source": row["source"], "count": int(row["cnt"])} for _, row in df.iterrows()]


def show_recent_news(limit=10):
    """Показывает последние новости из базы."""
    db_url = get_database_url()
    engine = create_engine(db_url)
    
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT ts, ticker, source, LEFT(content, 100) as content_preview, sentiment_score
            FROM knowledge_base
            ORDER BY ts DESC
            LIMIT :limit
        """), conn, params={"limit": limit})
    
    if df.empty:
        print("📰 Новостей в базе нет")
        return
    
    print(f"\n📰 Последние {len(df)} новостей:\n")
    for _, row in df.iterrows():
        print(f"[{row['ts']}] {row['ticker']} | {row['source']}")
        print(f"  {row['content_preview']}...")
        if pd.notna(row['sentiment_score']):
            print(f"  Sentiment: {row['sentiment_score']:.2f}")
        print()
    
    engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование:")
        print("  python news_importer.py add          - интерактивное добавление")
        print("  python news_importer.py import <file.csv> - импорт из CSV")
        print("  python news_importer.py import <file.json> - импорт из JSON")
        print("  python news_importer.py show [limit] - показать последние новости")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "add":
        add_news_interactive()
    elif command == "import":
        if len(sys.argv) < 3:
            print("❌ Укажите путь к файлу")
            sys.exit(1)
        file_path = sys.argv[2]
        if file_path.endswith('.csv'):
            import_from_csv(file_path)
        elif file_path.endswith('.json'):
            import_from_json(file_path)
        else:
            print("❌ Поддерживаются только CSV и JSON файлы")
    elif command == "show":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        show_recent_news(limit)
    else:
        print(f"❌ Неизвестная команда: {command}")


