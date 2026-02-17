"""
Утилиты для работы с sentiment score
Конвертация между шкалами 0.0-1.0 и -1.0 до 1.0
"""


def normalize_sentiment(sentiment_0_1: float) -> float:
    """
    Конвертирует sentiment из шкалы 0.0-1.0 в центрированную шкалу -1.0 до 1.0
    
    Args:
        sentiment_0_1: Sentiment в шкале 0.0 (отрицательный) до 1.0 (положительный)
        
    Returns:
        Sentiment в шкале -1.0 (отрицательный) до 1.0 (положительный)
        
    Формула: (sentiment - 0.5) * 2
    - 0.0 -> -1.0 (очень отрицательный)
    - 0.5 -> 0.0 (нейтральный)
    - 1.0 -> 1.0 (очень положительный)
    """
    if sentiment_0_1 is None:
        return 0.0
    return (sentiment_0_1 - 0.5) * 2.0


def denormalize_sentiment(sentiment_neg1_1: float) -> float:
    """
    Конвертирует sentiment из центрированной шкалы -1.0 до 1.0 в шкалу 0.0-1.0
    
    Args:
        sentiment_neg1_1: Sentiment в шкале -1.0 (отрицательный) до 1.0 (положительный)
        
    Returns:
        Sentiment в шкале 0.0 (отрицательный) до 1.0 (положительный)
        
    Формула: (sentiment / 2.0) + 0.5
    - -1.0 -> 0.0 (очень отрицательный)
    - 0.0 -> 0.5 (нейтральный)
    - 1.0 -> 1.0 (очень положительный)
    """
    if sentiment_neg1_1 is None:
        return 0.5
    return (sentiment_neg1_1 / 2.0) + 0.5


def apply_sentiment_to_signal(base_signal_strength: float, sentiment: float) -> float:
    """
    Применяет sentiment к силе сигнала (умножение)
    
    Args:
        base_signal_strength: Базовая сила сигнала (0.0-1.0)
        sentiment: Sentiment в центрированной шкале (-1.0 до 1.0)
        
    Returns:
        Скорректированная сила сигнала
        
    Примеры:
        - base=0.8, sentiment=1.0 -> 0.8 (положительный sentiment усиливает)
        - base=0.8, sentiment=-1.0 -> -0.8 (отрицательный sentiment инвертирует)
        - base=0.8, sentiment=0.0 -> 0.0 (нейтральный sentiment нейтрализует)
    """
    return base_signal_strength * (1.0 + sentiment)



