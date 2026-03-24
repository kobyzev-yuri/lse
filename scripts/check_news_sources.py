#!/usr/bin/env python3
"""
Диагностика поступления новостей: проверка БД, один прогон RSS, наличие ключей.
Запуск: python scripts/check_news_sources.py (или docker exec lse-bot python scripts/check_news_sources.py)
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from datetime import datetime

# Загружаем конфиг
from config_loader import get_database_url, get_config_value
from sqlalchemy import create_engine, text


def main():
    print("=" * 60)
    print("Проверка поступления новостей (knowledge_base)")
    print("=" * 60)

    # 1. База данных
    db_url = get_database_url()
    if not db_url:
        print("❌ DATABASE_URL не задан (config.env не загружен или пустой).")
        return 1
    print("✅ DATABASE_URL задан")

    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            r = conn.execute(text("SELECT COUNT(*) FROM knowledge_base")).scalar()
            last = conn.execute(text("SELECT MAX(ts) FROM knowledge_base")).scalar()
            cb_rss = conn.execute(
                text(
                    "SELECT COUNT(*) FROM knowledge_base WHERE source LIKE '%Central Bank%'"
                )
            ).scalar()
        engine.dispose()
        print(f"   Записей в knowledge_base: {r}")
        print(f"   Из них RSS центробанков (source …Central Bank…): {cb_rss}")
        print(f"   Последняя запись (ts):   {last or '—'}")
    except Exception as e:
        print(f"❌ Ошибка доступа к БД: {e}")
        return 1

    # 1b. Проверка записи в БД (важно на сервере: БД в контейнере или удалённая)
    print("   Тест записи в knowledge_base (INSERT + ROLLBACK)...")
    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                marker = f"LSE_CHECK_WRITE_TEST_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                conn.execute(
                    text("""
                        INSERT INTO knowledge_base (ts, ticker, source, content, event_type, link)
                        VALUES (:ts, 'LSE_TEST', 'check_news_sources', :content, 'TEST', '')
                    """),
                    {"ts": datetime.utcnow(), "content": marker},
                )
                trans.rollback()
            except Exception:
                trans.rollback()
                raise
        engine.dispose()
        print("   ✅ Запись в БД возможна (транзакция откатана, тестовая строка не сохранена)")
    except Exception as e:
        print(f"   ❌ Ошибка при записи в БД: {e}")
        print("   На сервере проверьте: контейнер видит DATABASE_URL, сеть до PostgreSQL, права на INSERT.")
        try:
            engine.dispose()
        except Exception:
            pass
        return 1

    # 2. Ключи источников
    newsapi_key = get_config_value("NEWSAPI_KEY", "")
    print(f"\n📰 NewsAPI: ключ {'задан' if newsapi_key else 'не задан'} (в config.env)")
    try:
        from services.newsapi_fetcher import newsapi_cooldown_active, newsapi_cooldown_until, _cooldown_file
        if newsapi_cooldown_active():
            print(f"   ⏳ Cooldown после 429 активен до {newsapi_cooldown_until()} — запросы пропускаются. Сброс: rm {_cooldown_file()}")
        else:
            print("   Cooldown после 429: не активен")
    except Exception as e:
        print(f"   (проверка cooldown: {e})")
    av_key = get_config_value("ALPHAVANTAGE_KEY", "") or get_config_value("ALPHAVANTAGE_API_KEY", "")
    print(f"📊 Alpha Vantage: ключ {'задан' if av_key else 'не задан'}")

    # 3. Один прогон RSS (без ключей)
    print("\n📡 Запуск RSS фидов (один раз)...")
    try:
        from services.rss_news_fetcher import fetch_and_save_rss_news
        saved, skipped = fetch_and_save_rss_news()
        print(f"   RSS: сохранено новых {saved}, пропущено (link уже в БД): {skipped}")
        if saved == 0 and skipped > 0:
            print(
                "   ℹ️ Это нормально: дедупликация по полю link. Новые статьи появятся, когда в фидах будут URL, которых ещё нет в knowledge_base."
            )
        if saved == 0 and skipped == 0:
            print("   ⚠️ Из фидов получено 0 записей — проверьте сеть или доступность URL фидов (Fed, BoE, ECB, BoJ).")
    except Exception as e:
        print(f"   ❌ Ошибка RSS: {e}")

    # 4. Рекомендации
    print("\n" + "=" * 60)
    print("Чтобы новости поступали регулярно:")
    print("  1. Cron: */15 * * * * ... fetch_news_cron.py (каждые 15 мин)")
    print("  2. Лог: tail -f logs/news_fetch.log — в конце каждого запуска строка «За этот запуск всего сохранено новых: N»")
    print("  3. RSS «0 новых» при пропуске N>0 — не сбой: статьи уже в БД. Если и получено 0 из фидов — сеть/URL.")
    print("  4. Дополнительно: NEWSAPI, Investing — см. логи; Docker: доступ наружу.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
