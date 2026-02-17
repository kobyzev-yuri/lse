#!/usr/bin/env python3
"""
Пример использования фабрики стратегий
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from analyst_agent import AnalystAgent
from strategies import get_strategy_factory
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def example_1_basic_usage():
    """Пример 1: Базовое использование через AnalystAgent"""
    print("\n" + "="*60)
    print("Пример 1: Базовое использование через AnalystAgent")
    print("="*60 + "\n")
    
    agent = AnalystAgent(use_strategy_factory=True)
    
    tickers = ["MSFT", "SNDK"]
    for ticker in tickers:
        print(f"\n📊 Анализ для {ticker}:")
        decision = agent.get_decision(ticker)
        print(f"   Решение: {decision}")


def example_2_detailed_analysis():
    """Пример 2: Детальный анализ с информацией о стратегии"""
    print("\n" + "="*60)
    print("Пример 2: Детальный анализ с информацией о стратегии")
    print("="*60 + "\n")
    
    agent = AnalystAgent(use_strategy_factory=True, use_llm=False)
    
    ticker = "MSFT"
    result = agent.get_decision_with_llm(ticker)
    
    print(f"\n📊 Результаты анализа для {ticker}:")
    print(f"   Решение: {result['decision']}")
    print(f"   Технический сигнал: {result['technical_signal']}")
    print(f"   Sentiment: {result['sentiment']:.3f}")
    
    if result.get('selected_strategy'):
        print(f"\n📋 Выбранная стратегия: {result['selected_strategy']}")
        
        if result.get('strategy_result'):
            strategy = result['strategy_result']
            print(f"   Сигнал: {strategy.get('signal')}")
            print(f"   Уверенность: {strategy.get('confidence', 0):.2f}")
            print(f"   Обоснование: {strategy.get('reasoning', 'N/A')[:100]}...")
            print(f"   Стоп-лосс: {strategy.get('stop_loss')}%")
            print(f"   Тейк-профит: {strategy.get('take_profit')}%")


def example_3_direct_factory():
    """Пример 3: Прямое использование фабрики стратегий"""
    print("\n" + "="*60)
    print("Пример 3: Прямое использование фабрики стратегий")
    print("="*60 + "\n")
    
    factory = get_strategy_factory()
    
    # Получаем все стратегии
    print("Доступные стратегии:")
    for strategy in factory.get_all_strategies():
        print(f"  - {strategy.name}")
    
    # Тестируем каждую стратегию
    test_data = {
        "close": 350.0,
        "sma_5": 345.0,
        "volatility_5": 2.5,
        "avg_volatility_20": 3.0,
        "technical_signal": "BUY"
    }
    
    test_news = [
        {"source": "Reuters", "content": "Test news", "sentiment_score": 0.7}
    ]
    
    print("\n📋 Проверка подходящих стратегий:")
    for strategy in factory.get_all_strategies():
        is_suitable = strategy.is_suitable(
            technical_data=test_data,
            news_data=test_news,
            sentiment_score=0.75
        )
        print(f"  {strategy.name}: {'✅ Подходит' if is_suitable else '❌ Не подходит'}")
    
    # Выбор стратегии
    print("\n🎯 Выбор стратегии:")
    selected = factory.select_strategy(
        technical_data=test_data,
        news_data=test_news,
        sentiment_score=0.75
    )
    
    if selected:
        print(f"   Выбрана: {selected.name}")
        result = selected.calculate_signal(
            ticker="MSFT",
            technical_data=test_data,
            news_data=test_news,
            sentiment_score=0.75
        )
        print(f"   Сигнал: {result['signal']}")
        print(f"   Уверенность: {result['confidence']:.2f}")


def example_4_comparison():
    """Пример 4: Сравнение стратегий для разных условий"""
    print("\n" + "="*60)
    print("Пример 4: Сравнение стратегий для разных условий")
    print("="*60 + "\n")
    
    factory = get_strategy_factory()
    
    scenarios = [
        {
            "name": "Трендовый рынок",
            "data": {
                "close": 350.0,
                "sma_5": 345.0,
                "volatility_5": 1.5,
                "avg_volatility_20": 2.5,
                "technical_signal": "BUY"
            },
            "news": [],
            "sentiment": 0.7
        },
        {
            "name": "Волатильный рынок",
            "data": {
                "close": 350.0,
                "sma_5": 340.0,
                "volatility_5": 4.0,
                "avg_volatility_20": 2.5,
                "technical_signal": "BUY"
            },
            "news": [{"source": "MACRO", "content": "Macro event", "sentiment_score": 0.5}],
            "sentiment": 0.5
        },
        {
            "name": "Экстремальная волатильность",
            "data": {
                "close": 350.0,
                "sma_5": 345.0,
                "volatility_5": 5.0,
                "avg_volatility_20": 2.5,
                "technical_signal": "BUY"
            },
            "news": [
                {"source": "US_MACRO", "content": "Important macro", "sentiment_score": 0.8}
            ],
            "sentiment": 0.85
        }
    ]
    
    for scenario in scenarios:
        print(f"\n📊 Сценарий: {scenario['name']}")
        selected = factory.select_strategy(
            technical_data=scenario['data'],
            news_data=scenario['news'],
            sentiment_score=scenario['sentiment']
        )
        
        if selected:
            result = selected.calculate_signal(
                ticker="TEST",
                technical_data=scenario['data'],
                news_data=scenario['news'],
                sentiment_score=scenario['sentiment']
            )
            print(f"   Стратегия: {selected.name}")
            print(f"   Сигнал: {result['signal']}")
            print(f"   Уверенность: {result['confidence']:.2f}")
            print(f"   Стоп-лосс: {result['stop_loss']}%")
            print(f"   Тейк-профит: {result['take_profit']}%")


if __name__ == "__main__":
    example_1_basic_usage()
    example_2_detailed_analysis()
    example_3_direct_factory()
    example_4_comparison()



