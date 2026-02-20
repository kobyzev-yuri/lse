"""
Telegram Bot для LSE Trading System
Основной класс бота для работы с независимыми инструментами (золото, валютные пары)
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import re
from typing import Optional, Dict, Any
from datetime import datetime

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
        
        # Создаем приложение
        self.application = Application.builder().token(token).build()
        
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
        self.application.add_handler(CommandHandler("tickers", self._handle_tickers))
        self.application.add_handler(CommandHandler("ask", self._handle_ask))
        self.application.add_handler(CommandHandler("portfolio", self._handle_portfolio))
        self.application.add_handler(CommandHandler("buy", self._handle_buy))
        self.application.add_handler(CommandHandler("sell", self._handle_sell))
        self.application.add_handler(CommandHandler("history", self._handle_history))
        self.application.add_handler(CommandHandler("recommend", self._handle_recommend))
        
        # Обработка текстовых сообщений (для произвольных запросов)
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        
        # Обработка callback queries (для inline кнопок)
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
    
    def _check_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя"""
        if self.allowed_users is None:
            return True
        return user_id in self.allowed_users
    
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
/chart <ticker> [days] — график
/ask <вопрос> — вопрос (работает в группах!)
/tickers — список инструментов

**Песочница (вход/выход, P&L):**
/portfolio — портфель и P&L
/buy <ticker> <кол-во> — купить
/sell <ticker> [кол-во] — продать (без кол-ва — вся позиция)
/history [N] — последние сделки
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
`/history [N]` — последние N сделок (по умолч. 15)
`/recommend <ticker>` — рекомендация: когда открыть позицию, стоп-лосс, размер позиции
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
            
            # Получаем новости через AnalystAgent
            news_df = self.analyst.get_recent_news(ticker)
            
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
            logger.error(f"Ошибка получения новостей для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(
                f"❌ Ошибка получения новостей для {ticker}: {str(e)}"
            )
    
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
        
        ticker_raw = context.args[0].upper()
        ticker = _normalize_ticker(ticker_raw)
        days = 1  # По умолчанию текущий день
        
        if len(context.args) >= 2:
            try:
                days = int(context.args[1])
                days = max(1, min(30, days))  # Ограничиваем от 1 до 30 дней
            except ValueError:
                pass
        
        try:
            await update.message.reply_text(f"📈 Построение графика для {ticker}...")
            
            # Получаем данные из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            from datetime import datetime, timedelta
            import pandas as pd
            
            engine = create_engine(get_database_url())
            cutoff_date = datetime.now() - timedelta(days=days)
            
            logger.info(f"Запрос данных для {ticker} с {cutoff_date} (последние {days} дней)")
            
            with engine.connect() as conn:
                df = pd.read_sql(
                    text("""
                        SELECT date, close, sma_5, volatility_5, rsi
                        FROM quotes
                        WHERE ticker = :ticker AND date >= :cutoff_date
                        ORDER BY date ASC
                    """),
                    conn,
                    params={"ticker": ticker, "cutoff_date": cutoff_date}
                )
            
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
                matplotlib.use('Agg')  # Используем backend без GUI
                import matplotlib.pyplot as plt
                import matplotlib.dates as mdates
                from io import BytesIO
                
                logger.info("Инициализация matplotlib...")
                
                df['date'] = pd.to_datetime(df['date'])
                
                # Если данных мало (1-2 точки), используем один график
                if len(df) <= 2:
                    fig, ax1 = plt.subplots(1, 1, figsize=(10, 6))
                    ax1.plot(df['date'], df['close'], marker='o', label='Цена закрытия', linewidth=2, color='#2E86AB')
                    ax1.set_ylabel('Цена', fontsize=10)
                    ax1.set_xlabel('Дата', fontsize=10)
                    ax1.legend(loc='best')
                    ax1.grid(True, alpha=0.3)
                    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
                    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')
                    fig.suptitle(f'{ticker} - График цены', fontsize=14, fontweight='bold')
                else:
                    # Два графика: цена и RSI
                    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
                    fig.suptitle(f'{ticker} - График цены', fontsize=14, fontweight='bold')
                    
                    # График цены и SMA
                    ax1.plot(df['date'], df['close'], label='Цена закрытия', linewidth=2, color='#2E86AB')
                    if 'sma_5' in df.columns and df['sma_5'].notna().any():
                        ax1.plot(df['date'], df['sma_5'], label='SMA(5)', linewidth=1.5, color='#A23B72', linestyle='--')
                    ax1.set_ylabel('Цена', fontsize=10)
                    ax1.legend(loc='best')
                    ax1.grid(True, alpha=0.3)
                    
                    # График RSI (если есть)
                    if 'rsi' in df.columns and df['rsi'].notna().any():
                        ax2.plot(df['date'], df['rsi'], label='RSI', linewidth=2, color='#F18F01')
                        ax2.axhline(y=70, color='r', linestyle='--', alpha=0.5, label='Перекупленность')
                        ax2.axhline(y=30, color='g', linestyle='--', alpha=0.5, label='Перепроданность')
                        ax2.set_ylabel('RSI', fontsize=10)
                        ax2.set_ylim(0, 100)
                        ax2.legend(loc='best')
                        ax2.grid(True, alpha=0.3)
                    
                    # Форматируем даты на оси X
                    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
                    if days > 7:
                        ax2.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 7)))
                    else:
                        ax2.xaxis.set_major_locator(mdates.DayLocator(interval=1))
                    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')
                
                plt.tight_layout()
                
                logger.info("Сохранение графика в буфер...")
                # Сохраняем в BytesIO
                img_buffer = BytesIO()
                plt.savefig(img_buffer, format='png', dpi=100, bbox_inches='tight')
                img_buffer.seek(0)
                plt.close()
                
                logger.info(f"Отправка графика для {ticker} ({len(df)} точек данных)")
                
                # Формируем подпись с объяснением формата данных
                caption = f"📈 {ticker} - {days} дней ({len(df)} точек)"
                if days == 1:
                    caption += "\n\nℹ️ Данные: дневные (цена закрытия за день)"
                elif len(df) < 5:
                    caption += f"\n\nℹ️ Данные: дневные (цена закрытия). Для более детального графика используйте больше дней."
                
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
    
    async def _handle_tickers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик команды /tickers"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        try:
            # Получаем список тикеров из БД
            from sqlalchemy import create_engine, text
            from config_loader import get_database_url
            
            engine = create_engine(get_database_url())
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT DISTINCT ticker FROM quotes ORDER BY ticker")
                )
                tickers = [row[0] for row in result]
            
            if not tickers:
                await update.message.reply_text("ℹ️ Нет отслеживаемых инструментов")
                return
            
            # Группируем по типам
            commodities = [t for t in tickers if '=' in t or t.startswith('GC')]
            currencies = [t for t in tickers if 'USD' in t or 'EUR' in t or 'GBP' in t]
            stocks = [t for t in tickers if t not in commodities and t not in currencies]
            
            response = "📊 **Отслеживаемые инструменты:**\n\n"
            
            if commodities:
                response += "🥇 **Товары:**\n"
                response += "\n".join([f"  • {t}" for t in commodities[:10]])
                response += "\n\n"
            
            if currencies:
                response += "💱 **Валютные пары:**\n"
                response += "\n".join([f"  • {t}" for t in currencies[:10]])
                response += "\n\n"
            
            if stocks:
                response += "📈 **Акции:**\n"
                response += "\n".join([f"  • {t}" for t in stocks[:10]])
            
            if len(tickers) > 30:
                response += f"\n\n... и еще {len(tickers) - 30} инструментов"
            
            await update.message.reply_text(response, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Ошибка получения списка тикеров: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
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
        """Последние сделки: /history [N]."""
        user_id = update.effective_user.id
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        agent = self._get_execution_agent()
        if not agent:
            await update.message.reply_text("❌ Песочница недоступна.")
            return
        limit = 15
        if context.args and len(context.args) >= 1:
            try:
                limit = min(int(context.args[0]), 50)
            except ValueError:
                pass
        try:
            rows = agent.get_trade_history(limit=limit)
            if not rows:
                await update.message.reply_text("История сделок пуста.")
                return
            lines = ["📜 **Последние сделки:**"]
            for r in rows:
                ts = r["ts"].strftime("%Y-%m-%d %H:%M") if hasattr(r["ts"], "strftime") else str(r["ts"])
                side = "🟢" if r["side"] == "BUY" else "🔴"
                lines.append(f"{side} {ts} — {r['side']} {r['ticker']} x{r['quantity']:.0f} @ ${r['price']:.2f} ({r['signal_type']})")
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
                    
                    for ticker in tickers:
                        ticker = _normalize_ticker(ticker)
                        ticker_names.append(ticker)
                        news_df = self.analyst.get_recent_news(ticker)
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
                            type_str = f" [{event_type}]" if event_type else ""
                            response += f"**{ticker}** - {date_str}{sentiment_str}\n🔹 {source}{type_str}\n{preview}\n\n"
                        
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
                        news_df = self.analyst.get_recent_news(ticker)
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
        response = f"📰 **Новости для {_escape_markdown(ticker)}** (последние 7 дней, топ {top_n}):\n\n"

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
            if sentiment is not None:
                if sentiment > 0.6:
                    sentiment_str = " 📈"
                elif sentiment < 0.4:
                    sentiment_str = " 📉"
            date_str = ts.strftime('%Y-%m-%d %H:%M') if hasattr(ts, 'strftime') else str(ts)
            type_str = f" [{event_type}]" if event_type else ""  # event_type уже экранирован
            response += f"📅 {date_str}{sentiment_str}\n🔹 **{source}**{type_str}\n{preview}\n\n"
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
