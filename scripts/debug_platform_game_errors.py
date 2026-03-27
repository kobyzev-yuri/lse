#!/usr/bin/env python3
from datetime import datetime
import json
import requests
from services.ticker_groups import get_tickers_game_5m
from services.recommend_5m import get_5m_technical_signal
from services.game_5m import GAME_NOTIONAL_USD

url = "http://platform-game:8080/game"
created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def mk_pos(tkr, tech):
    price = float(tech.get("price"))
    decision = str((tech.get("technical_decision_effective") or tech.get("decision") or "HOLD")).upper()
    direction = "SHORT" if decision == "SELL" else "LONG"
    take_pct = float(tech.get("take_profit_pct") or 5.0)
    stop_pct = float(tech.get("stop_loss_pct") or 2.0)
    units = max(1, int(GAME_NOTIONAL_USD / price))
    if direction == "SHORT":
        take_price = price * (1.0 - take_pct / 100.0)
        stop_price = price * (1.0 + stop_pct / 100.0)
    else:
        take_price = price * (1.0 + take_pct / 100.0)
        stop_price = price * (1.0 - stop_pct / 100.0)
    return {
        "orderType": "MARKET",
        "market": {
            "instrument": tkr,
            "direction": direction,
            "createdAt": created_at,
            "takeProfit": round(float(take_price), 4),
            "stopLoss": round(float(stop_price), 4),
            "units": int(units),
        },
    }

for t in (get_tickers_game_5m() or []):
    tech = get_5m_technical_signal(t, days=5, use_llm_news=False) or {}
    if not tech.get("price"):
        continue
    pos = mk_pos(t, tech)
    r = requests.post(url, json={"positions": [pos]}, timeout=30)
    if r.status_code >= 400:
        print("="*80)
        print("TICKER:", t, "STATUS:", r.status_code)
        print("REQUEST JSON:")
        print(json.dumps({"positions": [pos]}, ensure_ascii=False, indent=2))
        print("RESPONSE BODY:")
        print((r.text or "")[:2000])
