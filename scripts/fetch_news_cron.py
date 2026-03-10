#!/usr/bin/env python3
"""
Cron скрипт для автоматического получения новостей из всех источников
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from datetime import datetime

# Импорты модулей парсинга
from services.rss_news_fetcher import fetch_and_save_rss_news
from services.investing_calendar_parser import fetch_and_save_investing_calendar
from services.alphavantage_fetcher import fetch_all_alphavantage_data
from services.newsapi_fetcher import fetch_and_save_newsapi_news

# Настройка логирования
log_dir = project_root / 'logs'
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / 'news_fetch.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


def fetch_all_news_sources():
    """
    Получает новости из всех настроенных источников
    """
    logger.info("=" * 60)
    logger.info(f"🚀 Начало получения новостей - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)
    
    sources_status = {}
    
    # 1. RSS фиды центральных банков (всегда работает, бесплатно)
    try:
        logger.info("\n📡 Источник 1/5: RSS фиды центральных банков")
        fetch_and_save_rss_news()
        sources_status['RSS'] = '✅ Успешно'
    except Exception as e:
        logger.error(f"❌ Ошибка RSS фидов: {e}")
        sources_status['RSS'] = f'❌ Ошибка: {e}'
    
    # 2. Investing.com Economic Calendar (web scraping)
    try:
        logger.info("\n📅 Источник 2/6: Investing.com Economic Calendar")
        fetch_and_save_investing_calendar()
        sources_status['Investing.com Calendar'] = '✅ Успешно'
    except Exception as e:
        logger.error(f"❌ Ошибка Investing.com Calendar: {e}")
        sources_status['Investing.com Calendar'] = f'❌ Ошибка: {e}'

    # 2b. Investing.com News (лента stock-market-news, по тикерам из ключевых слов)
    try:
        logger.info("\n📰 Источник 2b/6: Investing.com News")
        from services.investing_news_fetcher import fetch_and_save_investing_news
        n = fetch_and_save_investing_news(max_articles=25)
        sources_status['Investing.com News'] = f'✅ Успешно (добавлено {n})' if n else '✅ Нет новых'
    except Exception as e:
        logger.error(f"❌ Ошибка Investing.com News: {e}")
        sources_status['Investing.com News'] = f'❌ Ошибка: {e}'
    
    # 3. Alpha Vantage (требует API ключ)
    try:
        logger.info("\n📊 Источник 3/6: Alpha Vantage API")
        # Получаем тикеры из конфига или используем дефолтные
        from config_loader import get_config_value
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
    
    # 4. NewsAPI (требует API ключ)
    try:
        logger.info("\n📰 Источник 4/6: NewsAPI")
        fetch_and_save_newsapi_news()
        sources_status['NewsAPI'] = '✅ Успешно'
    except Exception as e:
        logger.error(f"❌ Ошибка NewsAPI: {e}")
        sources_status['NewsAPI'] = f'❌ Ошибка: {e}'

    # 5. LLM (GPT/Gemini и т.д.) — прямой запрос новостей по тикерам из LLM_NEWS_TICKERS (при USE_LLM_NEWS=true)
    try:
        logger.info("\n🤖 Источник 5/6: LLM (новости по тикеру)")
        from services.llm_news_fetcher import fetch_and_save_llm_news
        from services.ticker_groups import get_tickers_fast
        fast = get_tickers_fast()
        default_llm = (fast[0] if fast else "SNDK")
        llm_tickers = get_config_value("LLM_NEWS_TICKERS", default_llm).strip()
        for t in [x.strip() for x in llm_tickers.split(",") if x.strip()]:
            nid, skip_reason = fetch_and_save_llm_news(t)
            if nid is not None:
                sources_status[f'LLM({t})'] = '✅ Успешно'
            else:
                sources_status[f'LLM({t})'] = f'⏭️ Пропущено ({skip_reason or "ошибка"})'
        has_llm_ticker = any(k.startswith('LLM(') for k in sources_status)
        if not has_llm_ticker and 'LLM' not in str(sources_status):
            sources_status['LLM'] = '⏭️ Пропущено (USE_LLM_NEWS не включён или нет ключа)'
    except Exception as e:
        logger.error(f"❌ Ошибка LLM-новостей: {e}")
        sources_status['LLM'] = f'❌ Ошибка: {e}'
    
    # Итоговый отчет
    logger.info("\n" + "=" * 60)
    logger.info("📊 Итоговый статус источников:")
    for source, status in sources_status.items():
        logger.info(f"   {source}: {status}")
    logger.info("=" * 60)
    logger.info(f"✅ Завершено получение новостей - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")


if __name__ == "__main__":
    try:
        fetch_all_news_sources()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка получения новостей: {e}")
        sys.exit(1)
