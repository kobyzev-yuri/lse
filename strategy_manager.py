"""
Интеллектуальный диспетчер стратегий
Выбирает оптимальную стратегию на основе режима рынка
"""

import logging
import math
from typing import Dict, Any, List, Optional
from strategies.momentum_strategy import MomentumStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy
from strategies.volatile_gap_strategy import VolatileGapStrategy
from strategies.geopolitical_bounce_strategy import GeopoliticalBounceStrategy
from strategies.neutral_strategy import NeutralStrategy
from strategies.base_strategy import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyManager:
    """
    Интеллектуальный диспетчер для выбора оптимальной стратегии
    на основе волатильности, sentiment и ценовых гэпов.

    Контекст: портфельный цикл и AnalystAgent работают по **дневным** котировкам
    (последние дни, SMA/вола за 5 и 20 дней) и **KB-новостям** (взвешенный sentiment, insight).
    Поля ``vix_value`` / ``vix_regime`` в ``technical_data`` (из ``AnalystAgent.get_vix_regime``)
    смягчают выбор «панических» веток при низком VIX — в духе количественного risk-on/off.
    """
    
    def __init__(self):
        """Инициализация всех доступных стратегий"""
        self.strategies = [
            GeopoliticalBounceStrategy(),
            MomentumStrategy(),
            MeanReversionStrategy(),
            VolatileGapStrategy(),
        ]
        
        # Пороги для принятия решений
        self.high_volatility_threshold = 1.5  # Коэффициент волатильности
        self.extreme_sentiment_threshold = 0.6  # В центрированной шкале (-1.0 до 1.0)
        self.gap_threshold = 3.0  # Процент гэпа
        
        logger.info(f"✅ StrategyManager инициализирован с {len(self.strategies)} стратегиями")
    
    def select_strategy(
        self,
        ticker: str,
        technical_data: Dict[str, Any],
        news_data: List[Dict[str, Any]],
        sentiment_score: float  # -1.0 до 1.0 (центрированная шкала)
    ) -> Optional[BaseStrategy]:
        """
        Выбирает оптимальную стратегию на основе режима рынка
        
        Args:
            ticker: Тикер инструмента
            technical_data: Технические данные (close, sma_5, volatility_5, avg_volatility_20, open_price)
            news_data: Список новостей
            sentiment_score: Взвешенный sentiment в центрированной шкале (-1.0 до 1.0)
            
        Returns:
            Выбранная стратегия или None если ни одна не подходит
        """
        volatility_5 = technical_data.get('volatility_5')
        avg_volatility_20 = technical_data.get('avg_volatility_20')
        open_price = technical_data.get('open_price')
        close = technical_data.get('close')
        
        # Расчет коэффициента волатильности
        volatility_ratio = 1.0
        if volatility_5 and avg_volatility_20 and avg_volatility_20 > 0:
            volatility_ratio = volatility_5 / avg_volatility_20
        vol_str = f"{volatility_ratio:.2f}x" if math.isfinite(volatility_ratio) else "—"

        # Расчет гэпа (если есть данные об открытии)
        gap_percent = 0.0
        if open_price and close and open_price > 0:
            gap_percent = abs((close - open_price) / open_price) * 100

        vix_val = technical_data.get("vix_value")
        vix_reg = technical_data.get("vix_regime")
        vix_note = ""
        if vix_val is not None:
            vix_note = f", VIX={float(vix_val):.2f} ({vix_reg})"

        logger.info(f"📊 Анализ режима рынка для {ticker}:")
        logger.info(f"   Волатильность: {vol_str} (порог: {self.high_volatility_threshold}){vix_note}")
        logger.info(f"   Sentiment: {sentiment_score:.2f} (порог: ±{self.extreme_sentiment_threshold})")
        logger.info(f"   Гэп: {gap_percent:.2f}% (порог: {self.gap_threshold}%)")
        prev_day_return_pct = technical_data.get("prev_day_return_pct")
        if prev_day_return_pct is not None:
            logger.info(f"   Падение пред. сессии: {prev_day_return_pct:.2f}%")
        
        # Логика выбора стратегии (The Switch)
        
        # 1. GeopoliticalBounceStrategy: вчера падение ≥2% — ловим отскок long
        if prev_day_return_pct is not None and prev_day_return_pct <= -2.0:
            selected = self._get_strategy_by_name("Geopolitical Bounce")
            if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(
                    f"🔄 Резкое падение пред. сессии ({prev_day_return_pct:.2f}%) → GeopoliticalBounceStrategy для {ticker}"
                )
                return selected
        
        # 2. VolatileGapStrategy: очень высокая волатильность + гэп или экстремальный sentiment.
        # При низком VIX рынок чаще «переваривает» шум — требуем чуть более жёсткие условия для этой ветки.
        vix_low = vix_val is not None and float(vix_val) < 20.0
        vol_bar_volatile = self.high_volatility_threshold + (0.25 if vix_low else 0.0)
        extreme_bar = self.extreme_sentiment_threshold + (0.08 if vix_low else 0.0)
        if volatility_ratio > vol_bar_volatile:
            if gap_percent > self.gap_threshold or abs(sentiment_score) > extreme_bar:
                selected = self._get_strategy_by_name("Volatile Gap")
                if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                    logger.info(f"🔄 Volatility is high ({volatility_ratio:.2f}x), Sentiment is Extreme ({sentiment_score:.2f}) -> Switching to VolatileGapStrategy for {ticker}")
                    return selected
                else:
                    logger.info(f"   ⚠️ VolatileGap не подходит для {ticker} (is_suitable вернул False)")
        
        # 3. MomentumStrategy: низкая волатильность + положительный sentiment (чуть шире окно при LOW_FEAR / VIX<18)
        momentum_vol_cap = 1.0
        if vix_reg == "LOW_FEAR" or (vix_val is not None and float(vix_val) < 18.0):
            momentum_vol_cap = 1.12
        if volatility_ratio < momentum_vol_cap and sentiment_score > 0.3:
            selected = self._get_strategy_by_name("Momentum")
            if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"🔄 Market is calm (volatility={volatility_ratio:.2f}x), Positive sentiment ({sentiment_score:.2f}) -> Using MomentumStrategy for {ticker}")
                return selected
            else:
                logger.info(f"   ⚠️ Momentum не подходит для {ticker} (is_suitable вернул False)")
        
        # 4. MeanReversionStrategy: высокая волатильность + нейтральный sentiment
        if volatility_ratio > 1.2 and abs(sentiment_score) < 0.4:
            selected = self._get_strategy_by_name("Mean Reversion")
            if selected and selected.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"🔄 Market is volatile ({volatility_ratio:.2f}x), Neutral sentiment ({sentiment_score:.2f}) -> Using MeanReversionStrategy for {ticker}")
                return selected
            else:
                logger.info(f"   ⚠️ MeanReversion не подходит для {ticker} (is_suitable вернул False)")
        
        # 5. Fallback: проверяем все стратегии и выбираем первую подходящую
        for strategy in self.strategies:
            if strategy.is_suitable(technical_data, news_data, sentiment_score):
                logger.info(f"✅ Выбрана стратегия: {strategy.name} (fallback)")
                return strategy
        
        # 6. Нейтральный режим: ни одна стратегия не подошла — консервативный HOLD
        default_strategy = NeutralStrategy()
        logger.info(
            f"📋 Условия не подходят ни под одну стратегию → используется {default_strategy.name} (удержание)"
        )
        return default_strategy
    
    def _get_strategy_by_name(self, name: str) -> Optional[BaseStrategy]:
        """Возвращает стратегию по имени"""
        for strategy in self.strategies:
            if strategy.name == name:
                return strategy
        return None
    
    def get_all_strategies(self) -> List[BaseStrategy]:
        """Возвращает список всех доступных стратегий"""
        return self.strategies


# Глобальный экземпляр менеджера
_strategy_manager: Optional[StrategyManager] = None


def get_strategy_manager() -> StrategyManager:
    """Получить глобальный экземпляр менеджера стратегий"""
    global _strategy_manager
    if _strategy_manager is None:
        _strategy_manager = StrategyManager()
    return _strategy_manager

