#!/usr/bin/env python3
"""
Опциональный крон: премаркет-поллинг за 30–60 мин до открытия US.
Собирает контекст премаркета по TICKERS_FAST (и опционально тикерам портфельной игры),
логирует в logs/premarket_cron.log. При PREMARKET_ALERT_TELEGRAM=true отправляет алерт в Telegram.

Запуск вручную: python scripts/premarket_cron.py
Cron (пример: 8:30 ET = 13:30 UTC зимой): 30 13 * * 1-5  cd /path/to/lse && python scripts/premarket_cron.py
Настройка часов: подстройте под свой часовой пояс (см. setup_cron.sh).
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from datetime import datetime

log_dir = project_root / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "premarket_cron.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def main() -> None:
    try:
        from services.market_session import get_market_session_context
        from services.premarket import get_premarket_context
        from services.ticker_groups import get_tickers_fast, get_tickers_for_portfolio_game
        from config_loader import get_config_value
    except ImportError as e:
        logger.error("Импорт: %s", e)
        sys.exit(1)

    ctx = get_market_session_context()
    phase = (ctx.get("session_phase") or "").strip()
    if phase != "PRE_MARKET":
        logger.info("Сейчас не премаркет (phase=%s), выход", phase)
        sys.exit(0)

    # Тикеры: FAST + опционально портфельные (без дубликатов)
    fast = get_tickers_fast()
    try:
        portfolio = get_tickers_for_portfolio_game()
    except Exception:
        portfolio = []
    seen = set()
    tickers = []
    for t in fast + portfolio:
        if t not in seen:
            seen.add(t)
            tickers.append(t)

    if not tickers:
        logger.warning("Нет тикеров для премаркета")
        sys.exit(0)

    results = []
    for ticker in tickers:
        try:
            pm = get_premarket_context(ticker)
            if pm.get("error"):
                logger.warning("%s: %s", ticker, pm.get("error"))
                continue
            prev = pm.get("prev_close")
            last = pm.get("premarket_last")
            gap = pm.get("premarket_gap_pct")
            mins = pm.get("minutes_until_open")
            results.append((ticker, prev, last, gap, mins))
            logger.info(
                "%s: prev_close=%s premarket_last=%s gap_pct=%s min_to_open=%s",
                ticker, prev, last, gap, mins,
            )
        except Exception as e:
            logger.warning("%s: %s", ticker, e)

    # Опционально: алерт в Telegram
    alert = get_config_value("PREMARKET_ALERT_TELEGRAM", "").strip().lower() in ("1", "true", "yes")
    if alert and results:
        token = get_config_value("TELEGRAM_BOT_TOKEN", "").strip()
        chat_ids_raw = get_config_value("TELEGRAM_SIGNAL_CHAT_IDS", "") or get_config_value("TELEGRAM_SIGNAL_CHAT_ID", "")
        chat_ids = [x.strip() for x in chat_ids_raw.split(",") if x.strip()]
        if token and chat_ids:
            lines = ["📊 **Премаркет** (до открытия US)"]
            for ticker, prev, last, gap, mins in results:
                gap_str = f"{gap:+.2f}%" if gap is not None else "—"
                mins_str = f"{mins} мин" if mins is not None else "—"
                lines.append(f"• {ticker}: премаркет {last} гэп {gap_str} до открытия {mins_str}")
            text = "\n".join(lines)
            import urllib.parse
            import urllib.request
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_ids[0], "text": text, "parse_mode": "Markdown"}).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        logger.info("Премаркет-алерт отправлен в Telegram")
                    else:
                        logger.warning("Telegram: %s %s", resp.status, resp.read())
            except Exception as e:
                logger.warning("Отправка алерта: %s", e)
        else:
            logger.debug("PREMARKET_ALERT_TELEGRAM=true, но нет TELEGRAM_BOT_TOKEN или chat id")

    sys.exit(0)


if __name__ == "__main__":
    main()
