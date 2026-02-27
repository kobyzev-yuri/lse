"""
Telegram Bot для LSE Trading System
Основной класс бота для работы с независимыми инструментами (золото, валютные пары)
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import logging
import math
import re
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

from analyst_agent import AnalystAgent
from services.vector_kb import VectorKB
from config_loader import get_config_value

logger = logging.getLogger(__name__)


def _escape_markdown(text: str) -> str:
    """Экранирует символы, ломающие Telegram Markdown (* _ [ ] `)."""
    if not text:
        return ""
    s = str(text)
    for c in ("\\", "_", "*", "[", "]", "`"):
        s = s.replace(c, "\\" + c)
    return s


def _normalize_ticker(ticker: str) -> str:
    """
    Нормализует тикер: исправляет распространённые ошибки (GC-F -> GC=F, GBPUSD-X -> GBPUSD=X).
    """
    if not ticker:
        return ticker
    ticker = ticker.upper().strip()
    # Исправляем дефис на = для фьючерсов и валют
    if ticker.endswith("-F") or ticker.endswith("-X"):
        ticker = ticker[:-2] + "=" + ticker[-1]
    # Исправляем дефис в середине для валютных пар (GBP-USD -> GBPUSD=X)
    if "-" in ticker and len(ticker) >= 6:
        parts = ticker.split("-")
        if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3:
            ticker = parts[0] + parts[1] + "=X"
    return ticker


class LSETelegramBot:
    """
    Telegram Bot для LSE Trading System
    
    Фокус на независимых инструментах:
    - Золото (GC=F)
    - Валютные пары (GBPUSD=X, EURUSD=X и т.д.)
    - Отдельные акции (MSFT, SNDK и т.д.)
    """
    
    def __init__(self, token: str, allowed_users: Optional[list] = None):
        """
        Инициализация бота
        
        Args:
            token: Telegram Bot Token
            allowed_users: Список разрешенных user_id (если None - доступ для всех)
        """
        self.token = token
        self.allowed_users = allowed_users
        
        # Инициализация компонентов
        # LLM отключена для обычного анализа, используется только для команды /ask
        self.analyst = AnalystAgent(use_llm=False, use_strategy_factory=True)
        self.vector_kb = VectorKB()
        
        # Инициализация LLM только для обработки вопросов в /ask
        try:
            from services.llm_service import get_llm_service
            self.llm_service = get_llm_service()
            logger.info("✅ LLM сервис инициализирован для обработки вопросов (/ask)")
        except Exception as e:
            logger.warning(f"⚠️ LLM сервис недоступен для вопросов: {e}")
            self.llm_service = None
        
        # Создаем приложение (увеличенные таймауты — при медленной сети отправка графика/фото иначе даёт TimedOut)
        builder = (
            Application.builder()
            .token(token)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .connect_timeout(15.0)
        )
        try:
            builder.media_write_timeout(60.0)  # отправка фото (chart5m и т.д.)
        except AttributeError:
            pass  # старые версии PTB без media_write_timeout
        self.application = builder.build()
        
        # Получаем информацию о боте для логирования
        async def get_bot_info():
            bot_info = await self.application.bot.get_me()
            logger.info(f"Bot info: username={bot_info.username}, id={bot_info.id}, first_name={bot_info.first_name}")
            return bot_info
        
        # Регистрируем handlers
        self._register_handlers()
        
        logger.info("✅ LSE Telegram Bot инициализирован")
        
        # Логируем информацию о боте после инициализации
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Если loop уже запущен, создаём задачу
                loop.create_task(get_bot_info())
            else:
                # Если loop не запущен, запускаем
                loop.run_until_complete(get_bot_info())
        except Exception as e:
            logger.warning(f"Не удалось получить информацию о боте: {e}")
    
    def _register_handlers(self):
        """Регистрация обработчиков команд и сообщений"""
        # Команды
        self.application.add_handler(CommandHandler("start", self._handle_start))
        self.application.add_handler(CommandHandler("help", self._handle_help))
        self.application.add_handler(CommandHandler("signal", self._handle_signal))
        self.application.add_handler(CommandHandler("news", self._handle_news))
        self.application.add_handler(CommandHandler("price", self._handle_price))
        self.application.add_handler(CommandHandler("chart", self._handle_chart))
        self.application.add_handler(CommandHandler("chart5m", self._handle_chart5m))
        self.application.add_handler(CommandHandler("table5m", self._handle_table5m))
        self.application.add_handler(CommandHandler("tickers", self._handle_tickers))
        self.application.add_handler(CommandHandler("ask", self._handle_ask))
        self.application.add_handler(CommandHandler("portfolio", self._handle_portfolio))
        self.application.add_handler(CommandHandler("buy", self._handle_buy))
        self.application.add_handler(CommandHandler("sell", self._handle_sell))
        self.application.add_handler(CommandHandler("history", self._handle_history))
        self.application.add_handler(CommandHandler("recommend", self._handle_recommend))
        self.application.add_handler(CommandHandler("recommend5m", self._handle_recommend5m))
        self.application.add_handler(CommandHandler("game5m", self._handle_game5m))
        self.application.add_handler(CommandHandler("dashboard", self._handle_dashboard))
        
        # Обработка текстовых сообщений (для произвольных запросов)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        
        # Обработка callback queries (для inline кнопок)
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
    
    def _check_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя"""
        if self.allowed_users is None:
            return True
        return user_id in self.allowed_users

    async def _reply_to_update(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        parse_mode: str | None = "Markdown",
    ) -> None:
        """Отправляет ответ в чат: через message.reply_text или bot.send_message, если message отсутствует."""
        if update.message is not None:
            await update.message.reply_text(text, parse_mode=parse_mode)
            return
        if update.effective_chat is not None and context.bot is not None:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode=parse_mode,
            )
            return
        logger.warning("Не удалось отправить ответ: нет update.message и effective_chat")

    async def _get_recent_news_async(self, ticker: str, timeout: int = 30):
        """
        Получает новости для тикера в executor с таймаутом.
        Не блокирует event loop. При таймауте выбрасывает asyncio.TimeoutError.
        """
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, self.analyst.get_recent_news, ticker),
            timeout=timeout,
        )
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /start"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        welcome_text = """
🤖 **LSE Trading Bot**

Анализ и виртуальная торговля (песочница):
• Золото (GC=F), валюты (GBPUSD=X), акции (MSFT, SNDK)

**Команды:**
/signal <ticker> — анализ
/news <ticker> [N] — новости
/price <ticker> — цена
/chart <ticker> [days] — график дневной
/chart5m <ticker> [days] — график 5 мин (по требованию)
/table5m <ticker> [days] — таблица 5m свечей
/recommend5m [ticker] [days] — рекомендация по 5m + 5д статистике (по умолч. SNDK, 5 дн.)
/game5m [ticker] — мониторинг игры 5m: позиция, сделки, win rate и PnL (по умолч. SNDK)
/dashboard [5m|daily|all] — дашборд по тикерам: решения, 5m, новости (проактивный мониторинг)
/ask <вопрос> — вопрос (работает в группах!)
/tickers — список инструментов

**Песочница (вход/выход, P&L):**
/portfolio — портфель и P&L
/buy <ticker> <кол-во> — купить
/sell <ticker> [кол-во] — продать (без кол-ва — вся позиция)
/history [тикер] [N] — последние сделки (с тикером — фильтр по тикеру)
/recommend [ticker] — рекомендация: когда открыть позицию и параметры управления

/help — справка
        """
        
        await update.message.reply_text(welcome_text, parse_mode='Markdown')
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /help"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        help_text = """
📖 **Справка по командам**

**Анализ сигналов:**
`/signal` — справка и список доступных тикеров
`/signal <ticker>` — полный анализ (решение, цена, RSI, sentiment)
  Пример: `/signal MSFT` или `/signal GC=F`
  Показывает: цену, технический анализ, sentiment, рекомендацию

**Новости:**
`/news <ticker> [N]` - Новости за последние 7 дней (топ N, по умолч. 10)
  Пример: `/news MSFT` или `/news MSFT 15`
  Показывает: последние новости с источником и sentiment

**Цена:**
`/price <ticker>` - Текущая цена инструмента
  Пример: `/price MSFT`

**График:**
`/chart <ticker> [days]` - График цены за период (по умолч. 1 день, макс. 30)
  Пример: `/chart GC=F` или `/chart GC=F 7`
`/chart5m <ticker> [days]` - Внутридневной график 5 мин (по требованию, макс. 7 дней)
`/table5m <ticker> [days]` - Таблица последних 5-минутных свечей (макс. 7 дней)

**Список инструментов:**
`/tickers` - Показать все отслеживаемые инструменты

**Произвольные вопросы:**
`/ask <вопрос>` - Задать вопрос боту (работает в группах!)

**Примеры вопросов:**
• `/ask какая цена золота`
• `/ask какие новости по MSFT`
• `/ask анализ GBPUSD`
• `/ask сколько стоит золото`
• `/ask что с фунтом`

**Песочница (виртуальная торговля):**
`/portfolio` — кэш, позиции и P&L по последним ценам
`/buy <ticker> <кол-во>` — купить по последней цене из БД
`/sell <ticker>` — закрыть всю позицию; `/sell <ticker> <кол-во>` — частичная продажа
`/history [тикер] [N]` — последние сделки (по умолч. 15); с тикером — только по нему. В ответе — стратегия [GAME_5M / Portfolio / Manual]
`/recommend <ticker>` — рекомендация: когда открыть позицию, стоп-лосс, размер позиции
`/recommend5m [ticker] [days]` — рекомендация по 5m и 5д статистике (интрадей, по умолч. SNDK 5д)
`/game5m [ticker]` — мониторинг игры 5m: открытая позиция, последние сделки, win rate и PnL (по умолч. SNDK)
`/dashboard [5m|daily|all]` — дашборд: все тикеры, сигналы, 5m (SNDK), новости за 7 дн. Для смены курса и решений.
  В /ask можно спросить: _когда можно открыть позицию по SNDK и какие параметры советуешь?_
  Пример: `/recommend SNDK`, `/buy GC=F 5`, `/sell MSFT`
        """
        
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    def _get_available_tickers(self) -> list:
        """Возвращает список тикеров из БД для справки по /signal и /tickers."""
        try:
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
                )
                return [row[0] for row in result]
        except Exception as e:
            logger.warning(f"Не удалось загрузить тикеры из БД: {e}")
            return []

    async def _handle_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /signal [ticker]. Без аргумента — справка и список тикеров."""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        # Без аргумента — показываем справку и доступные тикеры
        if not context.args or len(context.args) == 0:
            tickers = self._get_available_tickers()
            help_msg = (
                "📌 **Как пользоваться /signal**\n\n"
                "Команда даёт анализ по инструменту: решение (BUY/HOLD/SELL), цену, RSI, "
                "технический сигнал, sentiment новостей и выбранную стратегию.\n\n"
                "**Формат:**\n"
                "`/signal` — эта справка и список тикеров\n"
                "`/signal <тикер>` — анализ по выбранному инструменту\n\n"
                "**Примеры:**\n"
                "`/signal MSFT`\n"
                "`/signal GC=F`\n"
                "`/signal GBPUSD=X`\n\n"
                "**Как выбирается стратегия:**\n"
                "По волатильности и sentiment: Momentum (тренд), Mean Reversion (откат), Volatile Gap (гэпы). "
                "Если ни одна не подошла — **Neutral** (режим не определён, рекомендация удержание).\n\n"
            )
            if tickers:
                commodities = [t for t in tickers if "=" in t or str(t).startswith("GC")]
                currencies = [t for t in tickers if "USD" in str(t) or "EUR" in str(t) or "GBP" in str(t)]
                stocks = [t for t in tickers if t not in commodities and t not in currencies]
                help_msg += "**Доступные тикеры:**\n"
                if stocks:
                    help_msg += "Акции: " + ", ".join(f"`{t}`" for t in stocks[:20]) + "\n"
                if currencies:
                    help_msg += "Валюты: " + ", ".join(f"`{t}`" for t in currencies[:15]) + "\n"
                if commodities:
                    help_msg += "Товары: " + ", ".join(f"`{t}`" for t in commodities[:10]) + "\n"
                if len(tickers) > 45:
                    help_msg += f"\n_Всего {len(tickers)} инструментов. Полный список: /tickers_"
            else:
                help_msg += "_Список тикеров пуст (нет данных в БД)._"
            await update.message.reply_text(help_msg, parse_mode="Markdown")
            return
        
        # Извлекаем тикер: если первый аргумент не похож на тикер (служебные слова), ищем дальше
        ticker = None
        if context.args:
            first_arg = context.args[0].upper()
            # Служебные слова, которые не тикеры
            skip_words = {'ДЛЯ', 'ПО', 'АНАЛИЗ', 'АНАЛИЗА', 'ПОКАЖИ', 'ДАЙ', 'THE', 'FOR', 'SHOW', 'GET'}
            if first_arg not in skip_words and len(first_arg) >= 2:
                ticker = first_arg
            else:
                # Пробуем найти тикер в остальных аргументах или извлекаем из всего текста
                if len(context.args) > 1:
                    ticker = context.args[1].upper()
                else:
                    # Извлекаем тикер из полного текста сообщения
                    full_text = update.message.text or ""
                    ticker = self._extract_ticker_from_text(full_text)
                    if not ticker:
                        ticker = first_arg  # Fallback на первый аргумент
        
        if not ticker:
            await update.message.reply_text(
                "❌ Не указан тикер\n"
                "Пример: `/signal GBPUSD=X` или `/signal GC=F`",
                parse_mode='Markdown'
            )
            return
        
        # Нормализуем тикер (GC-F -> GC=F и т.д.)
        ticker = _normalize_ticker(ticker)
        
        logger.info(f"📊 Запрос /signal для {ticker} от пользователя {update.effective_user.id} (исходные args: {context.args})")
        
        try:
            # Показываем, что анализ начат
            await update.message.reply_text(f"🔍 Анализ {ticker}...")
            
            # Получаем решение от AnalystAgent
            logger.info(f"Вызов analyst.get_decision_with_llm({ticker})")
            decision_result = self.analyst.get_decision_with_llm(ticker)
            logger.info(f"Получен результат для {ticker}: decision={decision_result.get('decision')}")
            
            # Форматируем ответ
            logger.info(f"Форматирование ответа для {ticker}")
            response = self._format_signal_response(ticker, decision_result)
            logger.info(f"Ответ сформирован для {ticker}, длина: {len(response)} символов")
            
            # Пытаемся отправить с Markdown, при ошибке парсинга — без форматирования
            try:
                logger.info(f"Отправка ответа для {ticker} с Markdown")
                await update.message.reply_text(response, parse_mode='Markdown')
                logger.info(f"✅ Ответ для {ticker} успешно отправлен")
            except Exception as parse_err:
                if 'parse' in str(parse_err).lower() or 'entit' in str(parse_err).lower():
                    logger.warning(f"Ошибка парсинга Markdown для {ticker}, отправляем без форматирования: {parse_err}")
                    await update.message.reply_text(response)
                    logger.info(f"✅ Ответ для {ticker} отправлен без форматирования")
                else:
                    logger.error(f"Ошибка отправки для {ticker}: {parse_err}", exc_info=True)
                    raise
            
        except Exception as e:
            logger.error(f"Ошибка анализа сигнала для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Ошибка анализа {ticker}: {str(e)}"
            )
    
    async def _handle_news(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /news <ticker>"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        # Извлекаем ticker и опциональный лимит: /news MSFT  или  /news MSFT 15
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите тикер\n"
                "Пример: `/news GC=F` или `/news MSFT 15` (число — сколько новостей показать, по умолчанию 10)",
                parse_mode='Markdown'
            )
            return
        
        ticker_raw = context.args[0].upper()
        ticker = _normalize_ticker(ticker_raw)
        limit = 10
        if len(context.args) >= 2:
            try:
                n = int(context.args[1])
                limit = max(1, min(50, n))
            except ValueError:
                pass
        
        try:
            await update.message.reply_text(f"📰 Поиск новостей для {ticker}...")
            
            news_timeout = 30
            try:
                news_df = await self._get_recent_news_async(ticker, timeout=news_timeout)
            except asyncio.TimeoutError:
                logger.error(f"Таймаут получения новостей для {ticker} ({news_timeout} с)")
                await update.message.reply_text(
                    f"❌ Получение новостей для {ticker} заняло больше {news_timeout} с. "
                    "Попробуйте позже или проверьте доступность БД."
                )
                return
            
            if news_df.empty:
                await update.message.reply_text(
                    f"ℹ️ Новостей для {ticker} не найдено за последние 7 дней"
                )
                return
            
            # Форматируем новости (top N по умолчанию 10)
            response = self._format_news_response(ticker, news_df, top_n=limit)
            
            async def _send_news_part(text: str):
                try:
                    await update.message.reply_text(text, parse_mode='Markdown')
                except Exception as parse_err:
                    if 'parse' in str(parse_err).lower() or 'entit' in str(parse_err).lower():
                        await update.message.reply_text(text)
                    else:
                        raise
            
            # Telegram имеет лимит 4096 символов на сообщение
            if len(response) > 4000:
                parts = self._split_long_message(response, max_length=4000)
                for part in parts:
                    await _send_news_part(part)
            else:
                await _send_news_part(response)
            
        except Exception as e:
            err_type = type(e).__name__
            err_msg = str(e)
            logger.error(
                f"Ошибка получения новостей для {ticker}: [{err_type}] {err_msg}",
                exc_info=True,
            )
            if "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
                reply = (
                    f"❌ Запрос новостей для {ticker} завершился по таймауту. "
                    "Возможны перегрузка БД или медленный запрос к knowledge_base. Попробуйте позже."
                )
            else:
                reply = f"❌ Ошибка получения новостей для {ticker}: {err_msg}"
            await update.message.reply_text(reply)
    
    async def _handle_price_by_ticker(self, update: Update, ticker: str, ticker_raw: str = None):
        """Вспомогательная функция для получения цены по тикеру"""
        if ticker_raw is None:
            ticker_raw = ticker
        try:
            # Получаем последнюю цену из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text("""
                        SELECT date, close, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker
                        ORDER BY date DESC
                        LIMIT 1
                    """),
                    {"ticker": ticker}
                )
                row = result.fetchone()
            
            if not row:
                # Пробуем найти похожий тикер в БД
                # Ищем по базовому символу (GC, GBPUSD и т.д.)
                base_symbol = ticker.replace('=', '').replace('-', '').replace('X', '').replace('F', '')
                with engine.connect() as conn:
                    similar = conn.execute(
                        text("""
                            SELECT DISTINCT ticker FROM quotes
                            WHERE ticker LIKE :pattern1 OR ticker LIKE :pattern2
                            ORDER BY ticker
                            LIMIT 5
                        """),
                        {
                            "pattern1": f"{base_symbol}%",
                            "pattern2": f"%{base_symbol}%"
                        }
                    ).fetchall()
                if similar:
                    suggestions = ", ".join([f"`{s[0]}`" for s in similar])
                    await update.message.reply_text(
                        f"❌ Нет данных для `{ticker_raw}`\n\n"
                        f"Возможно, вы имели в виду: {suggestions}",
                        parse_mode='Markdown'
                    )
                else:
                    await update.message.reply_text(
                        f"❌ Нет данных для `{ticker_raw}`\n"
                        f"Проверьте тикер или запустите `update_prices.py {ticker}`",
                        parse_mode='Markdown'
                    )
                return
            
            date, close, sma_5, vol_5, rsi = row
            
            # Форматируем значения с проверкой на None
            date_str = date.strftime('%Y-%m-%d') if date else 'N/A'
            close_str = f"${close:.2f}" if close is not None else "N/A"
            sma_str = f"${sma_5:.2f}" if sma_5 is not None else "N/A"
            vol_str = f"{vol_5:.2f}%" if vol_5 is not None else "N/A"
            
            # Форматируем RSI
            rsi_text = ""
            if rsi is not None:
                if rsi >= 70:
                    rsi_emoji = "🔴"
                    rsi_status = "перекупленность"
                elif rsi <= 30:
                    rsi_emoji = "🟢"
                    rsi_status = "перепроданность"
                elif rsi >= 60:
                    rsi_emoji = "🟡"
                    rsi_status = "близко к перекупленности"
                elif rsi <= 40:
                    rsi_emoji = "🟡"
                    rsi_status = "близко к перепроданности"
                else:
                    rsi_emoji = "⚪"
                    rsi_status = "нейтральная зона"
                rsi_text = f"\n{rsi_emoji} RSI: {rsi:.1f} ({rsi_status})"
            
            # Экранируем ticker для Markdown
            ticker_escaped = _escape_markdown(ticker)
            
            response = f"""
💰 **{ticker_escaped}**

📅 Дата: {date_str}
💵 Цена: {close_str}
📈 SMA(5): {sma_str}
📊 Волатильность(5): {vol_str}{rsi_text}
            """
            
            # Пытаемся отправить с Markdown, при ошибке — без форматирования
            try:
                await update.message.reply_text(response.strip(), parse_mode='Markdown')
            except Exception as parse_err:
                if 'parse' in str(parse_err).lower() or 'entit' in str(parse_err).lower():
                    logger.warning(f"Ошибка парсинга Markdown для /price {ticker}, отправляем без форматирования: {parse_err}")
                    await update.message.reply_text(response.strip())
                else:
                    raise
            
        except Exception as e:
            logger.error(f"Ошибка получения цены для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def _handle_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /price <ticker>"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите тикер\n"
                "Пример: `/price GC=F`",
                parse_mode='Markdown'
            )
            return
        
        ticker_raw = context.args[0].upper()
        ticker = _normalize_ticker(ticker_raw)
        await self._handle_price_by_ticker(update, ticker, ticker_raw)
    
    async def _handle_chart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /chart <ticker> [days]"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Укажите тикер\n"
                "Пример: `/chart GC=F` или `/chart GC=F 7` (за 7 дней)",
                parse_mode='Markdown'
            )
            return
        
        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 1  # По умолчанию текущий день
        for i in range(1, len(context.args)):
            try:
                d = int(context.args[i].strip())
                days = max(1, min(30, d))
                break
            except (ValueError, IndexError):
                continue

        try:
            await update.message.reply_text(f"📈 Построение графика для {ticker}...")

            # Получаем данные из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from datetime import datetime, timedelta
            import pandas as pd

            engine = create_engine(get_database_url())
            # Начало дня (00:00), чтобы не отсечь дневные свечи с date в полночь
            cutoff_date = (datetime.now() - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            
            logger.info(f"Запрос данных для {ticker} с {cutoff_date} (последние {days} дней)")
            
            with engine.connect() as conn:
                df = pd.read_sql(
                    text("""
                        SELECT date, open, high, low, close, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker AND date >= :cutoff_date
                        ORDER BY date ASC
                    """),
                    conn,
                    params={"ticker": ticker, "cutoff_date": cutoff_date}
                )
                # Сделки за период графика — для отметок входа/выхода (фиксация прибыли/убытков)
                end_date = (pd.Timestamp(df["date"].max()) + pd.Timedelta(days=1)) if not df.empty else datetime.now()
                trades_rows = conn.execute(
                    text("""
                        SELECT ts, price, side, signal_type
                        FROM trade_history
                        WHERE ticker = :ticker AND ts >= :cutoff_date AND ts < :end_date
                        ORDER BY ts ASC
                    """),
                    {"ticker": ticker, "cutoff_date": cutoff_date, "end_date": end_date},
                ).fetchall()
            
            logger.info(f"Получено {len(df)} записей для {ticker}")
            
            if df.empty:
                logger.warning(f"Нет данных для {ticker} за последние {days} дней")
                await update.message.reply_text(
                    f"❌ Нет данных для {ticker} за последние {days} дней\n"
                    f"Попробуйте увеличить период: `/chart {ticker} 7`",
                    parse_mode='Markdown'
                )
                return
            
            # Объясняем пользователю формат данных
            if days == 1 and len(df) == 1:
                await update.message.reply_text(
                    f"ℹ️ **Формат данных:**\n\n"
                    f"В базе хранятся **дневные данные** (цена закрытия за день), "
                    f"а не внутридневные.\n\n"
                    f"За один день = одна запись (цена закрытия).\n\n"
                    f"Для графика с несколькими точками используйте:\n"
                    f"`/chart {ticker} 7` (7 дней = 7 точек)\n"
                    f"`/chart {ticker} 30` (30 дней = 30 точек)",
                    parse_mode='Markdown'
                )
            
            # Строим график
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                import matplotlib.dates as mdates
                from matplotlib.patches import Rectangle
                from matplotlib.lines import Line2D
                from io import BytesIO

                logger.info("Инициализация matplotlib...")
                try:
                    plt.style.use('seaborn-v0_8-whitegrid')
                except Exception:
                    pass
                plt.rcParams['font.size'] = 9

                df['date'] = pd.to_datetime(df['date'])
                n_points = len(df)
                has_ohlc = all(c in df.columns and df[c].notna().any() for c in ('open', 'high', 'low'))

                # Разбор сделок для отметок на графике (вход / тейк / стоп / выход)
                trades_buy_ts, trades_buy_p = [], []
                trades_take_ts, trades_take_p = [], []
                trades_stop_ts, trades_stop_p = [], []
                trades_other_ts, trades_other_p = [], []
                for row in trades_rows:
                    ts, price, side, signal_type = row[0], float(row[1]), row[2], (row[3] or "")
                    if ts is None:
                        continue
                    ts = pd.Timestamp(ts)
                    if getattr(ts, "tzinfo", None) is not None:
                        try:
                            ts = ts.tz_localize(None)
                        except Exception:
                            ts = ts.tz_convert(None) if ts.tzinfo else ts
                    if side == "BUY":
                        trades_buy_ts.append(ts)
                        trades_buy_p.append(price)
                    elif side == "SELL":
                        sig = (signal_type or "").upper()
                        if sig == "TAKE_PROFIT":
                            trades_take_ts.append(ts)
                            trades_take_p.append(price)
                        elif sig == "STOP_LOSS":
                            trades_stop_ts.append(ts)
                            trades_stop_p.append(price)
                        else:
                            trades_other_ts.append(ts)
                            trades_other_p.append(price)

                # Интервал подписей дат: все точки рисуем, подписи реже
                if n_points <= 7:
                    day_interval = 1
                elif n_points <= 14:
                    day_interval = 2
                else:
                    day_interval = max(1, n_points // 10)

                def draw_price_axes(ax1, use_ohlc):
                    ax1.set_facecolor('#ffffff')
                    if use_ohlc:
                        width = 0.7
                        half = width / 2
                        hr = df['high'].max() - df['low'].min()
                        hr = hr if hr and hr > 0 else float(df['close'].max() - df['close'].min() or 1)
                        min_body = max(0.005 * hr, 0.01)
                        for _, row in df.iterrows():
                            x = mdates.date2num(row['date'])
                            o = row.get('open') if pd.notna(row.get('open')) else row['close']
                            h = row.get('high') if pd.notna(row.get('high')) else max(o, row['close'])
                            l = row.get('low') if pd.notna(row.get('low')) else min(o, row['close'])
                            c = float(row['close'])
                            o, h, l = float(o), float(h), float(l)
                            # Тени (тонкие)
                            ax1.vlines(x, l, h, color='#444', linewidth=0.6, alpha=0.9)
                            top, bot = max(o, c), min(o, c)
                            body_h = (top - bot) if top > bot else min_body
                            if top == bot:
                                bot -= min_body / 2
                                body_h = min_body
                            color = '#26a69a' if c >= o else '#ef5350'  # зелёный / красный
                            rect = Rectangle((x - half, bot), width, body_h, facecolor=color, edgecolor=color, linewidth=0.5)
                            ax1.add_patch(rect)
                        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                        ax1.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                        leg_up = Line2D([0], [0], color='#26a69a', linewidth=6, label='Рост')
                        leg_dn = Line2D([0], [0], color='#ef5350', linewidth=6, label='Падение')
                        legend_handles = [leg_up, leg_dn]
                    else:
                        ax1.plot(df['date'], df['close'], color='#1565c0', linewidth=2, label='Close')
                        legend_handles = []
                    if 'sma_5' in df.columns and df['sma_5'].notna().any():
                        ax1.plot(df['date'], df['sma_5'], color='#7e57c2', linewidth=1.2, linestyle='--', label='SMA(5)')
                    ax1.set_ylabel('Цена', fontsize=10)
                    h = list(legend_handles) + [l for l in ax1.get_lines() if (l.get_label() or '').startswith('SMA')]
                    ax1.legend(handles=h if h else None, loc='upper left', framealpha=0.9)
                    ax1.grid(True, linestyle='--', alpha=0.4)
                    ax1.tick_params(axis='both', labelsize=9)

                def draw_trade_markers(ax):
                    """Отметки сделок: вход (BUY), тейк, стоп, прочий выход."""
                    if trades_buy_ts:
                        ax.scatter(trades_buy_ts, trades_buy_p, color='#2e7d32', marker='^', s=80, zorder=5, label='Вход (BUY)', edgecolors='darkgreen', linewidths=1)
                    if trades_take_ts:
                        ax.scatter(trades_take_ts, trades_take_p, color='#1b5e20', marker='v', s=80, zorder=5, label='Тейк', edgecolors='darkgreen', linewidths=1)
                    if trades_stop_ts:
                        ax.scatter(trades_stop_ts, trades_stop_p, color='#c62828', marker='v', s=80, zorder=5, label='Стоп', edgecolors='darkred', linewidths=1)
                    if trades_other_ts:
                        ax.scatter(trades_other_ts, trades_other_p, color='#757575', marker='v', s=60, zorder=4, label='Выход', edgecolors='gray', linewidths=0.8)

                has_rsi = 'rsi' in df.columns and df['rsi'].notna().any()
                if n_points <= 2 or not has_rsi:
                    fig, ax1 = plt.subplots(1, 1, figsize=(11, 5), facecolor='white')
                    draw_price_axes(ax1, has_ohlc)
                    draw_trade_markers(ax1)
                    ax1.legend(loc='upper left', framealpha=0.9)
                    ax1.set_xlabel('Дата', fontsize=10)
                    ax1.set_title(f'{ticker}  —  {n_points} дн.', fontsize=11, fontweight='bold', pad=6)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=30, ha='right')
                else:
                    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), facecolor='white', sharex=True,
                                                    gridspec_kw={'height_ratios': [1.4, 0.8], 'hspace': 0.08})
                    draw_price_axes(ax1, has_ohlc)
                    draw_trade_markers(ax1)
                    ax1.legend(loc='upper left', framealpha=0.9)
                    ax1.set_title(f'{ticker}  —  {n_points} дн.', fontsize=11, fontweight='bold', pad=6)
                    ax2.set_facecolor('#ffffff')
                    ax2.plot(df['date'], df['rsi'], color='#ff9800', linewidth=1.8, label='RSI')
                    ax2.axhline(y=70, color='#c62828', linestyle='--', alpha=0.6, linewidth=0.8)
                    ax2.axhline(y=30, color='#2e7d32', linestyle='--', alpha=0.6, linewidth=0.8)
                    ax2.set_ylabel('RSI', fontsize=10)
                    ax2.set_ylim(0, 100)
                    ax2.legend(loc='upper left', framealpha=0.9)
                    ax2.grid(True, linestyle='--', alpha=0.4)
                    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
                    ax2.xaxis.set_major_locator(mdates.DayLocator(interval=day_interval))
                    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha='right')
                    ax2.tick_params(axis='both', labelsize=9)

                plt.tight_layout(pad=1.2)
                img_buffer = BytesIO()
                plt.savefig(img_buffer, format='png', dpi=120, bbox_inches='tight', facecolor='white')
                img_buffer.seek(0)
                plt.close()
                
                logger.info(f"Отправка графика для {ticker} ({len(df)} точек данных)")
                
                # Формируем подпись
                n_trades = len(trades_buy_ts) + len(trades_take_ts) + len(trades_stop_ts) + len(trades_other_ts)
                caption = f"📈 {ticker} - {days} дней ({len(df)} точек)"
                if n_trades > 0:
                    caption += f"\n📌 Сделки на графике: вход (▲), тейк/стоп/выход (▼) — {n_trades} шт. (те же, что в /history)"
                if has_ohlc:
                    caption += "\n\nℹ️ Свечи: open, high, low, close (дневные)"
                elif days == 1:
                    caption += "\n\nℹ️ Данные: дневные (цена закрытия за день)"
                elif len(df) < 5:
                    caption += "\n\nℹ️ Данные: дневные (цена закрытия). Для свечей загрузите OHLC: python update_prices.py --backfill 30"
                
                # Отправляем изображение
                await update.message.reply_photo(photo=img_buffer, caption=caption)
                
            except ImportError as e:
                logger.error(f"Ошибка импорта matplotlib: {e}")
                await update.message.reply_text(
                    "❌ Библиотека matplotlib не установлена.\n"
                    "Установите: `pip install matplotlib`"
                )
            except Exception as e:
                logger.error(f"Ошибка построения графика: {e}", exc_info=True)
                await update.message.reply_text(f"❌ Ошибка построения графика: {str(e)}")
            
        except Exception as e:
            logger.error(f"Ошибка построения графика для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка построения графика: {str(e)}")

    def _fetch_5m_data_sync(self, ticker: str, days: int = 5):
        """Синхронная загрузка 5-минутных данных через yfinance (вызывать из executor).

        Запрашивает явный диапазон дат [сегодня − days .. сегодня], чтобы получать
        самые свежие данные. Yahoo при period='1d' отдаёт «последний торговый день»
        с задержкой, поэтому без start/end данные могут быть за прошлые дни.
        """
        import yfinance as yf
        import pandas as pd
        t = yf.Ticker(ticker)
        days = min(max(1, days), 7)
        end_date = datetime.utcnow() + timedelta(days=1)  # end exclusive
        start_date = datetime.utcnow() - timedelta(days=days)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        df = t.history(start=start_str, end=end_str, interval="5m", auto_adjust=False)
        if df is None or df.empty:
            return None
        df = df.rename_axis("datetime").reset_index()
        for c in ("Open", "High", "Low", "Close"):
            if c not in df.columns:
                return None
        return df

    async def _handle_chart5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """График 5-минутных данных по требованию."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        if not context.args:
            await update.message.reply_text(
                "❌ Укажите тикер. Пример: `/chart5m SNDK` или `/chart5m GBPUSD=X 3`",
                parse_mode="Markdown"
            )
            return
        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 5
        for i in range(1, len(context.args)):
            try:
                days = max(1, min(7, int(context.args[i].strip())))
                break
            except (ValueError, IndexError):
                continue
        await update.message.reply_text(f"📥 Загрузка 5m данных для {ticker} за {days} дн....")
        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(None, self._fetch_5m_data_sync, ticker, days)
        except Exception as e:
            logger.exception("Ошибка загрузки 5m")
            await update.message.reply_text(f"❌ Ошибка загрузки: {e}")
            return
        if df is None or df.empty:
            await update.message.reply_text(
                f"❌ Нет 5m данных для {ticker}. Yahoo даёт 5m за 1–7 дней. "
                "Попробуйте /chart5m SNDK 1 или 7. В выходные биржа закрыта."
            )
            return
        # Открытая позиция для отметки на графике (игра 5m или портфель)
        entry_price = None
        try:
            from services.game_5m import get_open_position as get_game_position
            pos = get_game_position(ticker)
            if pos and isinstance(pos.get("entry_price"), (int, float)):
                entry_price = float(pos["entry_price"])
        except Exception:
            pass
        if entry_price is None:
            try:
                from execution_agent import ExecutionAgent
                ex = ExecutionAgent()
                summary = ex.get_portfolio_summary()
                for p in (summary.get("positions") or []):
                    if p.get("ticker") == ticker and isinstance(p.get("entry_price"), (int, float)):
                        entry_price = float(p["entry_price"])
                        break
            except Exception:
                pass
        # Прогноз для графика: хай сессии, оценка подъёма по кривизне, тейк при открытой позиции
        d5_chart = None
        try:
            from services.recommend_5m import get_decision_5m
            d5_chart = await loop.run_in_executor(None, lambda: get_decision_5m(ticker, days=days, use_llm_news=False))
        except Exception:
            pass
        try:
            import pandas as pd
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from io import BytesIO
            df["datetime"] = pd.to_datetime(df["datetime"])
            # Шкала в времени американской биржи (Eastern): для отображения — naive Eastern
            if hasattr(df["datetime"].dtype, "tz") and df["datetime"].dtype.tz is not None:
                dt_plot = df["datetime"].dt.tz_convert("America/New_York").dt.tz_localize(None)
            else:
                dt_plot = df["datetime"]
            dt_min = dt_plot.min()
            dt_max = dt_plot.max()
            last_close = float(df["Close"].iloc[-1])
            extend_hours = 2
            dt_max_ext = dt_max + pd.Timedelta(hours=extend_hours)
            fig, ax = plt.subplots(1, 1, figsize=(11, 5), facecolor="white")
            ax.set_facecolor("#ffffff")
            ax.set_xlim(dt_min, dt_max_ext)
            ax.plot(dt_plot, df["Close"], color="#1565c0", linewidth=1.2, label="Close")
            if "Open" in df.columns:
                ax.fill_between(dt_plot, df["Low"], df["High"], alpha=0.15, color="#1565c0")
            if entry_price is not None:
                ax.axhline(
                    entry_price,
                    color="#2e7d32",
                    linestyle="--",
                    linewidth=1.2,
                    alpha=0.9,
                    label=f"Вход @ {entry_price:.2f}",
                )
            # Прогноз на графике: хай сессии, оценка подъёма по кривизне, тейк при открытой позиции
            if d5_chart:
                price_cur = d5_chart.get("price")
                session_high = d5_chart.get("session_high")
                est_bounce = d5_chart.get("estimated_bounce_pct")
                if session_high is not None and session_high > 0:
                    ax.axhline(
                        session_high,
                        color="#f57c00",
                        linestyle=":",
                        linewidth=1.0,
                        alpha=0.85,
                        label=f"Хай сессии {session_high:.2f}",
                    )
                if price_cur is not None and price_cur > 0 and est_bounce is not None and est_bounce > 0:
                    forecast_price = price_cur * (1 + est_bounce / 100.0)
                    ax.axhline(
                        forecast_price,
                        color="#00897b",
                        linestyle="-.",
                        linewidth=1.0,
                        alpha=0.85,
                        label=f"Прогноз подъёма ~{forecast_price:.2f}",
                    )
                if entry_price is not None and entry_price > 0:
                    try:
                        from services.game_5m import _effective_take_profit_pct
                        mom = d5_chart.get("momentum_2h_pct")
                        take_pct = _effective_take_profit_pct(mom)
                        take_level = entry_price * (1 + take_pct / 100.0)
                        ax.axhline(
                            take_level,
                            color="#2e7d32",
                            linestyle=":",
                            linewidth=1.0,
                            alpha=0.7,
                            label=f"Тейк +{take_pct:.1f}%",
                        )
                    except Exception:
                        pass
            # Зона прогноза: аппроксимация хвоста (linear/quadratic) и пролонгация при тренде ≥5 свечей
            ax.axvline(dt_max, color="#c62828", linestyle="-", linewidth=0.9, alpha=0.8)
            ax.axvspan(dt_max, dt_max_ext, alpha=0.08, color="#c62828", zorder=0)
            min_bars_trend = 5
            prolong_bars = 12  # баров 5m в зону прогноза (~1 ч)
            prolongation_method = "ema"  # ema — для волатильных (рекомендация Gemini); linear, quadratic — альтернативы
            forecast_defined = False
            if len(df) >= min_bars_trend and last_close > 0:
                from services.chart_prolongation import fit_and_prolong
                closes_tail = df["Close"].astype(float).iloc[-min_bars_trend:].values
                res = fit_and_prolong(closes_tail, method=prolongation_method, prolong_bars=prolong_bars)
                slope_per_bar = res["slope_per_bar"]
                min_slope_pct = 0.01
                min_slope = last_close * (min_slope_pct / 100.0)
                if slope_per_bar >= min_slope or slope_per_bar <= -min_slope:
                    # Якорь: первая точка кривой = last_close (без скачка)
                    curve_prices = res["curve_prices"]
                    anchor_shift = last_close - (curve_prices[0] if curve_prices else last_close)
                    curve_prices = [p + anchor_shift for p in curve_prices]
                    end_price = curve_prices[-1]
                    bar_offsets = res["curve_bar_offsets"]
                    prolong_dts = [dt_max + pd.Timedelta(minutes=5 * k) for k in bar_offsets]
                    label = "Прогноз ↑" if slope_per_bar >= min_slope else "Прогноз ↓"
                    ax.plot(prolong_dts, curve_prices, color="#c62828", linewidth=1.2, linestyle="-", alpha=0.9, label=label)
                    forecast_defined = True
            if not forecast_defined:
                ax.plot([dt_max, dt_max_ext], [last_close, last_close], color="#c62828", linewidth=0.8, linestyle=":", alpha=0.4, label="Текущая цена")
                ax.text(0.985, 0.5, "…\nпрогноз\nне определён", transform=ax.transAxes, fontsize=9, color="#c62828", ha="right", va="center", style="italic")
            # Сделки игры 5m за период графика (те же, что в /history)
            buy_ts, buy_p = [], []
            take_ts, take_p = [], []
            stop_ts, stop_p = [], []
            other_ts, other_p = [], []
            try:
                from services.game_5m import get_trades_for_chart
                # Берём сделки за весь календарный день (чтобы не терять из-за TZ: список в /history = те же точки на графике)
                dt_min_day = pd.Timestamp(dt_min).replace(hour=0, minute=0, second=0, microsecond=0)
                dt_max_end = pd.Timestamp(dt_max) + pd.Timedelta(days=1)
                trades = get_trades_for_chart(ticker, dt_min_day.to_pydatetime(), dt_max_end.to_pydatetime())
                for t in trades:
                    ts = t["ts"]
                    try:
                        if ts is not None and hasattr(ts, "tzinfo") and getattr(ts, "tzinfo", None) is not None:
                            ts_pd = pd.Timestamp(ts)
                            if ts_pd.tzinfo is not None:
                                ts = ts_pd.tz_convert("America/New_York").tz_localize(None).to_pydatetime()
                    except Exception:
                        pass
                    p = float(t["price"])
                    if t["side"] == "BUY":
                        buy_ts.append(ts)
                        buy_p.append(p)
                    elif t["side"] == "SELL":
                        sig = (t.get("signal_type") or "").upper()
                        if sig == "TAKE_PROFIT":
                            take_ts.append(ts)
                            take_p.append(p)
                        elif sig == "STOP_LOSS":
                            stop_ts.append(ts)
                            stop_p.append(p)
                        else:
                            other_ts.append(ts)
                            other_p.append(p)
                if buy_ts:
                    ax.scatter(buy_ts, buy_p, color="#2e7d32", marker="^", s=70, zorder=5, label="Вход (BUY)", edgecolors="darkgreen", linewidths=1)
                if take_ts:
                    ax.scatter(take_ts, take_p, color="#1b5e20", marker="v", s=70, zorder=5, label="Тейк (хорошо)", edgecolors="darkgreen", linewidths=1)
                if stop_ts:
                    ax.scatter(stop_ts, stop_p, color="#c62828", marker="v", s=70, zorder=5, label="Стоп (плохо)", edgecolors="darkred", linewidths=1)
                if other_ts:
                    ax.scatter(other_ts, other_p, color="#757575", marker="v", s=50, zorder=4, label="Выход (SELL/время)", edgecolors="gray", linewidths=0.8)
                # Расширяем ось X, чтобы сделки после 16:00 (или в другом TZ) были видны
                all_ts = buy_ts + take_ts + stop_ts + other_ts
                if all_ts:
                    x_max_cur = ax.get_xlim()[1]
                    x_max_trades = max(mdates.date2num(t) for t in all_ts if t is not None)
                    if x_max_trades > x_max_cur:
                        ax.set_xlim(right=x_max_trades + 0.002)
            except Exception:
                pass
            ax.set_ylabel("Цена", fontsize=10)
            ax.set_xlabel("Дата, время", fontsize=10)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
            ax.legend(loc="upper left", fontsize=8)
            ax.grid(True, linestyle="--", alpha=0.4)
            range_str = f"{dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}"
            ax.set_title(f"{ticker} — 5m ({len(df)} точек) · время US Eastern", fontsize=11, fontweight="bold")
            plt.tight_layout()
            buf = BytesIO()
            plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="white")
            buf.seek(0)
            plt.close()
            n_markers = len(buy_ts) + len(take_ts) + len(stop_ts) + len(other_ts)
            caption = f"📈 {ticker} — 5 мин, {len(df)} свечей. Данные за: {range_str}"
            if n_markers > 0:
                caption += f"\n📌 Сделки на графике: ▲ вход, ▼ тейк/стоп/выход ({n_markers} шт.) — те же, что в /history. Время — ET."
            if entry_price is not None:
                caption += f"\n📌 Позиция открыта @ ${entry_price:.2f}"
            await update.message.reply_photo(
                photo=buf,
                caption=caption,
            )
        except Exception as e:
            logger.exception("Ошибка графика 5m")
            await update.message.reply_text(f"❌ Ошибка графика: {e}")

    async def _handle_table5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Таблица последних 5-минутных свечей."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        if not context.args:
            await update.message.reply_text(
                "❌ Укажите тикер. Пример: `/table5m SNDK` или `/table5m GC=F 2`",
                parse_mode="Markdown"
            )
            return
        ticker_raw = context.args[0].strip().upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 3
        for i in range(1, len(context.args)):
            try:
                days = max(1, min(7, int(context.args[i].strip())))
                break
            except (ValueError, IndexError):
                continue
        await update.message.reply_text(f"📥 Загрузка 5m для {ticker}...")
        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(None, self._fetch_5m_data_sync, ticker, days)
        except Exception as e:
            logger.exception("Ошибка загрузки 5m")
            await update.message.reply_text(f"❌ Ошибка загрузки: {e}")
            return
        if df is None or df.empty:
            await update.message.reply_text(f"❌ Нет 5m данных для {ticker}.")
            return
        import pandas as pd
        df["datetime"] = pd.to_datetime(df["datetime"])
        total = len(df)
        df_sorted = df.sort_values("datetime", ascending=False)
        range_str = ""
        if not df_sorted.empty:
            dt_min = df_sorted["datetime"].min()
            dt_max = df_sorted["datetime"].max()
            range_str = f"\n_Период в данных: {dt_min.strftime('%d.%m %H:%M')} – {dt_max.strftime('%d.%m %H:%M')}_"
        df_head = df_sorted.head(25)
        lines = [f"`{'Дата':<16} {'O':>10} {'H':>10} {'L':>10} {'C':>10}`"]
        for _, row in df_head.iterrows():
            ts = row["datetime"].strftime("%d.%m %H:%M")
            o = float(row["Open"]) if pd.notna(row["Open"]) else 0.0
            h = float(row["High"]) if pd.notna(row["High"]) else 0.0
            lo = float(row["Low"]) if pd.notna(row["Low"]) else 0.0
            c = float(row["Close"]) if pd.notna(row["Close"]) else 0.0
            lines.append(f"`{ts:<16} {o:>10.4f} {h:>10.4f} {lo:>10.4f} {c:>10.4f}`")
        msg = f"📋 **{ticker}** — 5m свечи (последние {len(df_head)} из {total}){range_str}\n\n" + "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3970] + "\n…"
        await update.message.reply_text(msg, parse_mode="Markdown")

    def _build_dashboard_sync(self, mode: str = "all") -> str:
        """Строит сводку дашборда (делегирует в services.dashboard_builder)."""
        from services.dashboard_builder import build_dashboard_text
        return build_dashboard_text(mode)

    async def _handle_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Дашборд по отслеживаемым тикерам для проактивного мониторинга (решения, 5m, новости)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        mode = "all"
        if context.args:
            a = context.args[0].strip().lower()
            if a in ("5m", "daily", "all"):
                mode = a
        await update.message.reply_text("📥 Сбор дашборда...")
        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, self._build_dashboard_sync, mode)
        except Exception as e:
            logger.exception("Ошибка дашборда")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return
        if len(text) > 4000:
            parts = [text[i : i + 4000] for i in range(0, len(text), 4000)]
            for p in parts:
                await update.message.reply_text(p, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    
    async def _handle_tickers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /tickers"""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is None or not self._check_access(user_id):
            await self._reply_to_update(update, context, "❌ Доступ запрещен")
            return

        async def _send(text: str, parse_mode: str = "Markdown") -> None:
            await self._reply_to_update(update, context, text, parse_mode=parse_mode)

        try:
            # Получаем список тикеров из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from services.ticker_groups import (
                get_tickers_fast,
                get_tickers_for_portfolio_game,
            )

            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
                )
                tickers = [row[0] for row in result]

            if not tickers:
                await _send("ℹ️ Нет отслеживаемых инструментов")
                return

            # Игры: в каких группах используется тикер
            fast_set = set(get_tickers_fast())
            portfolio_set = set(get_tickers_for_portfolio_game())

            def _game_label(t: str) -> str:
                in_fast = t in fast_set
                in_port = t in portfolio_set
                if in_fast and in_port:
                    return " (5m, Портфель)"
                if in_fast:
                    return " (5m)"
                if in_port:
                    return " (Портфель)"
                return ""

            # Группируем по типам
            commodities = [t for t in tickers if '=' in t or t.startswith('GC')]
            currencies = [t for t in tickers if 'USD' in t or 'EUR' in t or 'GBP' in t]
            stocks = [t for t in tickers if t not in commodities and t not in currencies]

            response = "📊 **Отслеживаемые инструменты:**\n\n"

            def _line(t: str) -> str:
                return f"  • {_escape_markdown(t)}{_game_label(t)}"

            if commodities:
                response += "🥇 **Товары:**\n"
                response += "\n".join([_line(t) for t in commodities[:10]])
                response += "\n\n"

            if currencies:
                response += "💱 **Валютные пары:**\n"
                response += "\n".join([_line(t) for t in currencies[:10]])
                response += "\n\n"

            if stocks:
                response += "📈 **Акции:**\n"
                response += "\n".join([_line(t) for t in stocks[:10]])

            if len(tickers) > 30:
                response += "\n\n... и еще " + _escape_markdown(str(len(tickers) - 30)) + " инструментов"

            legend = "5m — быстрая игра; Портфель — trading_cycle (MEDIUM/LONG)."
            response += "\n\n" + _escape_markdown(legend)

            await _send(response)

        except Exception as e:
            logger.error(f"Ошибка получения списка тикеров: {e}", exc_info=True)
            await _send(f"❌ Ошибка: {str(e)}")
    
    def _get_recommendation_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Собирает данные для рекомендации: сигнал, цена, риск-параметры, позиция по тикеру."""
        try:
            result = self.analyst.get_decision_with_llm(ticker)
            decision = result.get("decision", "HOLD")
            strategy = result.get("selected_strategy") or "—"
            technical = result.get("technical_data") or {}
            sentiment = result.get("sentiment_normalized") or result.get("sentiment") or 0.0
            if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 1:
                sentiment = (sentiment - 0.5) * 2.0
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                    {"ticker": ticker},
                ).fetchone()
            price = float(row[0]) if row and row[0] is not None else None
            rsi = float(row[1]) if row and row[1] is not None else technical.get("rsi")
            try:
                from utils.risk_manager import get_risk_manager
                rm = get_risk_manager()
                stop_loss_pct = rm.get_stop_loss_percent()
                take_profit_pct = rm.get_take_profit_percent()
                max_pos_usd = rm.get_max_position_size(ticker)
                max_ticker_pct = rm.get_max_single_ticker_exposure()
            except Exception:
                stop_loss_pct = 5.0
                take_profit_pct = 10.0
                max_pos_usd = 10000.0
                max_ticker_pct = 20.0
            has_position = False
            position_info = None
            ex = self._get_execution_agent()
            if ex:
                summary = ex.get_portfolio_summary()
                for p in summary.get("positions") or []:
                    if p["ticker"] == ticker:
                        has_position = True
                        position_info = p
                        break
            return {
                "ticker": ticker,
                "decision": decision,
                "strategy": strategy,
                "price": price,
                "rsi": rsi,
                "sentiment": sentiment,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "max_position_usd": max_pos_usd,
                "max_ticker_pct": max_ticker_pct,
                "has_position": has_position,
                "position": position_info,
                "reasoning": result.get("reasoning", ""),
            }
        except Exception as e:
            logger.warning(f"Ошибка сбора рекомендации для {ticker}: {e}")
            return None

    def _format_recommendation(self, data: Dict[str, Any]) -> str:
        """Форматирует текст рекомендации по данным из _get_recommendation_data."""
        t = _escape_markdown(data["ticker"])
        decision = data["decision"]
        strategy = data["strategy"]
        price = data["price"]
        price_str = f"${price:.2f}" if price is not None else "—"
        rsi = data["rsi"]
        rsi_str = f"{rsi:.1f}" if rsi is not None else "—"
        sl = data["stop_loss_pct"]
        tp = data["take_profit_pct"]
        max_usd = data["max_position_usd"]
        max_pct = data["max_ticker_pct"]
        has_pos = data["has_position"]
        pos = data.get("position")
        if decision in ("BUY", "STRONG_BUY"):
            action = "можно открывать длинную позицию" if not has_pos else "позиция уже открыта — можно держать или докупать по своей тактике"
            emoji = "🟢"
        elif decision == "SELL":
            action = "рекомендуется закрыть или не открывать длинную позицию" if has_pos else "вход не рекомендую; можно рассмотреть короткую или ждать разворота"
            emoji = "🔴"
        else:
            action = "сигнал нейтральный — лучше подождать более чёткого сигнала перед входом"
            emoji = "⚪"
        lines = [
            f"{emoji} **Рекомендация по {t}**",
            "",
            f"**Сигнал:** {decision} (стратегия: {strategy})",
            f"**Цена:** {price_str}  ·  **RSI:** {rsi_str}",
            "",
            f"**Действие:** {action}",
            "",
            "**Параметры управления (песочница):**",
            f"• Стоп-лосс: −{sl:.0f}% от цены входа",
            f"• Тейк-профит (ориентир): +{tp:.0f}%",
            f"• Размер позиции: до ${max_usd:,.0f} или до {max_pct:.0f}% портфеля",
        ]
        if has_pos and pos:
            pnl = pos.get("pnl") or 0
            pnl_pct = pos.get("pnl_pct") or 0
            lines.append(f"\n_Текущая позиция: P&L ${pnl:,.2f} ({pnl_pct:+.2f}%)_")
        if data.get("reasoning"):
            lines.append(f"\n💭 _{_escape_markdown(str(data['reasoning'])[:180])}..._")
        return "\n".join(lines)

    def _get_recommendation_data_5m(self, ticker: str, days: int = 5) -> Optional[Dict[str, Any]]:
        """Собирает данные для рекомендации по 5m (свечи за 5–7 дн. + опционально LLM перед решением)."""
        try:
            from services.recommend_5m import get_decision_5m
            data_5m = get_decision_5m(ticker, days=days, use_llm_news=True)
            if not data_5m:
                return None
            has_position = False
            position_info = None
            ex = self._get_execution_agent()
            if ex:
                summary = ex.get_portfolio_summary()
                for p in summary.get("positions") or []:
                    if p["ticker"] == ticker:
                        has_position = True
                        position_info = p
                        break
            alex_rule = None
            if ticker.upper() == "SNDK":
                try:
                    from services.alex_rule import get_alex_rule_status
                    alex_rule = get_alex_rule_status(ticker, data_5m.get("price"))
                except Exception:
                    pass
            return {
                "ticker": ticker,
                "decision": data_5m["decision"],
                "strategy": "5m (интрадей + 5–7д статистика)",
                "price": data_5m["price"],
                "rsi": data_5m.get("rsi_5m"),
                "reasoning": data_5m.get("reasoning", ""),
                "period_str": data_5m.get("period_str", ""),
                "momentum_2h_pct": data_5m.get("momentum_2h_pct"),
                "volatility_5m_pct": data_5m.get("volatility_5m_pct"),
                "stop_loss_pct": data_5m.get("stop_loss_pct", 2.5),
                "take_profit_pct": data_5m.get("take_profit_pct", 5.0),
                "bars_count": data_5m.get("bars_count"),
                "has_position": has_position,
                "position": position_info,
                "alex_rule": alex_rule,
                "llm_insight": data_5m.get("llm_insight"),
                "llm_news_content": data_5m.get("llm_news_content"),
                "curvature_5m_pct": data_5m.get("curvature_5m_pct"),
                "possible_bounce_to_high_pct": data_5m.get("possible_bounce_to_high_pct"),
                "estimated_bounce_pct": data_5m.get("estimated_bounce_pct"),
                "session_high": data_5m.get("session_high"),
                "entry_advice": data_5m.get("entry_advice"),
                "entry_advice_reason": data_5m.get("entry_advice_reason"),
                "estimated_upside_pct_day": data_5m.get("estimated_upside_pct_day"),
                "suggested_take_profit_price": data_5m.get("suggested_take_profit_price"),
                "premarket_entry_recommendation": data_5m.get("premarket_entry_recommendation"),
                "premarket_suggested_limit_price": data_5m.get("premarket_suggested_limit_price"),
                "premarket_last": data_5m.get("premarket_last"),
                "premarket_gap_pct": data_5m.get("premarket_gap_pct"),
                "minutes_until_open": data_5m.get("minutes_until_open"),
                "max_position_usd": 0,
                "max_ticker_pct": 0,
            }
        except Exception as e:
            logger.warning(f"Ошибка рекомендации 5m для {ticker}: {e}")
            return None

    def _format_recommendation_5m(self, data: Dict[str, Any]) -> str:
        """Форматирует текст рекомендации по 5m данным."""
        t = _escape_markdown(data["ticker"])
        decision = data["decision"]
        price = data["price"]
        price_str = f"${price:.2f}" if price is not None else "—"
        rsi = data.get("rsi")
        rsi_str = f"{rsi:.1f}" if rsi is not None else "—"
        sl = data.get("stop_loss_pct", 2.5)
        tp = data.get("take_profit_pct", 5.0)
        period_str = data.get("period_str") or ""
        mom = data.get("momentum_2h_pct")
        mom_str = f"{mom:+.2f}%" if mom is not None else "—"
        vol = data.get("volatility_5m_pct")
        vol_str = f"{vol:.2f}%" if vol is not None else "—"
        has_pos = data.get("has_position", False)
        pos = data.get("position")
        if decision in ("BUY", "STRONG_BUY"):
            action = "можно открывать длинную позицию (по 5m)" if not has_pos else "позиция открыта — держать или докупать по тактике"
            emoji = "🟢"
        elif decision == "SELL":
            action = "рекомендуется закрыть или не входить" if has_pos else "вход не рекомендую по 5m"
            emoji = "🔴"
        else:
            action = "сигнал нейтральный — ждать более чёткого сигнала по 5m"
            emoji = "⚪"
        lines = [
            f"{emoji} **Рекомендация 5m по {t}**",
            "",
            f"**Сигнал:** {decision} (стратегия: 5m + 5д статистика)",
            f"**Цена:** {price_str}  ·  **RSI(5m):** {rsi_str}  ·  **Импульс 2ч:** {mom_str}  ·  **Волатильность 5m:** {vol_str}",
            "",
            f"**Период данных:** {period_str}" if period_str else "",
            "",
            f"**Действие:** {action}",
            "",
            "**Параметры (интрадей):**",
            f"• Стоп-лосс: −{sl:.1f}%  ·  Тейк-профит: +{tp:.1f}%",
        ]
        upside = data.get("estimated_upside_pct_day")
        take_price = data.get("suggested_take_profit_price")
        if upside is not None or take_price is not None:
            parts = []
            if upside is not None:
                parts.append(f"Оценка апсайда на день: +{upside:.1f}%")
            if take_price is not None:
                parts.append(f"Цель (close-ордер): ${take_price:.2f}")
            lines.append("• " + "  ·  ".join(parts))
        advice = data.get("entry_advice")
        advice_reason = data.get("entry_advice_reason")
        if advice in ("CAUTION", "AVOID") and advice_reason:
            lines.append("")
            lines.append(f"⚠️ **Вход:** {advice} — _{_escape_markdown(advice_reason)}_")
        pm_rec = data.get("premarket_entry_recommendation")
        if pm_rec:
            lines.append("")
            lines.append(f"📋 **Премаркет:** _{_escape_markdown(pm_rec[:200])}_")
        curv = data.get("curvature_5m_pct")
        bounce_to_high = data.get("possible_bounce_to_high_pct")
        est_bounce = data.get("estimated_bounce_pct")
        if curv is not None or bounce_to_high is not None:
            parts = []
            if curv is not None:
                parts.append(f"Кривизна 5m: {curv:+.3f}%" + (" (разворот вверх)" if curv > 0 else ""))
            if bounce_to_high is not None:
                parts.append(f"До хая сессии: +{bounce_to_high:.2f}%")
            if est_bounce is not None:
                parts.append(f"Оценка подъёма (по кривизне): ~+{est_bounce:.2f}%")
            lines.append("")
            lines.append("**График / возможный подъём:** " + "  ·  ".join(parts))
        if has_pos and pos:
            pnl = pos.get("pnl") or 0
            pnl_pct = pos.get("pnl_pct") or 0
            lines.append(f"\n_Позиция: P&L ${pnl:,.2f} ({pnl_pct:+.2f}%)_")
        if data.get("reasoning"):
            lines.append(f"\n💭 _{_escape_markdown(str(data['reasoning'])[:220])}_")
        llm_insight = data.get("llm_insight")
        llm_content = (data.get("llm_news_content") or "").strip()[:350]
        if llm_insight:
            lines.append("")
            lines.append(f"📰 **LLM (свежие новости/настроения):** _{_escape_markdown(llm_insight)}_")
        elif llm_content:
            lines.append("")
            lines.append(f"📰 **LLM:** _{_escape_markdown(llm_content)}…_")
        alex = data.get("alex_rule")
        if alex and alex.get("message"):
            lines.append("")
            lines.append(f"📋 _{_escape_markdown(alex['message'])}_")
        return "\n".join([s for s in lines if s])

    def _get_execution_agent(self):
        """Ленивая инициализация ExecutionAgent для песочницы."""
        if getattr(self, "_execution_agent", None) is None:
            try:
                from execution_agent import ExecutionAgent
                self._execution_agent = ExecutionAgent()
            except Exception as e:
                logger.warning(f"ExecutionAgent недоступен: {e}")
                self._execution_agent = False
        return self._execution_agent if self._execution_agent else None

    async def _handle_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Портфель: cash, позиции, текущая оценка и P&L."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна (не инициализирован ExecutionAgent).")
            return
        try:
            summary = agent.get_portfolio_summary()
            cash = summary["cash"]
            total = summary["total_equity"]
            lines = [f"💵 **Кэш:** ${cash:,.2f}", f"📊 **Итого (оценка):** ${total:,.2f}"]
            for p in summary["positions"]:
                pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"
                lines.append(
                    f"\n{pnl_emoji} **{_escape_markdown(p['ticker'])}** — {p['quantity']:.0f} шт.\n"
                    f"  Вход: ${p['entry_price']:.2f} → Сейчас: ${p['current_price']:.2f}\n"
                    f"  P&L: ${p['pnl']:,.2f} ({p['pnl_pct']:+.2f}%)"
                )
            if not summary["positions"]:
                lines.append("\n_Позиций нет. /buy <ticker> <кол-во>_")
            await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка портфеля: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def _handle_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Виртуальная покупка: /buy <ticker> <кол-во>."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "❌ Формат: `/buy <ticker> <кол-во>`\nПример: `/buy GC=F 5` или `/buy MSFT 10`",
                parse_mode='Markdown',
            )
            return
        ticker = _normalize_ticker(context.args[0])
        try:
            qty = float(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Укажите число в качестве количества.")
            return
        ok, msg = agent.execute_manual_buy(ticker, qty)
        await update.message.reply_text(msg if ok else f"❌ {msg}")

    async def _handle_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Виртуальная продажа: /sell <ticker> [кол-во]. Без кол-ва — закрыть всю позицию."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        if not context.args or len(context.args) < 1:
            await update.message.reply_text(
                "❌ Формат: `/sell <ticker>` или `/sell <ticker> <кол-во>`\nПример: `/sell GC=F` или `/sell MSFT 5`",
                parse_mode='Markdown',
            )
            return
        ticker = _normalize_ticker(context.args[0])
        qty = None
        if len(context.args) >= 2:
            try:
                qty = float(context.args[1])
            except ValueError:
                await update.message.reply_text("❌ Укажите число в качестве количества.")
                return
        ok, msg = agent.execute_manual_sell(ticker, qty)
        await update.message.reply_text(msg if ok else f"❌ {msg}")

    async def _handle_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Последние сделки: /history [тикер] [N] — без аргументов все сделки; с тикером только по этому тикеру."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        limit = 15
        ticker = None
        args = (context.args or [])[:2]
        if args:
            first = args[0].strip().upper()
            try:
                n = int(first)
                limit = min(n, 50)
            except ValueError:
                ticker = _normalize_ticker(first)
                if len(args) >= 2:
                    try:
                        limit = min(int(args[1].strip()), 50)
                    except ValueError:
                        pass
        try:
            rows = agent.get_trade_history(limit=limit, ticker=ticker)
            if not rows:
                msg = "История сделок пуста." if not ticker else f"По тикеру {ticker} сделок нет."
                await update.message.reply_text(msg)
                return
            title = f"📜 **Последние сделки**" + (f" ({ticker})" if ticker else "") + ":"
            lines = [title]
            for r in rows:
                ts = r["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(r["ts"], "strftime") else str(r["ts"])
                side = "🟢" if r["side"] == "BUY" else "🔴"
                strat = r.get("strategy_name", "—")
                lines.append(f"{side} {ts} — {r['side']} {r['ticker']} x{r['quantity']:.0f} @ ${r['price']:.2f} ({r['signal_type']}) [{strat}]")
            if rows and ticker:
                lines.append("")
                lines.append(f"📈 _Сделки на графике:_ `/chart5m {ticker} 7` или `/chart {ticker} 7`")
            await update.message.reply_text("\n".join(lines), parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Ошибка history: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

    async def _handle_recommend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Рекомендация: когда открыть позицию и какие параметры управления (стоп-лосс, размер)."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        ticker = None
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        if not ticker:
            await update.message.reply_text(
                "Укажите тикер для рекомендации.\n"
                "Пример: `/recommend SNDK` или `/recommend GC=F`\n\n"
                "Можно спросить текстом: _когда можно открыть позицию по SNDK и какие параметры советуешь?_",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text("🔍 Готовлю рекомендацию...")
        data = self._get_recommendation_data(ticker)
        if not data:
            await update.message.reply_text(f"❌ Не удалось получить рекомендацию для {ticker}. Проверьте тикер и данные в БД.")
            return
        try:
            text = self._format_recommendation(data)
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.exception("Ошибка форматирования рекомендации")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _handle_recommend5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Рекомендация по 5-минутным данным с учётом 5-дневной статистики (агрессивный интрадей, напр. SNDK)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        ticker = "SNDK"
        days = 5
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        if len(context.args) >= 2:
            try:
                days = max(1, min(7, int(context.args[1].strip())))
            except (ValueError, IndexError):
                pass
        await update.message.reply_text(f"📥 Загрузка 5m данных для {ticker} за {days} дн....")
        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, self._get_recommendation_data_5m, ticker, days
            )
        except Exception as e:
            logger.exception("Ошибка рекомендации 5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return
        if not data:
            await update.message.reply_text(
                f"❌ Нет 5m данных для {ticker} за {days} дн. Yahoo даёт 5m обычно за 1–7 дней. "
                "Попробуйте: /recommend5m SNDK 1 или /recommend5m SNDK 7. В выходные биржа закрыта — данных может не быть."
            )
            return
        try:
            text = self._format_recommendation_5m(data)
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.exception("Ошибка форматирования рекомендации 5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")

    async def _handle_game5m(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Мониторинг игры 5m: открытая позиция, закрытые сделки, win rate и PnL (только просмотр, сделками управляет send_sndk_signal_cron)."""
        if not self._check_access(update.effective_user.id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        ticker = "SNDK"
        if context.args and len(context.args) >= 1:
            ticker = _normalize_ticker(context.args[0])
        limit = 15
        if len(context.args) >= 2:
            try:
                limit = max(5, min(30, int(context.args[1].strip())))
            except (ValueError, IndexError):
                pass

        def _fetch_game5m():
            from services.game_5m import get_open_position, get_recent_results, get_strategy_params
            pos = get_open_position(ticker)
            results = get_recent_results(ticker, limit=limit)
            params = get_strategy_params()
            return pos, results, params

        loop = asyncio.get_event_loop()
        try:
            pos, results, params = await loop.run_in_executor(None, _fetch_game5m)
        except Exception as e:
            logger.exception("Ошибка загрузки игры 5m")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return

        lines = [f"📊 **Игра 5m — {_escape_markdown(ticker)}** (мониторинг)", ""]
        lines.append(f"Параметры: стоп −{params['stop_loss_pct']}%, тейк +{params['take_profit_pct']}%, макс. {params['max_position_days']} дн. _(config.env)_")
        lines.append("")
        if pos:
            entry_ts = pos.get("entry_ts")
            ts_str = str(entry_ts)[:16] if entry_ts else "—"
            lines.append(f"🟢 **Открытая позиция**")
            lines.append(f"Вход: {ts_str} @ ${pos['entry_price']:.2f} · {pos['quantity']:.0f} шт. · сигнал {pos.get('entry_signal_type', '—')}")
            lines.append("")
        else:
            lines.append("_Нет открытой позиции_")
            lines.append("")

        if not results:
            lines.append("_Закрытых сделок пока нет._")
        else:
            pnls = [r["pnl_pct"] for r in results if r.get("pnl_pct") is not None]
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            win_rate = (100.0 * wins / total) if total else 0
            avg_pnl = (sum(pnls) / total) if total else 0
            lines.append(f"**Закрытые сделки (последние {len(results)}):**")
            lines.append(f"Win rate: {wins}/{total} ({win_rate:.1f}%) · Средний PnL: {avg_pnl:+.2f}%")
            lines.append("")
            for r in results[:8]:
                exit_ts = r.get("exit_ts") or "—"
                exit_str = str(exit_ts)[:16] if exit_ts != "—" else "—"
                pct = r.get("pnl_pct")
                pct_str = f"{pct:+.2f}%" if pct is not None else "—"
                lines.append(f"• {exit_str} {r.get('exit_signal_type', '—')} PnL {pct_str}")
            if len(results) > 8:
                lines.append(f"_… и ещё {len(results) - 8} сделок_")
        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик произвольных текстовых сообщений"""
        # В группах игнорируем текстовые сообщения без упоминания
        # Используйте команду /ask для вопросов в группах
        if update.message.chat.type in ('group', 'supergroup'):
            return
        
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        text = update.message.text.strip()
        await self._process_query(update, text)
        
    async def _handle_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /ask <вопрос>"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        if not context.args or len(context.args) == 0:
            await update.message.reply_text(
                "❌ Задайте вопрос после команды\n"
                "Примеры:\n"
                "`/ask какая цена золота`\n"
                "`/ask какие новости по MSFT`\n"
                "`/ask анализ GBPUSD`",
                parse_mode='Markdown'
            )
            return
        
        # Объединяем все аргументы в один текст
        text = ' '.join(context.args).strip()
        logger.info(f"Обработка команды /ask: '{text}'")
        
        # Используем общую логику обработки запросов
        await self._process_query(update, text)
    
    async def _process_query(self, update: Update, text: str):
        """Общая логика обработки запросов (используется в /ask и текстовых сообщениях)"""
        logger.info(f"Обработка запроса: '{text}'")
        
        try:
            # Определяем тип запроса по ключевым словам
            text_lower = text.lower()
            is_news_query = any(word in text_lower for word in ['новости', 'новость', 'news', 'новостей', 'что пишут', 'что пишут про'])
            is_price_query = any(word in text_lower for word in ['цена', 'price', 'стоимость', 'стоит', 'сколько', 'какая цена', 'какая стоимость'])
            # Расширяем ключевые слова для анализа: "что с", "как дела", "ситуация" и т.д.
            is_analysis_query = any(word in text_lower for word in [
                'анализ', 'analysis', 'сигнал', 'signal', 'прогноз', 'forecast',
                'что с', 'как дела', 'ситуация', 'тренд', 'trend', 'рекомендация'
            ])
            is_recommendation_query = any(phrase in text_lower for phrase in [
                'когда можно открыть', 'когда открыть позицию', 'когда купить', 'когда войти',
                'какие параметры', 'параметры управления', 'что советуешь', 'какой стоп',
                'стоп-лосс', 'стейк-лосс', 'рекомендуй вход', 'можно ли открыть позицию'
            ])
            
            logger.info(f"Тип запроса: news={is_news_query}, price={is_price_query}, analysis={is_analysis_query}, recommend={is_recommendation_query}")
            
            # Пытаемся извлечь все тикеры из текста (может быть несколько)
            tickers = self._extract_all_tickers_from_text(text)
            logger.info(f"Извлечённые тикеры из текста '{text}': {tickers}")
            
            # Вопрос про вход в позицию и параметры управления — даём рекомендацию по тикеру
            if is_recommendation_query:
                rec_ticker = _normalize_ticker(tickers[0]) if tickers else None
                if not rec_ticker:
                    await update.message.reply_text(
                        "Укажите инструмент в вопросе, например:\n"
                        "• _когда можно открыть позицию по SNDK и какие параметры советуешь?_\n"
                        "• _рекомендуй параметры управления для GC=F_",
                        parse_mode="Markdown",
                    )
                    return
                await update.message.reply_text(f"🔍 Готовлю рекомендацию по {rec_ticker}...")
                data = self._get_recommendation_data(rec_ticker)
                if not data:
                    await update.message.reply_text(f"❌ Не удалось получить данные для {rec_ticker}.")
                    return
                recommendation_text = self._format_recommendation(data)
                if self.llm_service and recommendation_text:
                    try:
                        system_prompt = (
                            "Ты помощник по виртуальной торговле. Пользователь задаёт вопрос о том, когда открыть позицию и какие параметры управления использовать. "
                            "Ответь кратко и по делу на русском, опираясь ТОЛЬКО на приведённые данные. Упомяни: стоит ли открывать позицию сейчас, стоп-лосс, размер позиции. "
                            "Не придумывай цифры — используй только данные из контекста."
                        )
                        ctx = (
                            f"Данные для ответа:\n{recommendation_text}\n\n"
                            f"Вопрос пользователя: {text}"
                        )
                        result = self.llm_service.generate_response(
                            messages=[{"role": "user", "content": ctx}],
                            system_prompt=system_prompt,
                            temperature=0.3,
                            max_tokens=400,
                        )
                        answer = (result.get("response") or "").strip()
                        if answer:
                            await update.message.reply_text(answer, parse_mode="Markdown")
                            return
                    except Exception as e:
                        logger.warning(f"LLM для рекомендации не сработал: {e}")
                await update.message.reply_text(recommendation_text, parse_mode="Markdown")
                return
            
            if tickers:
                # Если найдено несколько тикеров и это запрос новостей - собираем все новости и выбираем топ N
                if is_news_query and len(tickers) > 1:
                    # Извлекаем количество новостей из запроса (если указано)
                    import re
                    count_match = re.search(r'(\d+)\s*(самые|топ|top|последние|важные)', text_lower)
                    top_n = int(count_match.group(1)) if count_match else 10
                    
                    await update.message.reply_text(f"📰 Поиск {top_n} самых важных новостей для {len(tickers)} инструментов...")
                    
                    # Собираем все новости по всем тикерам
                    import pandas as pd
                    all_news = []
                    ticker_names = []
                    
                    news_timeout_per_ticker = max(20, 60 // max(1, len(tickers)))
                    for ticker in tickers:
                        ticker = _normalize_ticker(ticker)
                        ticker_names.append(ticker)
                        try:
                            news_df = await self._get_recent_news_async(ticker, timeout=news_timeout_per_ticker)
                        except asyncio.TimeoutError:
                            logger.warning(f"Таймаут новостей для {ticker}, пропускаем")
                            continue
                        if not news_df.empty:
                            # Добавляем колонку с тикером для идентификации
                            news_df = news_df.copy()
                            news_df['ticker'] = ticker
                            all_news.append(news_df)
                    
                    if all_news:
                        # Объединяем все новости
                        combined_news = pd.concat(all_news, ignore_index=True)
                        
                        # Сортируем по важности:
                        # 1. Приоритет NEWS и EARNINGS над ECONOMIC_INDICATOR
                        # 2. По sentiment (более сильный sentiment = важнее)
                        # 3. По дате (более свежие = важнее)
                        def importance_score(row):
                            score = 0
                            # Приоритет типов событий
                            event_type = str(row.get('event_type', '')).upper()
                            if event_type == 'NEWS':
                                score += 1000
                            elif event_type == 'EARNINGS':
                                score += 800
                            elif event_type == 'ECONOMIC_INDICATOR':
                                score += 100
                            
                            # Sentiment (чем дальше от 0.5, тем важнее)
                            sentiment = row.get('sentiment_score', 0.5)
                            if sentiment is not None and not pd.isna(sentiment):
                                score += abs(sentiment - 0.5) * 500
                            
                            return score
                        
                        combined_news['importance'] = combined_news.apply(importance_score, axis=1)
                        combined_news = combined_news.sort_values('importance', ascending=False)
                        
                        # Берем топ N
                        top_news = combined_news.head(top_n)
                        
                        # Форматируем ответ
                        response = f"📰 **Топ {top_n} самых важных новостей** ({', '.join(ticker_names)}):\n\n"
                        
                        for idx, row in top_news.iterrows():
                            ticker = row.get('ticker', 'N/A')
                            ts = row.get('ts', '')
                            source = _escape_markdown(row.get('source') or '—')
                            event_type = _escape_markdown(row.get('event_type') or '')
                            content = row.get('content') or row.get('insight') or ''
                            if content:
                                preview = _escape_markdown(str(content)[:200])
                            else:
                                preview = "(без текста)"
                            
                            sentiment = row.get('sentiment_score')
                            sentiment_str = ""
                            if sentiment is not None and not pd.isna(sentiment):
                                if sentiment > 0.6:
                                    sentiment_str = " 📈"
                                elif sentiment < 0.4:
                                    sentiment_str = " 📉"
                            
                            date_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
                            prefix = "Ожидается отчёт:" if event_type == "EARNINGS" else ""
                            type_str = f" [{event_type}]" if event_type else ""
                            response += f"**{ticker}** - {prefix}{date_str}{sentiment_str}\n🔹 {source}{type_str}\n{preview}\n\n"
                        
                        try:
                            await update.message.reply_text(response, parse_mode='Markdown')
                        except Exception:
                            await update.message.reply_text(response)
                    else:
                        await update.message.reply_text(f"ℹ️ Не найдено новостей для {', '.join(ticker_names)}")
                elif len(tickers) == 1:
                    # Один тикер - обрабатываем как обычно
                    ticker = _normalize_ticker(tickers[0])
                    
                    if is_news_query:
                        # Извлекаем количество новостей из запроса (если указано)
                        import re
                        count_match = re.search(r'(\d+)\s*(самые|топ|top|последние)', text_lower)
                        top_n = int(count_match.group(1)) if count_match else 10
                        
                        # Запрос новостей
                        await update.message.reply_text(f"📰 Поиск новостей для {ticker}...")
                        try:
                            news_df = await self._get_recent_news_async(ticker, timeout=30)
                        except asyncio.TimeoutError:
                            await update.message.reply_text(
                                f"❌ Таймаут при получении новостей для {ticker}. Попробуйте позже."
                            )
                            return
                        response = self._format_news_response(ticker, news_df, top_n=top_n)
                        try:
                            await update.message.reply_text(response, parse_mode='Markdown')
                        except Exception:
                            await update.message.reply_text(response)
                    elif is_price_query:
                        # Запрос цены
                        await self._handle_price_by_ticker(update, ticker)
                    else:
                        # Полный анализ (по умолчанию, если найден тикер)
                        logger.info(f"Выполняем полный анализ для {ticker}")
                        await update.message.reply_text(f"🔍 Анализ {ticker}...")
                        
                        try:
                            decision_result = self.analyst.get_decision_with_llm(ticker)
                            logger.info(f"Получен результат анализа для {ticker}: {decision_result.get('decision')}")
                            response = self._format_signal_response(ticker, decision_result)
                            
                            try:
                                await update.message.reply_text(response, parse_mode='Markdown')
                            except Exception as e:
                                logger.warning(f"Ошибка отправки Markdown, отправляем без форматирования: {e}")
                                await update.message.reply_text(response)
                        except Exception as e:
                            logger.error(f"Ошибка при анализе {ticker}: {e}", exc_info=True)
                            await update.message.reply_text(f"❌ Ошибка при анализе {ticker}: {str(e)}")
                else:
                    # Несколько тикеров, но не новости - анализируем каждый
                    await update.message.reply_text(f"🔍 Анализ {len(tickers)} инструментов...")
                    
                    all_responses = []
                    for ticker in tickers:
                        ticker = _normalize_ticker(ticker)
                        try:
                            decision_result = self.analyst.get_decision_with_llm(ticker)
                            response = self._format_signal_response(ticker, decision_result)
                            all_responses.append(response)
                        except Exception as e:
                            logger.error(f"Ошибка при анализе {ticker}: {e}")
                            all_responses.append(f"❌ Ошибка при анализе {ticker}: {str(e)}")
                    
                    combined_response = "\n\n" + "="*40 + "\n\n".join(all_responses)
                    try:
                        await update.message.reply_text(combined_response, parse_mode='Markdown')
                    except Exception:
                        await update.message.reply_text(combined_response)
            else:
                # Тикер не найден - пробуем использовать LLM для понимания вопроса
                if self.llm_service:
                    logger.info("Тикер не найден, используем LLM для понимания вопроса")
                    await update.message.reply_text("🤖 Анализирую вопрос...")
                    
                    try:
                        # Пытаемся понять вопрос через LLM и найти тикер
                        llm_response = await self._ask_llm_about_ticker(update, text)
                        if llm_response:
                            try:
                                await update.message.reply_text(llm_response, parse_mode='Markdown')
                            except Exception:
                                await update.message.reply_text(llm_response)
                            return
                    except Exception as e:
                        logger.error(f"Ошибка при обращении к LLM: {e}", exc_info=True)
                
                # Fallback: ищем в Vector KB похожие события
                await update.message.reply_text("🔍 Поиск в базе знаний...")
                
                similar = self.vector_kb.search_similar(
                    query=text,
                    limit=3,
                    min_similarity=0.4
                )
                
                if similar.empty:
                    await update.message.reply_text(
                        "ℹ️ Не найдено релевантной информации.\n"
                        "Попробуйте указать тикер, например: GC=F или GBPUSD=X"
                    )
                else:
                    response = f"📚 **Найдено похожих событий:**\n\n"
                    for idx, row in similar.iterrows():
                        response += f"• {row.get('ticker', 'N/A')}: {row.get('content', '')[:100]}...\n"
                        response += f"  Similarity: {row.get('similarity', 0):.2f}\n\n"
                    
                    try:
                        await update.message.reply_text(response, parse_mode='Markdown')
                    except Exception:
                        await update.message.reply_text(response)
        
        except Exception as e:
            logger.error(f"Ошибка обработки запроса '{text}': {e}", exc_info=True)
            try:
                await update.message.reply_text(
                    f"❌ Ошибка обработки запроса: {str(e)}\n\n"
                    "Попробуйте использовать команды:\n"
                    "/ask <вопрос>\n"
                    "/signal <ticker>\n"
                    "/news <ticker>"
                )
            except Exception as send_err:
                logger.error(f"Ошибка отправки сообщения об ошибке: {send_err}")
    
    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик callback queries (для inline кнопок)"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        if not self._check_access(user_id):
            await query.edit_message_text("❌ Доступ запрещен")
            return
        
        # Обработка callback data
        data = query.data
        # Можно добавить логику для кнопок позже
    
    def _format_signal_response(self, ticker: str, decision_result: Dict[str, Any]) -> str:
        """Форматирует ответ с анализом сигнала"""
        decision = decision_result.get('decision', 'HOLD')
        technical_signal = decision_result.get('technical_signal', 'N/A')
        # Получаем sentiment (может быть в разных форматах)
        sentiment = decision_result.get('sentiment_normalized') or decision_result.get('sentiment', 0.0)
        if isinstance(sentiment, (int, float)):
            if 0.0 <= sentiment <= 1.0:
                # Конвертируем из 0.0-1.0 в -1.0-1.0
                sentiment = (sentiment - 0.5) * 2.0
        else:
            sentiment = 0.0
        strategy = decision_result.get('selected_strategy') or 'N/A'
        news_count = decision_result.get('news_count', 0)
        
        # Получаем текущую цену и RSI; при отсутствии RSI — считаем локально по close
        from sqlalchemy import create_engine, text
        from config_loader import get_database_url
        from services.rsi_calculator import get_or_compute_rsi
        
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker}
            )
            row = result.fetchone()
            if not row:
                logger.warning(f"Нет данных в quotes для {ticker}")
                price = "N/A"
                rsi = None
            else:
                price = f"${row[0]:.2f}" if row[0] is not None else "N/A"
                rsi = row[1] if row[1] is not None else None
        if rsi is None:
            rsi = get_or_compute_rsi(engine, ticker)
        
        # Эмодзи для решения
        decision_emoji = {
            'STRONG_BUY': '🟢',
            'BUY': '🟡',
            'HOLD': '⚪',
            'SELL': '🔴'
        }.get(decision, '⚪')
        
        # Эмодзи для sentiment
        if sentiment > 0.3:
            sentiment_emoji = '📈'
            sentiment_label = 'положительный'
        elif sentiment < -0.3:
            sentiment_emoji = '📉'
            sentiment_label = 'отрицательный'
        else:
            sentiment_emoji = '➡️'
            sentiment_label = 'нейтральный'
        
        # RSI: берём из ответа аналитика, если есть, иначе из БД уже подтянули выше
        rsi_to_show = rsi
        if rsi_to_show is None:
            rsi_to_show = (decision_result.get("technical_data") or {}).get("rsi")
        # Форматируем RSI — строка всегда есть (либо значение, либо "нет данных")
        if rsi_to_show is not None:
            if rsi_to_show >= 70:
                rsi_emoji = "🔴"
                rsi_status = "перекупленность"
            elif rsi_to_show <= 30:
                rsi_emoji = "🟢"
                rsi_status = "перепроданность"
            elif rsi_to_show >= 60:
                rsi_emoji = "🟡"
                rsi_status = "близко к перекупленности"
            elif rsi_to_show <= 40:
                rsi_emoji = "🟡"
                rsi_status = "близко к перепроданности"
            else:
                rsi_emoji = "⚪"
                rsi_status = "нейтральная зона"
            rsi_text = f"\n{rsi_emoji} **RSI:** {rsi_to_show:.1f} ({rsi_status})"
        else:
            # Локальный расчёт уже пробовали (get_or_compute_rsi); нет данных = мало истории close
            rsi_hint = "недостаточно данных (нужно 15 дней close) или запустите update_prices.py"
            rsi_text = f"\n⚪ **RSI:** нет данных ({rsi_hint})"
        
        # Экранируем ticker для Markdown (GBPUSD=X содержит =)
        ticker_escaped = _escape_markdown(ticker)
        
        response = f"""
{decision_emoji} **{ticker_escaped}** - {decision}

💰 **Цена:** {price}{rsi_text}
📊 **Технический сигнал:** {technical_signal}
{sentiment_emoji} **Sentiment:** {sentiment:.2f} ({sentiment_label})
📋 **Стратегия:** {strategy}
📰 **Новостей:** {news_count}
        """
        
        # Добавляем reasoning если есть (экранируем)
        if decision_result.get('reasoning'):
            reasoning_escaped = _escape_markdown(str(decision_result.get('reasoning')[:200]))
            response += f"\n💭 **Обоснование:**\n{reasoning_escaped}..."
        
        return response.strip()
    
    def _format_news_response(self, ticker: str, news_df, top_n: int = 10) -> str:
        """Форматирует ответ с новостями. top_n — сколько записей показать. Шум (календарные числа) скрыт."""
        def _is_noise(row) -> bool:
            """Запись — шум: ECONOMIC_INDICATOR с контентом в виде короткого числа (19.60M и т.п.)."""
            if row.get('event_type') != 'ECONOMIC_INDICATOR':
                return False
            raw = row.get('content') or row.get('insight') or ''
            if raw is None or (isinstance(raw, float) and str(raw) == 'nan'):
                return True
            text = str(raw).strip()
            if len(text) > 50 or ' ' in text:
                return False
            return True

        display_df = news_df[~news_df.apply(_is_noise, axis=1)].reset_index(drop=True)
        total_display = len(display_df)
        if total_display == 0:
            return (
                f"📰 **Новости для {_escape_markdown(ticker)}** (последние 7 дней)\n\n"
                "Нет новостей с текстом. В выборке только записи календаря без описания."
            )
        response = (
            f"📰 **Новости для {_escape_markdown(ticker)}** (последние 7 дней, топ {top_n})\n"
            "_sentiment: 0–1 (0=негатив, 0.5=нейтр., 1=позитив)_\n\n"
        )

        def _content_preview(row) -> str:
            raw = (row.get('content') or row.get('insight') or '')
            if raw is None or (isinstance(raw, float) and str(raw) == 'nan'):
                raw = ''
            text = str(raw).strip()
            event = row.get('event_type')
            if len(text) <= 30 and text and ' ' not in text:
                prefix = f"[{event}] " if event else ""
                return f"{prefix}{text}"
            return text[:250] if len(text) > 250 else text

        shown = 0
        for idx, row in display_df.iterrows():
            if shown >= top_n:
                break
            ts = row.get('ts', '')
            source = _escape_markdown(row.get('source') or '—')
            event_type = _escape_markdown(row.get('event_type') or '')
            preview = _escape_markdown(_content_preview(row))
            if not preview:
                preview = "(без текста)"
            sentiment = row.get('sentiment_score')
            sentiment_str = ""
            if sentiment is not None and not (isinstance(sentiment, float) and math.isnan(sentiment)):
                if sentiment > 0.6:
                    sentiment_str = " 📈"
                elif sentiment < 0.4:
                    sentiment_str = " 📉"
                # Числовое значение для проверки (сетка 0.0–1.0: 0=негатив, 0.5=нейтр., 1=позитив)
                sentiment_str += f" ({float(sentiment):.2f})"
            date_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
            # EARNINGS: ts = дата отчёта (ожидаемая), не дата публикации
            prefix = "Ожидается отчёт:" if event_type == "EARNINGS" else "📅"
            type_str = f" [{event_type}]" if event_type else ""
            response += f"{prefix} {date_str}{sentiment_str}\n🔹 **{source}**{type_str}\n{preview}\n"
            # Insight от LLM (начало) — для проверки в боте
            insight_val = row.get('insight')
            if insight_val and isinstance(insight_val, str) and insight_val.strip():
                insight_esc = _escape_markdown(insight_val.strip()[:100])
                if insight_esc:
                    response += f"💭 _{insight_esc}_\n"
            response += "\n"
            shown += 1

        if total_display > shown:
            response += f"\n... и еще {total_display - shown} записей"
        if len(display_df) < len(news_df):
            response += f"\n_{_escape_markdown(f'скрыто записей календаря без текста: {len(news_df) - len(display_df)}')}_"
        return response
    
    def _extract_ticker_from_text(self, text: str) -> Optional[str]:
        """Пытается извлечь ticker из текста, включая естественные названия"""
        text_upper = text.upper()
        text_lower = text.lower()
        
        # Маппинг естественных названий на тикеры
        natural_names = {
            # Товары
            'золото': 'GC=F',
            'gold': 'GC=F',
            'золота': 'GC=F',
            'золотом': 'GC=F',
            'золоте': 'GC=F',
            'золоту': 'GC=F',  # дательный падеж
            'золот': 'GC=F',   # родительный падеж множественного числа
            
            # Валютные пары
            'gbpusd': 'GBPUSD=X',
            'gbp/usd': 'GBPUSD=X',
            'gbp-usd': 'GBPUSD=X',
            'gbp usd': 'GBPUSD=X',
            'фунт': 'GBPUSD=X',
            'фунта': 'GBPUSD=X',
            'фунтом': 'GBPUSD=X',
            'фунте': 'GBPUSD=X',
            'фунту': 'GBPUSD=X',  # дательный падеж
            'фунт-доллар': 'GBPUSD=X',
            'фунт доллар': 'GBPUSD=X',
            'gbp': 'GBPUSD=X',  # короткое название
            
            'eurusd': 'EURUSD=X',
            'eur/usd': 'EURUSD=X',
            'eur-usd': 'EURUSD=X',
            'eur usd': 'EURUSD=X',
            'евро': 'EURUSD=X',
            'евро-доллар': 'EURUSD=X',
            'евро доллар': 'EURUSD=X',
            
            'usdjpy': 'USDJPY=X',
            'usd/jpy': 'USDJPY=X',
            'usd-jpy': 'USDJPY=X',
            'usd jpy': 'USDJPY=X',
            'йена': 'USDJPY=X',
            'йены': 'USDJPY=X',
            
            # Акции
            'microsoft': 'MSFT',
            'микрософт': 'MSFT',
            'sandisk': 'SNDK',
            'сандиск': 'SNDK',
        }
        
        # Проверяем естественные названия (сначала более длинные совпадения)
        # Сортируем по длине в обратном порядке, чтобы сначала проверять более длинные фразы
        sorted_names = sorted(natural_names.items(), key=lambda x: len(x[0]), reverse=True)
        for name, ticker in sorted_names:
            if name in text_lower:
                logger.debug(f"Найдено совпадение '{name}' -> {ticker} в тексте '{text_lower}'")
                return ticker
        
        # Известные тикеры
        known_tickers = [
            'GC=F', 'GBPUSD=X', 'EURUSD=X', 'USDJPY=X',
            'MSFT', 'SNDK', 'MU', 'LITE', 'ALAB', 'TER'
        ]
        
        for ticker in known_tickers:
            if ticker in text_upper:
                return ticker
        
        # Пытаемся найти паттерн тикера (3-5 заглавных букв)
        import re
        match = re.search(r'\b([A-Z]{2,5}(?:=X|=F)?)\b', text_upper)
        if match:
            return match.group(1)
        
        return None
    
    async def _ask_llm_about_ticker(self, update: Update, question: str) -> Optional[str]:
        """Использует LLM для понимания вопроса и поиска тикера"""
        if not self.llm_service:
            return None
        
        system_prompt = """Ты помощник для торгового бота. Твоя задача - понять вопрос пользователя о финансовых инструментах и определить, о каком инструменте идёт речь.

Доступные инструменты:
- Золото: GC=F (также "золото", "gold")
- Валютные пары: GBPUSD=X (фунт, GBP), EURUSD=X (евро, EUR), USDJPY=X (йена, JPY)
- Акции: MSFT (Microsoft), SNDK (Sandisk) и другие

Если пользователь спрашивает про инструмент, определи тикер и ответь в формате:
ТИКЕР: <тикер>
ОПИСАНИЕ: <краткое описание что это>

Если не можешь определить тикер, ответь:
НЕИЗВЕСТНО

Примеры:
- "что с фунтом" -> ТИКЕР: GBPUSD=X
- "какая цена золота" -> ТИКЕР: GC=F
- "новости по Microsoft" -> ТИКЕР: MSFT"""

        try:
            result = self.llm_service.generate_response(
                messages=[{"role": "user", "content": question}],
                system_prompt=system_prompt,
                temperature=0.1,
                max_tokens=200
            )
            
            response = result.get("response", "").strip()
            logger.info(f"LLM ответ на вопрос '{question}': {response}")
            
            # Пытаемся извлечь тикер из ответа LLM
            ticker_match = re.search(r'ТИКЕР:\s*([A-Z0-9=]+)', response, re.IGNORECASE)
            if ticker_match:
                ticker = ticker_match.group(1).upper()
                logger.info(f"LLM определил тикер: {ticker}")
                
                # Нормализуем тикер
                ticker = _normalize_ticker(ticker)
                
                # Выполняем анализ для найденного тикера
                decision_result = self.analyst.get_decision_with_llm(ticker)
                response = self._format_signal_response(ticker, decision_result)
                
                return response
            else:
                # LLM не смог определить тикер
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при обращении к LLM: {e}", exc_info=True)
            return None
    
    def _extract_all_tickers_from_text(self, text: str) -> list:
        """Извлекает все тикеры из текста (может быть несколько)"""
        text_upper = text.upper()
        text_lower = text.lower()
        
        found_tickers = []
        found_names = set()  # Чтобы не дублировать
        
        # Маппинг естественных названий на тикеры
        natural_names = {
            # Товары
            'золото': 'GC=F',
            'gold': 'GC=F',
            'золота': 'GC=F',
            'золотом': 'GC=F',
            'золоте': 'GC=F',
            'золоту': 'GC=F',
            'золот': 'GC=F',
            
            # Валютные пары
            'gbpusd': 'GBPUSD=X',
            'gbp/usd': 'GBPUSD=X',
            'gbp-usd': 'GBPUSD=X',
            'gbp usd': 'GBPUSD=X',
            'фунт': 'GBPUSD=X',
            'фунта': 'GBPUSD=X',
            'фунтом': 'GBPUSD=X',
            'фунте': 'GBPUSD=X',
            'фунту': 'GBPUSD=X',
            'фунт-доллар': 'GBPUSD=X',
            'фунт доллар': 'GBPUSD=X',
            'gbp': 'GBPUSD=X',
            
            'eurusd': 'EURUSD=X',
            'eur/usd': 'EURUSD=X',
            'eur-usd': 'EURUSD=X',
            'eur usd': 'EURUSD=X',
            'евро': 'EURUSD=X',
            'евро-доллар': 'EURUSD=X',
            'евро доллар': 'EURUSD=X',
            
            'usdjpy': 'USDJPY=X',
            'usd/jpy': 'USDJPY=X',
            'usd-jpy': 'USDJPY=X',
            'usd jpy': 'USDJPY=X',
            'йена': 'USDJPY=X',
            'йены': 'USDJPY=X',
            
            # Акции
            'microsoft': 'MSFT',
            'микрософт': 'MSFT',
            'sandisk': 'SNDK',
            'сандиск': 'SNDK',
        }
        
        # Проверяем естественные названия (сначала более длинные фразы)
        sorted_names = sorted(natural_names.items(), key=lambda x: len(x[0]), reverse=True)
        for name, ticker in sorted_names:
            if name in text_lower and name not in found_names:
                found_tickers.append(ticker)
                found_names.add(name)
                logger.debug(f"Найдено совпадение '{name}' -> {ticker} в тексте '{text_lower}'")
        
        # Известные тикеры
        known_tickers = [
            'GC=F', 'GBPUSD=X', 'EURUSD=X', 'USDJPY=X',
            'MSFT', 'SNDK', 'MU', 'LITE', 'ALAB', 'TER'
        ]
        
        for ticker in known_tickers:
            if ticker in text_upper and ticker not in found_tickers:
                found_tickers.append(ticker)
        
        # Пытаемся найти паттерн тикера (3-5 заглавных букв)
        import re
        matches = re.findall(r'\b([A-Z]{2,5}(?:=X|=F)?)\b', text_upper)
        for match in matches:
            if match not in found_tickers:
                found_tickers.append(match)
        
        return found_tickers
    
    def _split_long_message(self, text: str, max_length: int = 4000) -> list:
        """Разбивает длинное сообщение на части"""
        parts = []
        current_part = ""
        
        for line in text.split('\n'):
            if len(current_part) + len(line) + 1 > max_length:
                if current_part:
                    parts.append(current_part)
                    current_part = line + '\n'
                else:
                    # Строка слишком длинная, разбиваем по словам
                    words = line.split()
                    for word in words:
                        if len(current_part) + len(word) + 1 > max_length:
                            if current_part:
                                parts.append(current_part)
                            current_part = word + ' '
                        else:
                            current_part += word + ' '
            else:
                current_part += line + '\n'
        
        if current_part:
            parts.append(current_part)
        
        return parts
    
    def run_polling(self):
        """Запуск бота в режиме polling (для разработки)"""
        logger.info("🚀 Запуск Telegram бота в режиме polling...")
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)
    
    def get_webhook_handler(self):
        """Возвращает функцию-обработчик для webhook (для использования в FastAPI)"""
        async def webhook_handler(update: Update):
            await self.application.process_update(update)
        
        return webhook_handler


if __name__ == "__main__":
    """Единая точка запуска: scripts/run_telegram_bot.py (без дублирования логики)."""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    script = root / "scripts" / "run_telegram_bot.py"
    raise SystemExit(subprocess.run([sys.executable, str(script)], cwd=str(root)).returncode)
