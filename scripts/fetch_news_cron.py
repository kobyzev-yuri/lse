#!/usr/bin/env python3
"""
Cron скрипт для автоматического получения новостей из всех источников
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import time
import argparse
from datetime import datetime

# Импорты модулей парсинга
from config_loader import get_config_value
from services.rss_news_fetcher import fetch_and_save_rss_news
from services.investing_calendar_parser import fetch_and_save_investing_calendar
from services.alphavantage_fetcher import fetch_all_alphavantage_data
from services.newsapi_fetcher import fetch_and_save_newsapi_news

# Настройка логирования (если /app/logs смонтирован :ro — пишем только в stderr)
log_dir = project_root / 'logs'
handlers_list = [logging.StreamHandler()]
try:
    log_dir.mkdir(exist_ok=True)
    handlers_list.insert(0, logging.FileHandler(log_dir / 'news_fetch.log'))
except OSError:
    pass  # read-only FS — только StreamHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=handlers_list
)

logger = logging.getLogger(__name__)


def fetch_all_news_sources(mode: str = "all"):
    """
    Получает новости из всех настроенных источников
    """
    logger.info("=" * 60)
    logger.info(f"🚀 Начало получения новостей - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    mode = (mode or "all").strip().lower()
    if mode not in ("all", "core", "core-fast", "newsapi", "investing"):
        mode = "all"

    sources_status = {}
    rss_saved, rss_skipped = 0, 0
    newsapi_saved = 0
    n_investing = 0

    run_investing = mode in ("all", "investing")
    run_core_fast = mode in ("all", "core", "core-fast")
    run_newsapi = mode in ("all", "core", "newsapi")

    if run_core_fast:
        # 1. RSS фиды центральных банков (всегда работает, бесплатно)
        try:
            logger.info("\n📡 Источник core-fast 1/2: RSS фиды центральных банков")
            rss_saved, rss_skipped = fetch_and_save_rss_news()
            if rss_saved or rss_skipped:
                if rss_saved == 0 and rss_skipped > 0:
                    sources_status['RSS'] = (
                        f"✅ фидов обработано записей: {rss_skipped}, новых 0 (все link уже в knowledge_base)"
                    )
                else:
                    sources_status['RSS'] = f"✅ сохранено {rss_saved} новых, дубликатов {rss_skipped}"
            else:
                sources_status['RSS'] = "✅ 0 записей из фидов"
        except Exception as e:
            logger.error("❌ Ошибка RSS фидов: %s", e)
            sources_status['RSS'] = f'❌ Ошибка: {e}'

        # 2. Alpha Vantage (требует API ключ)
        try:
            logger.info("\n📊 Источник core-fast 2/2: Alpha Vantage API")
            # Получаем тикеры из конфига или используем дефолтные (get_config_value — импорт на уровне модуля)
            tickers_str = get_config_value('EARNINGS_TRACK_TICKERS', 'MSFT,SNDK,MU,LITE,ALAB,TER')
            tickers = [t.strip() for t in tickers_str.split(',')]

            # По умолчанию выключено: бесплатный план Alpha Vantage — 25 запросов/день и 1 запрос/сек;
            # экономические и технические индикаторы быстро сжигают лимит. Включите в config.env при необходимости.
            include_economic = get_config_value('ALPHAVANTAGE_FETCH_ECONOMIC', 'false').lower() == 'true'
            include_technical = get_config_value('ALPHAVANTAGE_FETCH_TECHNICAL', 'false').lower() == 'true'

            fetch_all_alphavantage_data(
                tickers=tickers,
                include_economic=include_economic,
                include_technical=include_technical
            )
            sources_status['Alpha Vantage'] = '✅ Успешно'
        except Exception as e:
            logger.error(f"❌ Ошибка Alpha Vantage: {e}")
            sources_status['Alpha Vantage'] = f'❌ Ошибка: {e}'

    if run_newsapi:
        # NewsAPI (отдельный режим, чтобы не тормозить core-fast при 429 backoff)
        try:
            logger.info("\n📰 Источник newsapi 1/1: NewsAPI")
            newsapi_saved = fetch_and_save_newsapi_news()
            if newsapi_saved is None:
                newsapi_saved = 0
            sources_status['NewsAPI'] = f"✅ сохранено {newsapi_saved} новых" if newsapi_saved else "✅ 0 новых (ключ не задан или все дубликаты)"
        except Exception as e:
            logger.error("❌ Ошибка NewsAPI: %s", e)
            sources_status['NewsAPI'] = f'❌ Ошибка: {e}'

    if run_investing:
        # 4. Investing.com Economic Calendar (web scraping)
        try:
            logger.info("\n📅 Источник investing 1/2: Investing.com Economic Calendar")
            fetch_and_save_investing_calendar()
            sources_status['Investing.com Calendar'] = '✅ Успешно'
        except Exception as e:
            logger.error(f"❌ Ошибка Investing.com Calendar: {e}")
            sources_status['Investing.com Calendar'] = f'❌ Ошибка: {e}'
        time.sleep(10)  # пауза между источниками, склонными к 429

        # 5. Investing.com News (лента stock-market-news, по тикерам из ключевых слов)
        try:
            logger.info("\n📰 Источник investing 2/2: Investing.com News")
            from services.investing_news_fetcher import fetch_and_save_investing_news
            try:
                max_inv = int((get_config_value("INVESTING_NEWS_MAX_ARTICLES", "40") or "40").strip())
            except (ValueError, TypeError):
                max_inv = 40
            max_inv = max(10, min(max_inv, 120))
            n_investing = fetch_and_save_investing_news(max_articles=max_inv) or 0
            sources_status['Investing.com News'] = f'✅ сохранено {n_investing} новых' if n_investing else '✅ 0 новых'
        except Exception as e:
            logger.error(f"❌ Ошибка Investing.com News: {e}")
            sources_status['Investing.com News'] = f'❌ Ошибка: {e}'

    # Итоговый отчет
    total_new = rss_saved + newsapi_saved + n_investing
    logger.info("\n" + "=" * 60)
    logger.info("📊 Итоговый статус источников:")
    for source, status in sources_status.items():
        logger.info("   %s: %s", source, status)
    logger.info("=" * 60)
    logger.info("📥 За этот запуск (mode=%s) всего сохранено новых записей: %s", mode, total_new)
    logger.info("✅ Завершено получение новостей - %s\n", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch news from sources")
    parser.add_argument(
        "--mode",
        choices=("all", "core", "core-fast", "newsapi", "investing"),
        default="all",
        help="all=все источники, core=RSS+AlphaVantage+NewsAPI, core-fast=RSS+AlphaVantage, newsapi=только NewsAPI, investing=только Investing",
    )
    args = parser.parse_args()
    try:
        fetch_all_news_sources(mode=args.mode)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка получения новостей: {e}")
        sys.exit(1)
