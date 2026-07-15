"""Ticker universe for ~6m chart-shape clustering (Настя TZ, Jul 2026).

Asterisks in the chat list were accidental — treat all names equally.
"""
from __future__ import annotations

from typing import List

# Order preserved from nastya/tz.txt (unique, upper).
NASTYA_SHAPE_TICKERS: List[str] = [
    "AAOI",
    "AEIS",
    "ALAB",
    "AMD",
    "AMKR",
    "AMZN",
    "ANET",
    "ARM",
    "ASML",
    "AVGO",
    "CDNS",
    "CIEN",
    "COHR",
    "CRDO",
    "CRWV",
    "DDOG",
    "DELL",
    "ENTG",
    "GOOGL",
    "INTC",
    "INTU",
    "KLAC",
    "LITE",
    "LRCX",
    "META",
    "MRVL",
    "MSFT",
    "MU",
    "MXL",
    "NBIS",
    "NOW",
    "NVDA",
    "ONTO",
    "ORCL",
    "PLTR",
    "QCOM",
    "RBLX",
    "SMCI",
    "SNDK",
    "SNPS",
    "TSM",
    "WDC",
]


def shape_cluster_tickers() -> List[str]:
    return list(NASTYA_SHAPE_TICKERS)
