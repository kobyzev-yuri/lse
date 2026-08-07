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

# StreamHandler only — cron already redirects stdout/stderr to news_fetch.log;
# a FileHandler to the same path would duplicate every line.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
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
    if mode not in ("all", "core", "core-fast", "newsapi", "investing", "tickers", "sa", "sa_sheet"):
        mode = "all"

    sources_status = {}
    rss_saved, rss_skipped = 0, 0
    newsapi_saved = 0
    n_investing = 0
    ticker_news_saved = 0
    yfinance_earnings_saved = 0
    sa_news_saved = 0
    sa_sheet_saved = 0

    run_investing = mode in ("all", "investing")
    run_core_fast = mode in ("all", "core", "core-fast")
    run_newsapi = mode in ("all", "core", "newsapi")
    run_tickers = mode in ("all", "core", "tickers")
    run_sa = mode in ("all", "sa")
    run_sa_sheet = mode in ("all", "sa", "sa_sheet")

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

    if run_sa_sheet:
        try:
            logger.info("\n📋 Источник SA Sheet: Google Sheet → knowledge_base")
            from services.sa_sheet_feed import ingest_sheet_to_kb, sheet_enabled

            if not sheet_enabled():
                sources_status["SeekingAlphaSheet"] = "⏭ пропуск (NOTEBOOK_SA_SHEET_ENABLED off)"
            else:
                sheet_result = ingest_sheet_to_kb()
                sa_sheet_saved = int(sheet_result.get("kb_inserted") or 0)
                if sheet_result.get("skipped"):
                    sources_status["SeekingAlphaSheet"] = f"⏭ {sheet_result.get('reason')}"
                else:
                    sources_status["SeekingAlphaSheet"] = (
                        f"✅ KB +{sa_sheet_saved}, rows={sheet_result.get('rows', 0)}, "
                        f"items={sheet_result.get('items', 0)}"
                    )
        except Exception as e:
            logger.error("❌ Ошибка SA Google Sheet: %s", e)
            sources_status["SeekingAlphaSheet"] = f"❌ Ошибка: {e}"

    if run_sa:
        try:
            logger.info("\n📰 Источник SA: Seeking Alpha Finance → knowledge_base (tickers+sections)")
            from services.seeking_alpha_finance import rapidapi_key
            from services.sa_section_subscriptions import run_sa_ingest

            if not rapidapi_key():
                sources_status["SeekingAlphaFinance"] = "⚠️ нет SEEKING_ALPHA_RAPIDAPI_KEY / RAPIDAPI_KEY"
            else:
                result = run_sa_ingest(include_tickers=True, include_sections=True)
                tp = result.get("tickers") if isinstance(result.get("tickers"), dict) else {}
                sp = result.get("sections") if isinstance(result.get("sections"), dict) else {}
                sa_news_saved = int(result.get("kb_inserted_total") or 0)
                t_n = len(tp.get("tickers") or [])
                s_n = len(sp.get("requested_sections") or sp.get("groups") or {})
                err_n = len(tp.get("errors") or {})
                sources_status["SeekingAlphaFinance"] = (
                    f"✅ KB +{sa_news_saved}, tickers={t_n}, sections={s_n}, "
                    f"ticker_api={tp.get('api_items', 0)}, section_items={sp.get('item_count', 0)}, "
                    f"errors={err_n}"
                )
        except Exception as e:
            logger.error("❌ Ошибка Seeking Alpha Finance: %s", e)
            sources_status["SeekingAlphaFinance"] = f"❌ Ошибка: {e}"

    if run_investing:
        # 4a. Official macro calendar (FRED + FOMC) → KB — before Investing (may 403 on GCP)
        try:
            logger.info("\n📅 Источник investing 0/2: FRED + FOMC → knowledge_base")
            from services.official_macro_calendar_kb import fetch_and_save_official_macro_calendar

            n_off, n_off_saved = fetch_and_save_official_macro_calendar()
            if n_off == 0:
                sources_status["Official macro (FRED/FOMC)"] = (
                    "⚠️ 0 событий (нужен FRED_API_KEY для FRED; FOMC без ключа)"
                )
            else:
                sources_status["Official macro (FRED/FOMC)"] = (
                    f"✅ событий: {n_off}, новых строк в KB: {n_off_saved}"
                )
        except Exception as e:
            logger.error("❌ Ошибка Official macro calendar (FRED/FOMC): %s", e)
            sources_status["Official macro (FRED/FOMC)"] = f"❌ Ошибка: {e}"

        # 4b. Investing.com Economic Calendar (JSON API по умолчанию; legacy HTML — INVESTING_CALENDAR_USE_HTML)
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
        rss_saved
        + newsapi_saved
        + n_investing
        + ticker_news_saved
        + yfinance_earnings_saved
        + sa_news_saved
        + sa_sheet_saved
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
        choices=("all", "core", "core-fast", "newsapi", "investing", "tickers", "sa", "sa_sheet"),
        default="all",
        help="all=все (+SA/+Sheet), core=RSS+AV+NewsAPI+TickerNews, core-fast=RSS+AV, newsapi, investing, tickers=Yahoo+Marketaux, sa=Sheet+SA Finance→KB, sa_sheet=Google Sheet SA→KB",
    )
    args = parser.parse_args()
    try:
        fetch_all_news_sources(mode=args.mode)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка получения новостей: {e}")
        sys.exit(1)
