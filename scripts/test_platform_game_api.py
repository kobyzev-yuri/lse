#!/usr/bin/env python3
"""
Проверка связи LSE → Platform POST /game из того же окружения, что и бот (в т.ч. внутри docker exec lse-bot).

Примеры:
  # URL из config.env (PLATFORM_GAME_API_URL)
  python scripts/test_platform_game_api.py

  # Явный URL (удобно до правки config.env)
  python scripts/test_platform_game_api.py --url http://172.17.0.1:18080/game

  # Другой тикер/цены
  python scripts/test_platform_game_api.py --instrument MU --entry 380 --take 395 --stop 370 --units 10

Шаги деплоя: docs/PLATFORM_GAME_DOCKER.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from config_loader import get_config_value


def main() -> int:
    p = argparse.ArgumentParser(description="POST /game smoke test (Kerim Platform)")
    p.add_argument(
        "--url",
        default="",
        help="Полный URL POST /game (по умолчанию: PLATFORM_GAME_API_URL из config.env)",
    )
    p.add_argument("--instrument", default="TSLA")
    p.add_argument("--entry", type=float, default=305.0, help="условная цена входа (для TP/SL в примере)")
    p.add_argument("--take", type=float, default=320.0, help="take profit price")
    p.add_argument("--stop", type=float, default=290.0, help="stop loss price")
    p.add_argument("--units", type=int, default=5)
    p.add_argument("--dry-run", action="store_true", help="только напечатать JSON тела, без HTTP")
    args = p.parse_args()

    url = (args.url or get_config_value("PLATFORM_GAME_API_URL", "") or "").strip()
    if not url and not args.dry_run:
        print(
            "Задайте --url или PLATFORM_GAME_API_URL в config.env.\n"
            "Пример для Linux (хост слушает 18080): http://172.17.0.1:18080/game",
            file=sys.stderr,
        )
        return 2

    body = {
        "positions": [
            {
                "orderType": "MARKET",
                "market": {
                    "instrument": args.instrument.upper(),
                    "direction": "LONG",
                    "createdAt": "2026-03-21T12:00:00Z",
                    "takeProfit": float(args.take),
                    "stopLoss": float(args.stop),
                    "units": int(args.units),
                },
            }
        ]
    }

    print("Request POST", url or "(dry-run)")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0

    import requests

    try:
        timeout = float((get_config_value("PLATFORM_GAME_API_TIMEOUT_SEC", "15") or "15").strip())
    except (ValueError, TypeError):
        timeout = 15.0
    r = requests.post(url, json=body, timeout=timeout)
    print("HTTP", r.status_code, r.reason)
    try:
        out = r.json()
        print(json.dumps(out, indent=2, ensure_ascii=False))
    except Exception:
        print(r.text[:2000])
        return 1 if not r.ok else 0
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
