#!/usr/bin/env python3
"""
Сводка по влиянию новостей на тикер: сколько новостей, sentiment, исходы (outcome_json).
Использование: python scripts/check_news_impact.py [SNDK]
Без аргумента — тикер SNDK.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from config_loader import get_database_url


def main():
    ticker = (sys.argv[1].strip().upper() if len(sys.argv) >= 2 else "SNDK")
    days = 90
    engine = create_engine(get_database_url())

    with engine.connect() as conn:
        # Сводка
        since = (datetime.now() - timedelta(days=days)).date()
        r = conn.execute(
            text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(sentiment_score) AS with_sentiment,
                    COUNT(outcome_json) AS with_outcome
                FROM knowledge_base
                WHERE ticker = :ticker AND ts >= :since
            """),
            {"ticker": ticker, "since": since},
        ).fetchone()
        total, with_sentiment, with_outcome = r[0], r[1], r[2]

        print(f"\n--- Влияние новостей на {ticker} (за {days} дн.) ---")
        print(f"Всего записей: {total}")
        print(f"С sentiment_score: {with_sentiment}")
        print(f"С outcome_json (исход по цене): {with_outcome}")

        if with_outcome == 0:
            print("\nИсходы пока не посчитаны. Запустите:")
            print("  EVENT_OUTCOME_DAYS_AFTER=7 python scripts/analyze_event_outcomes_cron.py")
            print("(нужны котировки в quotes на дату события и через 7 дней.)")
        else:
            rows = conn.execute(
                text("""
                    SELECT ts, source, LEFT(content, 70) AS content_short,
                           sentiment_score,
                           outcome_json->>'outcome' AS outcome,
                           outcome_json->>'price_change_pct' AS change_pct
                    FROM knowledge_base
                    WHERE ticker = :ticker AND outcome_json IS NOT NULL
                    ORDER BY ts DESC
                    LIMIT 15
                """),
                {"ticker": ticker},
            ).fetchall()
            print("\nПоследние события с исходом:")
            for row in rows:
                ts, source, content_short, sent, outcome, change_pct = row
                ts_str = str(ts)[:10] if ts else "—"
                sent_str = f"{float(sent):.2f}" if sent is not None else "—"
                print(f"  {ts_str} | {outcome or '—'} | {change_pct or '—'}% | sent={sent_str} | {source or '—'}")
                print(f"    {(content_short or '')}...")

        # Несколько последних новостей без исхода (напоминание что можно запустить анализ)
        recent = conn.execute(
            text("""
                SELECT id, ts, LEFT(content, 60) AS content_short
                FROM knowledge_base
                WHERE ticker = :ticker AND outcome_json IS NULL AND event_type = 'NEWS'
                ORDER BY ts DESC
                LIMIT 3
            """),
            {"ticker": ticker},
        ).fetchall()
        if recent and with_outcome < total:
            print("\nПримеры новостей без исхода (будут обработаны analyze_event_outcomes_cron через 7+ дней):")
            for row in recent:
                print(f"  id={row[0]} {row[1]} | {row[2]}...")
        print()


if __name__ == "__main__":
    main()
