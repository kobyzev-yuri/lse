#!/usr/bin/env python3
"""
Проверка «доски опционов» Polygon vs эталон Задачи 1 (Investing Option Chain).

Запуск на VM:
  docker exec lse-bot python scripts/verify_polygon_options_chain.py --ticker MU

После оплаты подписки Polygon Options ожидаем:
  - snapshot HTTP 200 (не 403)
  - contracts > 0 на выбранной экспирации
  - поля strike, call/put, volume, open_interest, bid/ask (как Investing.com)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

# Поля, которые есть на Investing Option Chain и нужны для сентимента (Задача 1)
INVESTING_EQUIVALENT_FIELDS = (
    "strike",
    "contract_type",
    "volume",
    "open_interest",
    "bid",
    "ask",
    "last",
    "underlying_price",
)


def _check_yfinance(ticker: str, expiration: str | None) -> Dict[str, Any]:
    try:
        import yfinance as yf
    except ImportError:
        return {"status": "skip", "error": "yfinance not installed"}

    t = yf.Ticker(ticker)
    exps = list(getattr(t, "options", []) or [])
    exp = expiration or (exps[0] if exps else None)
    if not exp:
        return {"status": "error", "error": "no yfinance expirations"}
    ch = t.option_chain(exp)
    calls, puts = len(ch.calls), len(ch.puts)
    sample_put = None
    if len(ch.puts):
        mid = ch.puts.iloc[len(ch.puts) // 2]
        sample_put = {
            "strike": float(mid["strike"]),
            "volume": float(mid.get("volume") or 0),
            "openInterest": float(mid.get("openInterest") or 0),
            "bid": float(mid.get("bid") or 0),
            "ask": float(mid.get("ask") or 0),
        }
    return {
        "status": "ok",
        "source": "yfinance",
        "url_hint": f"https://finance.yahoo.com/quote/{ticker}/options",
        "expiration_date": exp,
        "expirations_count": len(exps),
        "calls": calls,
        "puts": puts,
        "sample_put": sample_put,
        "note": "Справочное сравнение; не заменяет Polygon в прод-сентименте.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify Polygon options chain vs Task 1 (Investing board)")
    ap.add_argument("--ticker", default="MU")
    ap.add_argument("--expiration", default=None, help="YYYY-MM-DD; default nearest from Polygon ref")
    ap.add_argument("--compare-yfinance", action="store_true", help="Also fetch yfinance chain counts")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    ticker = args.ticker.strip().upper()
    from services.polygon_options import (
        fetch_option_expiration_dates,
        fetch_options_chain_snapshot,
        polygon_options_available,
    )

    report: Dict[str, Any] = {
        "ticker": ticker,
        "investing_url": f"https://www.investing.com/equities/micron-tech-options"
        if ticker == "MU"
        else f"https://www.investing.com/equities/{ticker.lower()}-options",
        "polygon_snapshot_docs": "https://polygon.io/docs/options/get_v3_snapshot_options__underlyingasset",
        "key_configured": polygon_options_available(),
    }

    exps = fetch_option_expiration_dates(ticker)
    report["polygon_reference"] = {
        "status": "ok" if exps else "empty",
        "expirations_count": len(exps),
        "first_expirations": exps[:10],
        "note": "Reference API — только метаданные контрактов (страйк/тип/дата), без live volume/OI.",
    }

    exp = args.expiration or (exps[0] if exps else None)
    report["test_expiration"] = exp

    if not exp:
        report["polygon_snapshot"] = {"status": "error", "error": "no expiration dates"}
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(report)
        return 1

    snap = fetch_options_chain_snapshot(ticker, expiration_date=exp, limit=250)
    report["polygon_snapshot"] = {
        "status": snap.get("status"),
        "error": snap.get("error"),
        "polygon_status": snap.get("polygon_status"),
        "underlying_price": snap.get("underlying_price"),
        "contract_count": snap.get("contract_count"),
    }

    contracts: List[Dict[str, Any]] = snap.get("contracts") or []
    if contracts:
        calls = [c for c in contracts if c.get("contract_type") == "call"]
        puts = [c for c in contracts if c.get("contract_type") == "put"]
        oi_pos = sum(1 for c in contracts if (c.get("open_interest") or 0) > 0)
        vol_pos = sum(1 for c in contracts if (c.get("volume") or 0) > 0)
        bid_pos = sum(1 for c in contracts if c.get("bid") is not None)
        mid = contracts[len(contracts) // 2]
        report["polygon_snapshot"].update(
            {
                "calls": len(calls),
                "puts": len(puts),
                "contracts_with_oi": oi_pos,
                "contracts_with_volume": vol_pos,
                "contracts_with_bid": bid_pos,
                "sample_contract": mid,
                "investing_equivalent_fields_present": {
                    f: f in mid for f in INVESTING_EQUIVALENT_FIELDS
                },
                "verdict": "OK — доска как Investing (snapshot с volume/OI/bid/ask)",
            }
        )
    elif snap.get("error") and "403" in str(snap.get("error")):
        report["polygon_snapshot"]["verdict"] = (
            "FAIL — 403: нужна подписка Polygon Options (Starter+). "
            "Reference API работает, но live-таблицы нет."
        )
    else:
        report["polygon_snapshot"]["verdict"] = "FAIL — пустой snapshot или ошибка API"

    if args.compare_yfinance:
        report["yfinance"] = _check_yfinance(ticker, exp)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"Ticker: {ticker}  |  Investing: {report['investing_url']}")
        print(f"Polygon key: {report['key_configured']}")
        ref = report["polygon_reference"]
        print(f"Reference expirations: {ref['expirations_count']}  sample: {ref.get('first_expirations', [])[:5]}")
        ps = report["polygon_snapshot"]
        print(f"Snapshot exp={exp}: status={ps.get('status')} contracts={ps.get('contract_count')}")
        if ps.get("error"):
            print(f"  error: {ps['error']}")
        print(f"  VERDICT: {ps.get('verdict')}")
        if args.compare_yfinance and "yfinance" in report:
            yf = report["yfinance"]
            print(f"yfinance: exp={yf.get('expiration_date')} calls={yf.get('calls')} puts={yf.get('puts')}")

    ok = (
        report.get("polygon_snapshot", {}).get("status") == "ok"
        and (report.get("polygon_snapshot", {}).get("contract_count") or 0) > 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
