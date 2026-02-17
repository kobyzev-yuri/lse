#!/usr/bin/env python3
"""
Пример использования LLM guidance для выбора торговой стратегии
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from analyst_agent import AnalystAgent
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def main():
    """Пример использования LLM guidance"""
    
    # Создаем агента с LLM поддержкой
    agent = AnalystAgent(use_llm=True)
    
    # Тестируем на разных тикерах
    test_tickers = ["MSFT", "SNDK"]
    
    for ticker in test_tickers:
        print(f"\n{'='*60}")
        print(f"Анализ для {ticker}")
        print(f"{'='*60}\n")
        
        # Получаем расширенный анализ с LLM guidance
        result = agent.get_decision_with_llm(ticker)
        
        print(f"\n📊 Результаты анализа:")
        print(f"   Решение: {result['decision']}")
        print(f"   Технический сигнал: {result['technical_signal']}")
        print(f"   Sentiment: {result['sentiment']:.3f}")
        
        # Выводим LLM guidance (стратегию)
        if result.get('llm_guidance'):
            guidance = result['llm_guidance']
            print(f"\n🤖 LLM Guidance (стратегия):")
            print(f"   Стратегия: {guidance.get('strategy', 'N/A')}")
            print(f"   Уверенность: {guidance.get('confidence', 0):.2f}")
            print(f"   Обоснование: {guidance.get('reasoning', 'N/A')}")
            
            if guidance.get('entry_price'):
                print(f"   Рекомендуемая цена входа: ${guidance['entry_price']:.2f}")
            if guidance.get('stop_loss'):
                print(f"   Рекомендуемый стоп-лосс: {guidance['stop_loss']:.2f}%")
            if guidance.get('take_profit'):
                print(f"   Рекомендуемый тейк-профит: {guidance['take_profit']:.2f}%")
        
        # Выводим детальный LLM анализ
        if result.get('llm_analysis'):
            llm_analysis = result['llm_analysis']
            print(f"\n📈 Детальный LLM анализ:")
            print(f"   Рекомендация: {llm_analysis.get('decision', 'N/A')}")
            print(f"   Уверенность: {llm_analysis.get('confidence', 0):.2f}")
            print(f"   Обоснование: {llm_analysis.get('reasoning', 'N/A')[:200]}...")
            
            if llm_analysis.get('risks'):
                print(f"   Риски: {', '.join(llm_analysis['risks'])}")
            if llm_analysis.get('key_factors'):
                print(f"   Ключевые факторы: {', '.join(llm_analysis['key_factors'])}")
        
        print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()



