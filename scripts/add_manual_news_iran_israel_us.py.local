#!/usr/bin/env python3
"""
Разовая вставка ручных новостей по эскалации Иран–Израиль–США и ожиданиям рынка
для отражения в ленте и при принятии решений по тикерам.

Использование:
  python scripts/add_manual_news_iran_israel_us.py

Данные и ожидания (28.02.2026): координированные удары США и Израиля по Ирану,
ответные удары Ирана; золото на рекордах, нефть с военной премией, акции под давлением.
"""

import sys
from pathlib import Path
from datetime import datetime

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from config_loader import get_database_url


ENTRIES = [
    {
        "ticker": "GC=F",
        "content": (
            "Iran-Israel-US escalation: US and Israel launched coordinated strikes on Iran; "
            "Iran retaliated with missiles at Israel and US base in Bahrain. "
            "Gold hit record highs near $5,020/oz on safe-haven demand. "
            "Expect elevated volatility and potential further upside on escalation headlines; "
            "risk-off supports gold. Next trading day: watch opening gap and VIX."
        ),
        "source": "Manual (market summary)",
        "link": "https://edition.cnn.com/2026/02/19/investing/oil-gold-prices-us-iran-tensions",
        "sentiment_score": 0.75,
        "insight": "Геополитическая эскалация — поддержка спроса на убежища; золото на рекордах. Волатильность высокая.",
        "event_type": "NEWS",
        "importance": "HIGH",
    },
    {
        "ticker": "MSFT",
        "content": (
            "Iran-Israel-US military escalation: global equities under pressure; "
            "S&P 500 and Nasdaq down on risk-off. Tech faces headwinds from geopolitical "
            "uncertainty and inflation fears from oil. Microsoft as mega-cap may see "
            "relative resilience but sector volatility expected. Next session: watch "
            "futures and bond yields; de-escalation headlines could support bounce."
        ),
        "source": "Manual (market summary)",
        "link": "https://www.reuters.com/world/china/global-markets-global-markets-2026-02-27",
        "sentiment_score": 0.40,
        "insight": "Риск-офф и геополитика давят на акции; техсектор под давлением. Ожидается волатильность открытия.",
        "event_type": "NEWS",
        "importance": "HIGH",
    },
    {
        "ticker": "AMD",
        "content": (
            "Middle East tensions (Iran-Israel-US strikes) add to tech/semiconductor headwinds. "
            "AMD recently faced selloff on guidance; geopolitical risk premium increases "
            "volatility for growth and cyclical names. Semiconductors sensitive to risk-off. "
            "Next trading day: monitor opening and sector rotation; any de-escalation may reduce pressure."
        ),
        "source": "Manual (market summary)",
        "link": "https://www.ainvest.com/news/amd-crash-middle-east-tensions-bitcoin-66k-test-flow-analysis-2602",
        "sentiment_score": 0.35,
        "insight": "Геополитика усиливает волатильность полупроводников; риск-офф негативен для growth.",
        "event_type": "NEWS",
        "importance": "HIGH",
    },
    {
        "ticker": "MU",
        "content": (
            "Iran-Israel-US escalation: semiconductors and tech under pressure. "
            "Micron has significant geopolitical exposure (Taiwan dependency per 10-K); "
            "international operations and Middle East risk premium add to volatility. "
            "Oil and gold up; equities and semis down on risk-off. Next session: "
            "watch Strait of Hormuz headlines and broad market open."
        ),
        "source": "Manual (market summary)",
        "link": "https://www.trefis.com/stock/mu/articles2/590820/the-hidden-dangers-facing-micron-technology-stock/2026-02-14",
        "sentiment_score": 0.38,
        "insight": "MU с геополитическим риском (Тайвань); эскалация Ближнего Востока давит на сектор.",
        "event_type": "NEWS",
        "importance": "HIGH",
    },
    {
        "ticker": "SNDK",
        "content": (
            "Market context: Iran-Israel-US military strikes drive risk-off; gold at records, "
            "oil up on Hormuz risk. Semiconductors and tech face volatility. "
            "Relevant for memory/semiconductor names: geopolitical premium and broad equity pressure "
            "expected into next trading day. Watch futures and escalation/de-escalation news."
        ),
        "source": "Manual (market summary)",
        "link": "https://markets.financialcontent.com/ms.intelvalue/article/marketminute-2026-2-18-global-markets-on-edge-as-iranian-tensions",
        "sentiment_score": 0.40,
        "insight": "Общий риск-офф и геополитика; полупроводники под давлением. Ожидания волатильного открытия.",
        "event_type": "NEWS",
        "importance": "HIGH",
    },
]


def main():
    engine = create_engine(get_database_url())
    with engine.begin() as conn:
        for e in ENTRIES:
            conn.execute(
                text("""
                    INSERT INTO knowledge_base (ts, ticker, source, content, sentiment_score, insight, event_type, importance, link)
                    VALUES (:ts, :ticker, :source, :content, :sentiment_score, :insight, :event_type, :importance, :link)
                """),
                {
                    "ts": datetime.now(),
                    "ticker": e["ticker"],
                    "source": e["source"],
                    "content": e["content"][:8000],
                    "sentiment_score": float(e["sentiment_score"]),
                    "insight": e["insight"][:2000] if e.get("insight") else None,
                    "event_type": e.get("event_type", "NEWS"),
                    "importance": e.get("importance", "HIGH"),
                    "link": (e.get("link") or "")[:2000],
                },
            )
    print(f"✅ Добавлено {len(ENTRIES)} ручных новостей (Иран–Израиль–США, ожидания рынка) для тикеров: {', '.join(x['ticker'] for x in ENTRIES)}")
    print("   Тикеры: GC=F, MSFT, AMD, MU, SNDK. Проверьте ленту и дашборд.")


if __name__ == "__main__":
    main()
