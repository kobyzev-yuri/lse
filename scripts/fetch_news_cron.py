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
from services.ticker_news_merge_fetcher import fetch_and_save_ticker_news

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
    if mode not in ("all", "core", "core-fast", "newsapi", "investing", "tickers", "sa"):
        mode = "all"

    sources_status = {}
    rss_saved, rss_skipped = 0, 0
    newsapi_saved = 0
    n_investing = 0
    ticker_news_saved = 0
    yfinance_earnings_saved = 0
    sa_news_saved = 0

    run_investing = mode in ("all", "investing")
    run_core_fast = mode in ("all", "core", "core-fast")
    run_newsapi = mode in ("all", "core", "newsapi")
    run_tickers = mode in ("all", "core", "tickers")
    run_sa = mode in ("all", "sa")

    if run_core_fast:
        # 1. RSS фиды центральных банков (всегда работает, бесплатно)
        try:
            logger.info("\n📡 Источник core-fast 1/3: RSS фиды центральных банков")
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
            logger.info("\n📊 Источник core-fast 2/3: Alpha Vantage API")
            # Получаем тикеры из конфига или используем дефолтные (get_config_value — импорт на уровне модуля)
            from services.earnings_intelligence_universe import get_earnings_calendar_tickers

            tickers = get_earnings_calendar_tickers()

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

        # 3. Yahoo earnings calendar (yfinance, без ключа)
        try:
            raw_yfe = (get_config_value("YFINANCE_EARNINGS_CALENDAR_SAVE", "true") or "true").strip().lower()
            if raw_yfe in ("1", "true", "yes"):
                logger.info("\n📅 Источник core-fast 3/3: Yahoo earnings (yfinance)")
                from services.yfinance_earnings_fetcher import fetch_and_save_yfinance_earnings

                yfinance_earnings_saved = int(fetch_and_save_yfinance_earnings() or 0)
                sources_status["Yahoo Earnings (yfinance)"] = (
                    f"✅ новых в KB: {yfinance_earnings_saved}" if yfinance_earnings_saved else "✅ 0 новых"
                )
            else:
                sources_status["Yahoo Earnings (yfinance)"] = "⏭ пропуск (YFINANCE_EARNINGS_CALENDAR_SAVE не true)"
        except Exception as e:
            logger.error("❌ Ошибка Yahoo earnings (yfinance): %s", e)
            sources_status["Yahoo Earnings (yfinance)"] = f"❌ Ошибка: {e}"

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

    if run_tickers:
        try:
            logger.info("\n🗞️ Источник tickers 1/1: Yahoo + Marketaux merge")
            ticker_news_saved = fetch_and_save_ticker_news() or 0
            sources_status["TickerNews"] = (
                f"✅ сохранено {ticker_news_saved} новых" if ticker_news_saved else "✅ 0 новых"
            )
        except Exception as e:
            logger.error("❌ Ошибка ticker news: %s", e)
            sources_status["TickerNews"] = f"❌ Ошибка: {e}"

    if run_sa:
        try:
            logger.info("\n📰 Источник SA: Seeking Alpha Finance → knowledge_base")
            from services.notebook_news_digest import build_sa_fetch_tickers
            from services.seeking_alpha_finance import fetch_and_save_sa_news, rapidapi_key

            if not rapidapi_key():
                sources_status["SeekingAlphaFinance"] = "⚠️ нет SEEKING_ALPHA_RAPIDAPI_KEY / RAPIDAPI_KEY"
            else:
                uni = build_sa_fetch_tickers(equity_only=True)
                tickers = list(uni.get("sa_fetch_tickers") or uni.get("group3_union") or [])
                extra_n = len(uni.get("sa_extra") or [])
                logger.info(
                    "SA universe: %s tickers (notebook+%s extras)",
                    len(tickers),
                    extra_n,
                )
                try:
                    per = int((get_config_value("NOTEBOOK_NEWS_PER_TICKER", "5") or "5").strip())
                except (ValueError, TypeError):
                    per = 5
                try:
                    sleep = float((get_config_value("NOTEBOOK_NEWS_SLEEP_SEC", "0.35") or "0.35").strip())
                except (ValueError, TypeError):
                    sleep = 0.35
                raw_mx = (get_config_value("NOTEBOOK_NEWS_MAX_TICKERS", "") or "").strip()
                max_t = int(raw_mx) if raw_mx.isdigit() else None
                bundle = fetch_and_save_sa_news(
                    tickers,
                    per_ticker=per,
                    sleep_sec=sleep,
                    max_tickers=max_t,
                )
                sa_news_saved = int(bundle.get("kb_inserted") or 0)
                err_n = len(bundle.get("errors") or {})
                sources_status["SeekingAlphaFinance"] = (
                    f"✅ KB +{sa_news_saved}, api_items={len(bundle.get('items') or [])}, "
                    f"tickers={len(tickers)} (+{extra_n} extra), errors={err_n}"
                )
        except Exception as e:
            logger.error("❌ Ошибка Seeking Alpha Finance: %s", e)
            sources_status["SeekingAlphaFinance"] = f"❌ Ошибка: {e}"

    if run_investing:
        # 4. Investing.com Economic Calendar (JSON API по умолчанию; legacy HTML — INVESTING_CALENDAR_USE_HTML)
        try:
            logger.info("\n📅 Источник investing 1/2: Investing.com Economic Calendar")
            n_ev, n_saved = fetch_and_save_investing_calendar()
            if n_ev == 0:
                sources_status["Investing.com Calendar"] = (
                    "⚠️ 0 событий (проверьте JSON API/сеть; при 403 на HTML — отключите "
                    "INVESTING_CALENDAR_USE_HTML или прокси INVESTING_CALENDAR_USE_SYSTEM_PROXY)"
                )
            else:
                sources_status["Investing.com Calendar"] = (
                    f"✅ событий в выборке: {n_ev}, новых строк в KB: {n_saved}"
                )
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
    total_new = (
        rss_saved + newsapi_saved + n_investing + ticker_news_saved + yfinance_earnings_saved + sa_news_saved
    )
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
        choices=("all", "core", "core-fast", "newsapi", "investing", "tickers", "sa"),
        default="all",
        help="all=все (+SA если ключ), core=RSS+AV+NewsAPI+TickerNews, core-fast=RSS+AV, newsapi, investing, tickers=Yahoo+Marketaux, sa=Seeking Alpha Finance→KB",
    )
    args = parser.parse_args()
    try:
        fetch_all_news_sources(mode=args.mode)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка получения новостей: {e}")
        sys.exit(1)
