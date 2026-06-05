#!/usr/bin/env python3
"""
Разовая вставка новости в knowledge_base по ссылке и тексту.
Использование:
  python scripts/add_manual_news.py "Заголовок или краткое содержание" "https://..." [тикер]
  Тикер по умолчанию — первый из TICKERS_FAST (config.env).
"""

import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from config_loader import get_database_url
from services.ticker_groups import get_tickers_fast


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    content = sys.argv[1].strip()
    link = sys.argv[2].strip()
    if len(sys.argv) > 3 and sys.argv[3].strip():
        ticker = sys.argv[3].strip().upper()
    else:
        fast = get_tickers_fast()
        ticker = (fast[0] if fast else "SNDK")
    source = "Investing.com"
    event_type = "NEWS"
    importance = "HIGH"
    # Короткий негативный контекст для шорта
    sentiment_score = 0.35
    insight = "Citron Research short call — негативный катализатор для цены."

    engine = create_engine(get_database_url())
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight, event_type, importance, link)
                VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight, :event_type, :importance, :link)
            """),
            {
                "ts": datetime.now(),
                "ticker": ticker,
                "source": source,
                "content": content[:8000],
                "sentiment_score": sentiment_score,
                "insight": insight,
                "event_type": event_type,
                "importance": importance,
                "link": link[:2000],
            },
        )
        row = conn.execute(text("SELECT LASTVAL()")).fetchone()
        kb_id = row[0] if row else None
    print(f"✅ Новость добавлена в knowledge_base, id={kb_id}, ticker={ticker}, link={link[:60]}...")


if __name__ == "__main__":
    main()
