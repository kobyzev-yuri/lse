import pandas as pd
from sqlalchemy import create_engine, text
import numpy as np
import re
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import logging

# Импорт LLM сервиса (опционально)
try:
    from services.llm_service import get_llm_service
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("⚠️ LLM сервис недоступен, будет использоваться базовый анализ")

# Импорт менеджера стратегий
try:
    from strategy_manager import get_strategy_manager
    STRATEGY_MANAGER_AVAILABLE = True
except ImportError:
    STRATEGY_MANAGER_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("⚠️ Менеджер стратегий недоступен")

# Импорт утилит для sentiment
try:
    from utils.sentiment_utils import normalize_sentiment, denormalize_sentiment
    SENTIMENT_UTILS_AVAILABLE = True
except ImportError:
    SENTIMENT_UTILS_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("⚠️ Утилиты sentiment недоступны")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def load_config():
    """Загружает конфигурацию из локального config.env или ../brats/config.env"""
    from config_loader import get_database_url
    return get_database_url()


class AnalystAgent:
    """Агент для анализа торговых сигналов на основе технических индикаторов и базы знаний"""
    
    def __init__(self, use_llm: bool = True, use_strategy_factory: bool = True):
        """
        Инициализация подключения к базе данных
        
        Args:
            use_llm: Использовать LLM для улучшения анализа (по умолчанию True)
            use_strategy_factory: Использовать фабрику стратегий (по умолчанию True)
        """
        self.db_url = load_config()
        self.engine = create_engine(self.db_url)
        self.use_llm = use_llm and LLM_AVAILABLE
        self.use_strategy_manager = use_strategy_factory and STRATEGY_MANAGER_AVAILABLE
        
        if self.use_llm:
            try:
                self.llm_service = get_llm_service()
                logger.info("✅ AnalystAgent инициализирован с LLM поддержкой")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось инициализировать LLM: {e}, используется базовый анализ")
                self.use_llm = False
                self.llm_service = None
        else:
            self.llm_service = None
        
        if self.use_strategy_manager:
            try:
                self.strategy_manager = get_strategy_manager()
                logger.info("✅ AnalystAgent инициализирован с менеджером стратегий")
            except Exception as e:
                logger.warning(f"⚠️ Не удалось инициализировать менеджер стратегий: {e}")
                self.use_strategy_manager = False
                self.strategy_manager = None
        else:
            self.strategy_manager = None
        
        if not self.use_llm and not self.use_strategy_manager:
            logger.info("✅ AnalystAgent инициализирован (базовый анализ)")

    def get_vix_regime(self, as_of: datetime | None = None) -> dict:
        """
        Определяет режим волатильности рынка по индексу VIX.
        
        Режимы:
        - HIGH_PANIC: высокий страх / паника на рынке
        - LOW_FEAR: низкий уровень страха, спокойный рынок
        - NEUTRAL: промежуточное состояние
        
        Args:
            as_of: Дата, на которую нужно определить режим (если None — используется последняя доступная точка)
        
        Returns:
            dict с ключами:
            - regime: строка режима ('HIGH_PANIC' | 'LOW_FEAR' | 'NEUTRAL' | 'NO_DATA')
            - vix_value: последнее значение VIX (float | None)
            - ts: метка времени этой точки (datetime | None)
        """
        logger.info("🌡  Определение режима VIX")

        query = """
            SELECT date, close
            FROM quotes
            WHERE ticker = :ticker
        """
        params: Dict[str, Any] = {"ticker": "^VIX"}

        if as_of is not None:
            query += " AND date <= :as_of"
            params["as_of"] = as_of

        query += " ORDER BY date DESC LIMIT 1"

        with self.engine.connect() as conn:
            result = conn.execute(text(query), params).fetchone()

        if not result:
            logger.warning("⚠️  Нет данных VIX (^VIX) в таблице quotes")
            return {"regime": "NO_DATA", "vix_value": None, "ts": None}

        ts, vix_value = result[0], float(result[1])

        # Простые пороговые значения для классификации
        if vix_value >= 25:
            regime = "HIGH_PANIC"
        elif vix_value <= 15:
            regime = "LOW_FEAR"
        else:
            regime = "NEUTRAL"

        logger.info(f"🌡  VIX={vix_value:.2f} на {ts} → режим: {regime}")
        return {"regime": regime, "vix_value": vix_value, "ts": ts}
    
    def get_last_5_days_quotes(self, ticker: str, include_today: bool = True) -> pd.DataFrame:
        """Выгружает последние 5 дней котировок для указанного тикера.
        Если include_today и в quotes ещё нет текущего дня — подтягивает последний close из yfinance (для игр в долгую)."""
        logger.info(f"📊 Загрузка последних 5 дней котировок для {ticker}")
        
        with self.engine.connect() as conn:
            query = text("""
                SELECT date, ticker, close, volume, sma_5, volatility_5, rsi
                FROM quotes
                WHERE ticker = :ticker
                ORDER BY date DESC
                LIMIT 5
            """)
            df = pd.read_sql(query, conn, params={"ticker": ticker})
        
        if df.empty:
            logger.warning(f"⚠️  Нет данных для тикера {ticker}")
            return df

        # Подтянуть текущий день из yfinance, если в quotes только предыдущие закрытия
        if include_today and not df.empty:
            last_date = pd.Timestamp(df["date"].iloc[0]).date()
            today = datetime.now().date()
            if last_date < today:
                try:
                    import yfinance as yf
                    t = yf.Ticker(ticker)
                    hist = t.history(period="2d", interval="1d", auto_adjust=False)
                    if hist is not None and not hist.empty:
                        hist = hist.rename_axis("Date").reset_index()
                        row = hist.iloc[-1]
                        row_date = pd.Timestamp(row["Date"]).date()
                        if row_date >= today and pd.notna(row.get("Close")):
                            today_df = pd.DataFrame([{
                                "date": row_date,
                                "ticker": ticker,
                                "close": float(row["Close"]),
                                "volume": float(row["Volume"]) if pd.notna(row.get("Volume")) else None,
                                "sma_5": None,
                                "volatility_5": None,
                                "rsi": None,
                            }])
                            if not today_df.empty and not df.empty:
                                # Убираем колонки полностью NA, чтобы избежать FutureWarning при concat
                                today_df = today_df.dropna(axis=1, how="all")
                                df = df.dropna(axis=1, how="all")
                                df = pd.concat([today_df, df], ignore_index=True)
                            elif not today_df.empty:
                                df = today_df
                            df = df.drop_duplicates(subset=["date"], keep="first").sort_values("date", ascending=False).head(5)
                            logger.info(f"   Добавлен текущий день {row_date} (close={row['Close']:.2f}) из yfinance")
                except Exception as e:
                    logger.debug("Не удалось подтянуть текущий день из yfinance: %s", e)
        
        logger.info(f"✅ Загружено {len(df)} записей для {ticker}")
        return df
    
    def get_average_volatility_20_days(self, ticker: str) -> float:
        """Вычисляет среднюю волатильность за последние 20 дней"""
        logger.info(f"📈 Расчет средней волатильности за 20 дней для {ticker}")
        
        with self.engine.connect() as conn:
            query = text("""
                SELECT AVG(volatility_5) as avg_volatility
                FROM (
                    SELECT volatility_5
                    FROM quotes
                    WHERE ticker = :ticker
                    ORDER BY date DESC
                    LIMIT 20
                ) as last_20
            """)
            result = conn.execute(query, {"ticker": ticker})
            row = result.fetchone()
        
        if row and row[0] is not None:
            avg_vol = float(row[0])
            logger.info(f"✅ Средняя волатильность за 20 дней: {avg_vol:.4f}")
            return avg_vol
        else:
            logger.warning(f"⚠️  Не удалось вычислить среднюю волатильность для {ticker}")
            return 0.0
    
    def check_technical_signal(self, ticker: str) -> str:
        """Проверяет технический сигнал: close > sma_5 и volatility_5 < средняя волатильность за 20 дней"""
        logger.info(f"🔍 Проверка технического сигнала для {ticker}")
        
        df = self.get_last_5_days_quotes(ticker)
        if df.empty:
            logger.warning(f"⚠️  Нет данных для анализа технического сигнала")
            return "NO_DATA"
        
        # Берем последнюю запись
        latest = df.iloc[0]
        close = float(latest['close'])
        sma_5 = float(latest['sma_5'])
        volatility_5 = float(latest['volatility_5'])
        rsi = float(latest['rsi']) if pd.notna(latest.get('rsi')) else None
        
        avg_volatility_20 = self.get_average_volatility_20_days(ticker)
        
        logger.info(f"📊 Параметры последней котировки:")
        logger.info(f"   Close: {close:.2f}")
        logger.info(f"   SMA_5: {sma_5:.2f}")
        logger.info(f"   Volatility_5: {volatility_5:.4f}")
        logger.info(f"   Avg Volatility 20: {avg_volatility_20:.4f}")
        if rsi is not None:
            rsi_status = "перекупленность" if rsi >= 70 else ("перепроданность" if rsi <= 30 else "нейтральная зона")
            logger.info(f"   RSI: {rsi:.1f} ({rsi_status})")
        else:
            logger.info(f"   RSI: N/A")
        
        # Проверка условий
        condition1 = close > sma_5
        condition2 = volatility_5 < avg_volatility_20 if avg_volatility_20 > 0 else False
        
        # Учитываем RSI: перепроданность (RSI < 30) усиливает BUY, перекупленность (RSI > 70) ослабляет
        rsi_factor = 1.0
        if rsi is not None:
            if rsi <= 30:
                rsi_factor = 1.2  # Усиливаем сигнал при перепроданности
                logger.info(f"   RSI указывает на перепроданность - усиление BUY сигнала")
            elif rsi >= 70:
                rsi_factor = 0.5  # Ослабляем сигнал при перекупленности
                logger.info(f"   RSI указывает на перекупленность - ослабление BUY сигнала")
        
        logger.info(f"🔍 Условия технического сигнала:")
        logger.info(f"   Close > SMA_5: {condition1} ({close:.2f} > {sma_5:.2f})")
        logger.info(f"   Volatility_5 < Avg_Vol_20: {condition2} ({volatility_5:.4f} < {avg_volatility_20:.4f})")
        logger.info(f"   RSI фактор: {rsi_factor:.2f}")
        
        if condition1 and condition2:
            signal = "BUY"
            logger.info(f"✅ Технический сигнал: {signal} (RSI фактор: {rsi_factor:.2f})")
        else:
            signal = "HOLD"
            logger.info(f"⚠️  Технический сигнал: {signal}")
        
        return signal
    
    def get_recent_news(self, ticker: str, hours: int = None) -> pd.DataFrame:
        """
        Получает новости для тикера за последние N часов.
        В выборку входят записи по самому тикеру и MACRO/US_MACRO. Окно по умолчанию — 7 дней (168 ч),
        чтобы в боте /news соответствовало надписи «последние 7 дней».
        """
        if hours is None:
            hours = 168  # 7 дней
        cutoff_time = datetime.now() - timedelta(hours=hours)

        logger.info(f"📰 Поиск новостей за последние {hours} ч для {ticker} или MACRO/US_MACRO")

        with self.engine.connect() as conn:
            query = text("""
                SELECT id, ts, ticker, source, content, sentiment_score, event_type, insight, link
                FROM knowledge_base
                WHERE (ticker = :ticker OR ticker = 'MACRO' OR ticker = 'US_MACRO')
                  AND ts >= :cutoff_time
                ORDER BY ts DESC
            """)
            df = pd.read_sql(query, conn, params={
                "ticker": ticker,
                "cutoff_time": cutoff_time
            })

        if df.empty:
            logger.info(f"ℹ️  Новостей за последние {hours} ч не найдено")
        else:
            logger.info(f"✅ Найдено {len(df)} новостей")
            # Сортируем: сначала NEWS и EARNINGS, потом остальное (ECONOMIC_INDICATOR в конец)
            order_map = {'NEWS': 0, 'EARNINGS': 1}
            df['_sort_order'] = df['event_type'].map(order_map).fillna(2).astype(int)
            df = df.sort_values(by=['_sort_order', 'ts'], ascending=[True, False]).drop(columns=['_sort_order'], errors='ignore')
            
            for idx, row in df.iterrows():
                event_type = "MACRO" if row['ticker'] in ['MACRO', 'US_MACRO'] else "TICKER"
                logger.info(f"   [{row['ts']}] {event_type} ({row['ticker']}): {row['content'][:50]}... (sentiment: {row['sentiment_score']})")
        
        return df
    
    def calculate_weighted_sentiment(self, news_df: pd.DataFrame, ticker: str) -> float:
        """
        Вычисляет взвешенный sentiment score.
        Новости с упоминанием конкретного тикера получают больший вес (weight=2.0),
        макро-новости получают стандартный вес (weight=1.0).
        """
        if news_df.empty:
            return 0.0
        
        # Проверяем, упоминается ли тикер в контенте
        def calculate_weight(row):
            ticker_in_content = ticker.upper() in str(row['content']).upper()
            is_ticker_news = row['ticker'] == ticker
            
            if is_ticker_news or ticker_in_content:
                # Новости с упоминанием тикера получают больший вес
                return 2.0
            else:
                # Макро-новости получают стандартный вес
                return 1.0
        
        # Добавляем веса к новостям
        news_df = news_df.copy()
        news_df['weight'] = news_df.apply(calculate_weight, axis=1)
        
        # Новости без sentiment (RSS, NewsAPI) считаем нейтральными (0.5), чтобы не ломать расчёт
        sentiment_series = news_df['sentiment_score'].fillna(0.5).astype(float)
        
        # Вычисляем взвешенный средний sentiment
        weighted_sum = (sentiment_series * news_df['weight']).sum()
        total_weight = news_df['weight'].sum()
        weighted_sentiment = weighted_sum / total_weight if total_weight > 0 else 0.0
        
        logger.info(f"📊 Взвешенный sentiment анализ:")
        logger.info(f"   Всего новостей: {len(news_df)}")
        logger.info(f"   Новостей с упоминанием тикера (weight=2.0): {len(news_df[news_df['weight'] == 2.0])}")
        logger.info(f"   Макро-новостей (weight=1.0): {len(news_df[news_df['weight'] == 1.0])}")
        
        # Логируем только первые 10 новостей для краткости (или все, если их меньше 10)
        max_log_news = min(10, len(news_df))
        for idx, row in news_df.head(max_log_news).iterrows():
            ticker_mentioned = ticker.upper() in str(row['content']).upper() or row['ticker'] == ticker
            # Безопасная обработка sentiment_score (может быть None, NaN или числом)
            sentiment_val = row['sentiment_score']
            if pd.isna(sentiment_val) or sentiment_val is None:
                sentiment_str = "None"
            else:
                try:
                    sentiment_str = f"{float(sentiment_val):.2f}"
                except (ValueError, TypeError):
                    sentiment_str = "None"
            content_preview = str(row['content'])[:50] + "..." if len(str(row['content'])) > 50 else str(row['content'])
            logger.info(f"   [{row['ts']}] Weight={row['weight']:.1f}, Sentiment={sentiment_str}, "
                       f"Ticker mentioned: {ticker_mentioned}, Content: {content_preview}")
        
        if len(news_df) > max_log_news:
            logger.info(f"   ... и еще {len(news_df) - max_log_news} новостей (показаны только первые {max_log_news})")
        
        logger.info(f"   Взвешенный средний sentiment (0.0-1.0): {weighted_sentiment:.3f}")
        
        # Конвертируем в центрированную шкалу (-1.0 до 1.0) для использования в стратегиях
        if SENTIMENT_UTILS_AVAILABLE:
            normalized_sentiment = normalize_sentiment(weighted_sentiment)
            logger.info(f"   Нормализованный sentiment (-1.0 до 1.0): {normalized_sentiment:.3f}")
            return normalized_sentiment
        else:
            return weighted_sentiment
    
    def get_decision(self, ticker: str) -> str:
        """Основной метод принятия решения на основе технического анализа и базы знаний"""
        logger.info(f"=" * 60)
        logger.info(f"🎯 Анализ для тикера: {ticker}")
        logger.info(f"=" * 60)

        # Режим рынка по VIX
        vix_info = self.get_vix_regime()
        vix_regime = vix_info.get("regime")
        logger.info(f"🌡  Режим VIX для анализа {ticker}: {vix_regime}")
        
        # Шаг 1: Проверка технического сигнала
        logger.info("\n📊 ШАГ 1: Анализ технических индикаторов")
        technical_signal = self.check_technical_signal(ticker)
        
        if technical_signal == "NO_DATA":
            logger.warning("⚠️  Недостаточно данных для принятия решения")
            return "NO_DATA"
        
        # Шаг 2: Проверка новостей и sentiment с учетом временного лага и весов
        logger.info("\n📰 ШАГ 2: Анализ новостей и sentiment (с учетом временного лага и весов)")
        news_df = self.get_recent_news(ticker)  # Использует автоматический выбор времени
        
        # Вычисляем взвешенный sentiment
        sentiment_positive = False
        weighted_sentiment = 0.0
        
        if not news_df.empty:
            # Используем взвешенный sentiment (новости с упоминанием тикера имеют больший вес)
            weighted_sentiment = self.calculate_weighted_sentiment(news_df, ticker)
            
            # Также показываем простые метрики для сравнения
            avg_sentiment = news_df['sentiment_score'].mean()
            max_sentiment = news_df['sentiment_score'].max()
            
            logger.info(f"📊 Сравнение метрик sentiment:")
            logger.info(f"   Простой средний sentiment: {avg_sentiment:.3f}")
            logger.info(f"   Максимальный sentiment: {max_sentiment:.3f}")
            logger.info(f"   Взвешенный sentiment: {weighted_sentiment:.3f}")
            
            # Используем взвешенный sentiment для принятия решения
            # weighted_sentiment теперь в центрированной шкале (-1.0 до 1.0)
            sentiment_positive = weighted_sentiment > 0.0  # В центрированной шкале 0.0 = нейтральный
            logger.info(f"   Взвешенный sentiment > 0.0 (положительный): {sentiment_positive}")
        else:
            logger.info("ℹ️  Новостей не найдено, sentiment анализ пропущен")
        
        # Шаг 3: Выбор стратегии и принятие решения
        logger.info("\n🎯 ШАГ 3: Выбор стратегии и принятие решения")
        
        # Используем менеджер стратегий, если доступен
        if self.use_strategy_manager and self.strategy_manager:
            try:
                # Подготавливаем данные для выбора стратегии
                df = self.get_last_5_days_quotes(ticker)
                latest = df.iloc[0] if not df.empty else None
                avg_volatility_20 = self.get_average_volatility_20_days(ticker)
                
                # Получаем цену открытия для расчета гэпа
                open_price = None
                if latest is not None and 'open' in latest:
                    open_price = float(latest['open'])
                elif not df.empty and len(df) > 1:
                    # Берем цену закрытия предыдущего дня как приближение открытия
                    prev_close = float(df.iloc[1]['close'])
                    open_price = prev_close
                
                # Доходность предыдущей сессии (для геополитического отскока: падение ≥2%)
                prev_day_return_pct = None
                if len(df) >= 3:
                    prev_close = float(df.iloc[1]['close'])
                    prev_prev_close = float(df.iloc[2]['close'])
                    if prev_prev_close and prev_prev_close > 0:
                        prev_day_return_pct = (prev_close - prev_prev_close) / prev_prev_close * 100
                # Текущая сессия: доходность от открытия до текущего close (если есть open_price)
                current_day_return_pct = None
                if open_price and open_price > 0 and latest is not None:
                    current_day_return_pct = (float(latest['close']) - open_price) / open_price * 100

                technical_data_for_strategy = {
                    "close": float(latest['close']) if latest is not None else None,
                    "open_price": open_price,
                    "sma_5": float(latest['sma_5']) if latest is not None else None,
                    "volatility_5": float(latest['volatility_5']) if latest is not None else None,
                    "avg_volatility_20": avg_volatility_20,
                    "technical_signal": technical_signal,
                    "prev_day_return_pct": prev_day_return_pct,
                    "current_day_return_pct": current_day_return_pct,
                }
                
                news_list = news_df.to_dict('records') if not news_df.empty else []
                
                # Конвертируем sentiment в центрированную шкалу, если нужно
                sentiment_for_strategy = weighted_sentiment
                if not SENTIMENT_UTILS_AVAILABLE or weighted_sentiment > 1.0 or weighted_sentiment < -1.0:
                    # Если sentiment еще не нормализован (0.0-1.0), нормализуем
                    if 0.0 <= weighted_sentiment <= 1.0:
                        sentiment_for_strategy = normalize_sentiment(weighted_sentiment)
                
                # Выбираем стратегию через менеджер
                selected_strategy = self.strategy_manager.select_strategy(
                    ticker=ticker,
                    technical_data=technical_data_for_strategy,
                    news_data=news_list,
                    sentiment_score=sentiment_for_strategy
                )
                
                if selected_strategy:
                    logger.info(f"📋 Используется стратегия: {selected_strategy.name}")
                    # Вычисляем сигнал через стратегию
                    strategy_result = selected_strategy.calculate_signal(
                        ticker=ticker,
                        technical_data=technical_data_for_strategy,
                        news_data=news_list,
                        sentiment_score=sentiment_for_strategy
                    )
                    decision = strategy_result.get('signal', 'HOLD')
                    logger.info(f"✅ РЕШЕНИЕ (через {selected_strategy.name}): {decision}")
                    logger.info(f"   Уверенность: {strategy_result.get('confidence', 0):.2f}")
                    logger.info(f"   Обоснование: {strategy_result.get('reasoning', 'N/A')}")
                    if strategy_result.get('insight'):
                        logger.info(f"   Insight: {strategy_result.get('insight')}")
                    logger.info(f"=" * 60)
                    return decision
                else:
                    logger.info("⚠️ Менеджер стратегий не выбрал стратегию, используем базовую логику")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при использовании менеджера стратегий: {e}, используем базовую логику")
                import traceback
                logger.error(traceback.format_exc())
        
        # Базовая логика (fallback)
        if technical_signal == "BUY" and sentiment_positive:
            decision = "STRONG_BUY"
            logger.info(f"✅ РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал BUY + Положительный sentiment новостей")
        elif technical_signal == "BUY":
            decision = "BUY"
            logger.info(f"✅ РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал BUY, но sentiment нейтральный или отсутствует")
        else:
            decision = "HOLD"
            logger.info(f"⚠️  РЕШЕНИЕ: {decision}")
            logger.info(f"   Причина: Технический сигнал не BUY")

        # Уточнение стиля входа в сделку в зависимости от режима VIX
        if vix_regime == "HIGH_PANIC":
            logger.info("⚠️  Режим HIGH_PANIC: рекомендуется ИСПОЛЬЗОВАТЬ ТОЛЬКО ЛИМИТНЫЕ ОРДЕРА, "
                        "избегать маркет-входов из-за высокой волатильности.")
        elif vix_regime == "LOW_FEAR" and decision in ("BUY", "STRONG_BUY"):
            logger.info("✅ Режим LOW_FEAR: разрешена агрессивная покупка на пробое максимумов (Breakout).")

        logger.info(f"=" * 60)
        return decision

    def _get_benchmark_signal(self, benchmark: str = "MU") -> Optional[str]:
        """Базовый сигнал по бенчмарку (без LLM) — для контекста в промпте при анализе другого тикера."""
        try:
            sig = self.check_technical_signal(benchmark)
            if sig == "NO_DATA":
                return None
            df = self.get_last_5_days_quotes(benchmark)
            if df.empty:
                return None
            latest = df.iloc[0]
            news_df = self.get_recent_news(benchmark)
            sentiment = 0.0
            news_list = []
            if not news_df.empty:
                sentiment = self.calculate_weighted_sentiment(news_df, benchmark)
                news_list = news_df.to_dict("records")
            if SENTIMENT_UTILS_AVAILABLE and (sentiment > 1.0 or sentiment < -1.0) and 0.0 <= sentiment <= 1.0:
                sentiment = normalize_sentiment(sentiment)
            tech = {
                "close": float(latest.get("close")),
                "sma_5": float(latest["sma_5"]) if latest.get("sma_5") is not None else None,
                "volatility_5": float(latest["volatility_5"]) if latest.get("volatility_5") is not None else None,
                "open_price": float(df.iloc[1]["close"]) if len(df) > 1 else None,
            }
            if self.use_strategy_manager and self.strategy_manager:
                try:
                    sel = self.strategy_manager.select_strategy(
                        ticker=benchmark, technical_data=tech, news_data=news_list, sentiment_score=sentiment
                    )
                    if sel:
                        res = sel.calculate_signal(
                            ticker=benchmark, technical_data=tech, news_data=news_list, sentiment_score=sentiment
                        )
                        return res.get("signal", "HOLD")
                except Exception:
                    pass
            if sig == "BUY" and sentiment > 0.0:
                return "STRONG_BUY"
            if sig == "BUY":
                return "BUY"
            return "HOLD"
        except Exception as e:
            logger.debug("Сигнал по бенчмарку %s: %s", benchmark, e)
            return None

    def get_decision_with_llm(self, ticker: str) -> dict:
        """
        Принятие решения с использованием LLM для улучшения анализа
        
        Returns:
            dict с полным анализом, включая LLM рекомендации
        """
        logger.info(f"=" * 60)
        logger.info(f"🎯 Расширенный анализ для тикера: {ticker} (с LLM)")
        logger.info(f"=" * 60)
        
        # При отсутствии RSI в БД — считаем локально по close (валюты/товары, без Finviz/Alpha Vantage)
        try:
            from services.rsi_calculator import get_or_compute_rsi
            get_or_compute_rsi(self.engine, ticker)
        except Exception as e:
            logger.debug(f"Локальный RSI для {ticker}: {e}")
        
        # Базовый анализ
        technical_signal = self.check_technical_signal(ticker)
        if technical_signal == "NO_DATA":
            return {
                "decision": "NO_DATA",
                "technical_signal": "NO_DATA",
                "sentiment": 0.0,
                "llm_analysis": None,
                "reasoning": "Недостаточно данных"
            }
        
        # Получаем данные для LLM
        df = self.get_last_5_days_quotes(ticker)
        latest = df.iloc[0] if not df.empty else None
        avg_volatility_20 = self.get_average_volatility_20_days(ticker)
        rsi_value = float(latest['rsi']) if latest is not None and pd.notna(latest.get('rsi')) else None
        
        technical_data = {
            "close": float(latest['close']) if latest is not None else None,
            "sma_5": float(latest['sma_5']) if latest is not None else None,
            "volatility_5": float(latest['volatility_5']) if latest is not None else None,
            "avg_volatility_20": avg_volatility_20,
            "rsi": rsi_value,
            "technical_signal": technical_signal
        }
        # Корреляция с бенчмарком (MU) за 14 дн. и текущий сигнал по MU — для контекста LLM (совместный контекст по паре).
        # Для MU не добавляем — смысл тривиален (1.0 с собой).
        if ticker and ticker.upper() != "MU":
            try:
                from report_generator import get_rolling_corr_with_benchmark
                corr_val, corr_label = get_rolling_corr_with_benchmark(self.engine, ticker, benchmark="MU", window_days=14)
                technical_data["corr_with_benchmark"] = corr_val
                technical_data["corr_label"] = corr_label
                mu_signal = self._get_benchmark_signal("MU")
                if mu_signal:
                    technical_data["benchmark_signal"] = mu_signal
            except Exception as e:
                logger.debug("Корреляция/сигнал бенчмарка для LLM недоступны: %s", e)
        
        news_df = self.get_recent_news(ticker)
        weighted_sentiment = 0.0
        news_list = []
        
        if not news_df.empty:
            weighted_sentiment = self.calculate_weighted_sentiment(news_df, ticker)
            news_list = news_df.to_dict('records')
        
        # Базовое решение (через менеджер стратегий или базовую логику)
        base_decision = "HOLD"
        strategy_result = None
        selected_strategy = None
        
        # Конвертируем sentiment в центрированную шкалу, если нужно
        sentiment_for_strategy = weighted_sentiment
        if not SENTIMENT_UTILS_AVAILABLE or weighted_sentiment > 1.0 or weighted_sentiment < -1.0:
            # Если sentiment еще не нормализован (0.0-1.0), нормализуем
            if 0.0 <= weighted_sentiment <= 1.0:
                sentiment_for_strategy = normalize_sentiment(weighted_sentiment)
        
        if self.use_strategy_manager and self.strategy_manager:
            try:
                # Добавляем open_price для расчета гэпа
                open_price = technical_data.get('open_price')
                if not open_price:
                    # Пытаемся получить из последних котировок
                    df = self.get_last_5_days_quotes(ticker)
                    if not df.empty and len(df) > 1:
                        open_price = float(df.iloc[1]['close'])  # Приближение
                
                technical_data_with_open = technical_data.copy()
                technical_data_with_open['open_price'] = open_price
                
                selected_strategy = self.strategy_manager.select_strategy(
                    ticker=ticker,
                    technical_data=technical_data_with_open,
                    news_data=news_list,
                    sentiment_score=sentiment_for_strategy
                )
                
                if selected_strategy:
                    strategy_result = selected_strategy.calculate_signal(
                        ticker=ticker,
                        technical_data=technical_data_with_open,
                        news_data=news_list,
                        sentiment_score=sentiment_for_strategy
                    )
                    base_decision = strategy_result.get('signal', 'HOLD')
                    logger.info(f"📋 Стратегия {selected_strategy.name}: {base_decision}")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при использовании менеджера стратегий: {e}")
        
        # Fallback к базовой логике
        if not strategy_result:
            # В центрированной шкале: > 0 = положительный
            sentiment_positive = sentiment_for_strategy > 0.0
            if technical_signal == "BUY" and sentiment_positive:
                base_decision = "STRONG_BUY"
            elif technical_signal == "BUY":
                base_decision = "BUY"
            else:
                base_decision = "HOLD"
        
        # Контекст премаркета для LLM (этап 3.1): при PRE_MARKET добавляем цену премаркета, гэп, минуты до открытия
        try:
            from services.market_session import get_market_session_context
            from services.premarket import get_premarket_context
            session_ctx = get_market_session_context()
            if (session_ctx.get("session_phase") or "").strip() == "PRE_MARKET":
                pm = get_premarket_context(ticker)
                if not pm.get("error"):
                    mins = pm.get("minutes_until_open") or session_ctx.get("minutes_until_open")
                    parts = [
                        "Сейчас премаркет NYSE.",
                        f"До открытия US: {mins} мин." if mins is not None else "",
                        f"Цена премаркета: {pm.get('premarket_last')}" if pm.get("premarket_last") is not None else "",
                        f"Гэп к вчерашнему закрытию: {pm.get('premarket_gap_pct'):+.2f}%" if pm.get("premarket_gap_pct") is not None else "",
                        f"Закрытие вчера: {pm.get('prev_close')}" if pm.get("prev_close") is not None else "",
                    ]
                    technical_data["premarket_note"] = " ".join(p for p in parts if p).strip()
        except Exception as e:
            logger.debug("Премаркет-контекст для LLM: %s", e)

        # Sentiment в шкале 0.0-1.0 для промпта и LLM
        sentiment_for_llm = denormalize_sentiment(weighted_sentiment) if SENTIMENT_UTILS_AVAILABLE else weighted_sentiment
        if sentiment_for_llm < 0 or sentiment_for_llm > 1:
            sentiment_for_llm = 0.5  # Fallback
        strategy_outcome_stats = ""
        try:
            from report_generator import get_engine, get_strategy_outcome_stats
            strategy_outcome_stats = get_strategy_outcome_stats(get_engine(), limit_days=180)
        except Exception as e:
            logger.debug("Статистика по стратегиям для LLM недоступна: %s", e)

        # LLM анализ (если доступен)
        llm_result = None
        llm_guidance = None
        
        if self.use_llm and self.llm_service:
            try:
                logger.info("\n🤖 ШАГ 3: LLM анализ торговой ситуации")
                llm_result = self.llm_service.analyze_trading_situation(
                    ticker=ticker,
                    technical_data=technical_data,
                    news_data=news_list,
                    sentiment_score=sentiment_for_llm,
                    strategy_name=selected_strategy.name if selected_strategy else None,
                    strategy_signal=base_decision if base_decision else None,
                    strategy_outcome_stats=strategy_outcome_stats if strategy_outcome_stats else None,
                )
                logger.info(f"✅ LLM анализ завершен: {llm_result.get('llm_analysis', {}).get('decision', 'N/A')}")
                llm_guidance = None
            except Exception as e:
                logger.error(f"❌ Ошибка LLM анализа: {e}")
                llm_result = None
                llm_guidance = None
        
        # Финальное решение (приоритет LLM, если доступен)
        if llm_result and llm_result.get('llm_analysis'):
            llm_decision = llm_result['llm_analysis'].get('decision', base_decision)
            # Маппинг LLM решений к нашим
            if llm_decision in ['BUY', 'STRONG_BUY']:
                final_decision = llm_decision
            else:
                final_decision = base_decision
        else:
            final_decision = base_decision
        
        # Конвертируем sentiment обратно в 0.0-1.0 для совместимости
        sentiment_0_1 = denormalize_sentiment(weighted_sentiment) if SENTIMENT_UTILS_AVAILABLE else weighted_sentiment
        
        result = {
            "decision": final_decision,
            "technical_signal": technical_signal,
            "sentiment": sentiment_0_1,  # Для совместимости возвращаем в шкале 0.0-1.0
            "sentiment_normalized": weighted_sentiment,  # В центрированной шкале -1.0 до 1.0
            "sentiment_positive": weighted_sentiment > 0.0,  # В центрированной шкале
            "technical_data": technical_data,
            "news_count": len(news_list),
            "strategy_result": strategy_result,  # Результат от выбранной стратегии
            "selected_strategy": selected_strategy.name if selected_strategy else None,
            "llm_analysis": llm_result.get('llm_analysis') if llm_result else None,
            "llm_guidance": llm_guidance,  # Добавляем LLM guidance со стратегией
            "llm_usage": llm_result.get('usage', {}) if llm_result else None,
            "base_decision": base_decision
        }
        # Для /prompt_entry с тикером: всегда заполняем промпт (актуальный контекст); ответ LLM — если был вызов
        if llm_result:
            if "prompt_system" in llm_result:
                result["prompt_system"] = llm_result["prompt_system"]
            if "prompt_user" in llm_result:
                result["prompt_user"] = llm_result["prompt_user"]
            if "llm_response_raw" in llm_result:
                result["llm_response_raw"] = llm_result["llm_response_raw"]
        elif self.llm_service:
            # LLM не вызывался или ошибка — всё равно подставляем собранный промпт для экспорта
            built = self.llm_service.build_entry_prompt(
                ticker=ticker,
                technical_data=technical_data,
                news_data=news_list,
                sentiment_score=sentiment_for_llm,
                strategy_name=selected_strategy.name if selected_strategy else None,
                strategy_signal=base_decision if base_decision else None,
                strategy_outcome_stats=strategy_outcome_stats if strategy_outcome_stats else None,
            )
            result["prompt_system"] = built["prompt_system"]
            result["prompt_user"] = built["prompt_user"]
            result["llm_response_raw"] = ""
        
        logger.info(f"\n🎯 ФИНАЛЬНОЕ РЕШЕНИЕ: {final_decision}")
        if llm_result:
            logger.info(f"   LLM рекомендация: {llm_result.get('llm_analysis', {}).get('decision', 'N/A')}")
            logger.info(f"   Уверенность LLM: {llm_result.get('llm_analysis', {}).get('confidence', 0):.2f}")
        # LLM guidance больше не используется, стратегия выбирается через StrategyManager
        if strategy_result and strategy_result.get('insight'):
            logger.info(f"   Insight: {strategy_result.get('insight')}")
        logger.info(f"=" * 60)
        
        return result


if __name__ == "__main__":
    # Пример использования
    agent = AnalystAgent()
    
    # Тестируем на разных тикерах
    test_tickers = ["MSFT", "SNDK"]
    
    for ticker in test_tickers:
        decision = agent.get_decision(ticker)
        print(f"\n🎯 Финальное решение для {ticker}: {decision}\n")

