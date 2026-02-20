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
        self.analyst = AnalystAgent(use_llm=False, use_strategy_factory=True)
        self.vector_kb = VectorKB()
        
        # Создаем приложение
        self.application = Application.builder().token(token).build()
        
        # Регистрируем handlers
        self._register_handlers()
        
        logger.info("✅ LSE Telegram Bot инициализирован")
    
    def _register_handlers(self):
        """Регистрация обработчиков команд и сообщений"""
        # Команды
        self.application.add_handler(CommandHandler("start", self._handle_start))
        self.application.add_handler(CommandHandler("help", self._handle_help))
        self.application.add_handler(CommandHandler("signal", self._handle_signal))
        self.application.add_handler(CommandHandler("news", self._handle_news))
        self.application.add_handler(CommandHandler("price", self._handle_price))
        self.application.add_handler(CommandHandler("tickers", self._handle_tickers))
        
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

Анализ независимых инструментов:
• Золото (GC=F)
• Валютные пары (GBPUSD=X, EURUSD=X)
• Акции (MSFT, SNDK и т.д.)

**Доступные команды:**
/signal — справка и список тикеров; /signal <ticker> — анализ
/news <ticker> [N] - Новости (N — сколько показать, по умолч. 10)
/price <ticker> - Текущая цена
/tickers - Список отслеживаемых инструментов
/help - Справка

**Примеры:**
/signal GC=F
/news MSFT 15
/price MSFT
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

**Список инструментов:**
`/tickers` - Показать все отслеживаемые инструменты

**Произвольные запросы:**
Можно задавать вопросы текстом, например:
"Какие новости по золоту?"
"Анализ GBPUSD"
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
        
        ticker = context.args[0].upper()
        
        try:
            # Показываем, что анализ начат
            await update.message.reply_text(f"🔍 Анализ {ticker}...")
            
            # Получаем решение от AnalystAgent
            decision_result = self.analyst.get_decision_with_llm(ticker)
            
            # Форматируем ответ
            response = self._format_signal_response(ticker, decision_result)
            
            await update.message.reply_text(response, parse_mode='Markdown')
            
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
        
        ticker = context.args[0].upper()
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
        
        ticker = context.args[0].upper()
        
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
                await update.message.reply_text(f"❌ Нет данных для {ticker}")
                return
            
            date, close, sma_5, vol_5, rsi = row
            
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
            
            response = f"""
💰 **{ticker}**

📅 Дата: {date.strftime('%Y-%m-%d') if date else 'N/A'}
💵 Цена: ${close:.2f}
📈 SMA(5): ${sma_5:.2f if sma_5 else 'N/A'}
📊 Волатильность(5): {vol_5:.2f}% {'' if vol_5 else 'N/A'}{rsi_text}
            """
            
            await update.message.reply_text(response.strip(), parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"Ошибка получения цены для {ticker}: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
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
    
    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик произвольных текстовых сообщений"""
        user_id = update.effective_user.id
        
        if not self._check_access(user_id):
            await update.message.reply_text("❌ Доступ запрещен")
            return
        
        text = update.message.text.strip()
        
        try:
            # Пытаемся извлечь ticker из текста
            ticker = self._extract_ticker_from_text(text)
            
            if ticker:
                # Если найден ticker, показываем анализ
                await update.message.reply_text(f"🔍 Анализ {ticker}...")
                
                decision_result = self.analyst.get_decision_with_llm(ticker)
                response = self._format_signal_response(ticker, decision_result)
                
                await update.message.reply_text(response, parse_mode='Markdown')
            else:
                # Ищем в Vector KB похожие события
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
                    
                    await update.message.reply_text(response, parse_mode='Markdown')
        
        except Exception as e:
            logger.error(f"Ошибка обработки текста: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Ошибка обработки запроса. Попробуйте использовать команды:\n"
                "/signal <ticker>\n"
                "/news <ticker>"
            )
    
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
        
        # Получаем текущую цену и RSI
        from sqlalchemy import create_engine, text
        from config_loader import get_database_url
        
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT close, rsi FROM quotes WHERE ticker = :ticker ORDER BY date DESC LIMIT 1"),
                {"ticker": ticker}
            )
            row = result.fetchone()
            price = f"${row[0]:.2f}" if row and row[0] else "N/A"
            rsi = row[1] if row and row[1] is not None else None
        
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
            # Для валют/товаров (=X, =F) RSI только через Alpha Vantage; для акций — Finviz или Alpha Vantage
            if "=X" in ticker or "=F" in ticker:
                rsi_hint = "загрузите индикаторы Alpha Vantage (валюты/товары)"
            else:
                rsi_hint = "update_finviz_data.py или Alpha Vantage"
            rsi_text = f"\n⚪ **RSI:** нет данных ({rsi_hint})"
        
        response = f"""
{decision_emoji} **{ticker}** - {decision}

💰 **Цена:** {price}{rsi_text}
📊 **Технический сигнал:** {technical_signal}
{sentiment_emoji} **Sentiment:** {sentiment:.2f} ({sentiment_label})
📋 **Стратегия:** {strategy}
📰 **Новостей:** {news_count}
        """
        
        # Добавляем reasoning если есть
        if decision_result.get('reasoning'):
            response += f"\n💭 **Обоснование:**\n{decision_result.get('reasoning')[:200]}..."
        
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
        """Пытается извлечь ticker из текста"""
        text_upper = text.upper()
        
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
