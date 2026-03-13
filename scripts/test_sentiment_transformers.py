#!/usr/bin/env python3
"""
Проверка расчёта sentiment при SENTIMENT_METHOD=transformers и SENTIMENT_MODEL=ProsusAI/finbert.

Перед запуском в config.env задайте:
  SENTIMENT_METHOD=transformers
  SENTIMENT_MODEL=ProsusAI/finbert

Запуск:
  python scripts/test_sentiment_transformers.py

При первом запуске модель скачается с HuggingFace (нужны transformers и torch).
Insight при transformers всегда None; при переходе на LLM-вход инсайт будет давать уже LLM.
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_config_value
from services.sentiment_analyzer import calculate_sentiment


SAMPLES = [
    ("Позитив", "Apple reported record quarterly revenue and raised its dividend. CEO said demand for iPhone remains strong."),
    ("Негатив", "Company cuts full-year guidance amid declining sales. Shares tumble 15% in pre-market."),
    ("Нейтраль", "The board will hold a meeting next week to discuss the strategic plan."),
]


def main():
    method = (get_config_value("SENTIMENT_METHOD", "llm") or "llm").strip().lower()
    model = (get_config_value("SENTIMENT_MODEL", "") or "ProsusAI/finbert").strip()
    print(f"SENTIMENT_METHOD={method}")
    print(f"SENTIMENT_MODEL={model or '(default FinBERT)'}")
    print("-" * 60)
    if method != "transformers":
        print("В config.env задайте SENTIMENT_METHOD=transformers для теста бесплатной модели.")
        print("Иначе будет использоваться LLM (если USE_LLM и API ключ заданы).")
        print("-" * 60)
    for label, text in SAMPLES:
        score, insight = calculate_sentiment(text)
        insight_str = f" | insight: {insight[:80]}..." if insight else " | insight: —"
        print(f"[{label}] sentiment={score:.3f}{insight_str}")
        print(f"      text: {text[:70]}...")
    print("-" * 60)
    print("Готово. При transformers insight всегда None — при LLM-входе инсайт будет из решения LLM.")


if __name__ == "__main__":
    main()
