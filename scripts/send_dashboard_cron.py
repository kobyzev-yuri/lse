#!/usr/bin/env python3
"""
Проактивная рассылка дашборда в Telegram по расписанию (cron).

Настройка:
  config.env:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_DASHBOARD_CHAT_ID=123456789   # куда слать дашборд (обязательно для рассылки)
    # или использует первый TELEGRAM_ALLOWED_USERS если CHAT_ID не задан

Cron (пример — 3 раза в день в торговые часы):
  0 10,14,18 * * 1-5 cd /path/to/lse && python scripts/send_dashboard_cron.py

Режим: передать all | 5m | daily первым аргументом (по умолч. all).
  python scripts/send_dashboard_cron.py 5m
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import urllib.parse
import urllib.request

from config_loader import get_config_value
from services.dashboard_builder import build_dashboard_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(token: str, chat_id: str, text: str, parse_mode: str = "Markdown") -> bool:
    """Отправляет сообщение в чат через Telegram Bot API."""
    if len(text) > MAX_MESSAGE_LENGTH:
        parts = [text[i : i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]
    else:
        parts = [text]
    url = TELEGRAM_SEND_URL.format(token=token)
    for part in parts:
        data = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": part, "parse_mode": parse_mode}
        ).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    logger.error("Telegram API error: %s %s", resp.status, resp.read())
                    return False
        except Exception as e:
            logger.exception("Send failed: %s", e)
            return False
    return True


def main():
    mode = "all"
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("5m", "daily", "all"):
        mode = sys.argv[1].strip().lower()

    token = get_config_value("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN не задан в config.env")
        sys.exit(1)

    chat_id = get_config_value("TELEGRAM_DASHBOARD_CHAT_ID", "").strip()
    if not chat_id:
        allowed = get_config_value("TELEGRAM_ALLOWED_USERS", "")
        if allowed:
            chat_id = allowed.split(",")[0].strip()
        if not chat_id:
            logger.error("Задайте TELEGRAM_DASHBOARD_CHAT_ID или TELEGRAM_ALLOWED_USERS в config.env")
            sys.exit(1)

    logger.info("Сбор дашборда (mode=%s), отправка в chat_id=%s", mode, chat_id)
    try:
        text = build_dashboard_text(mode)
    except Exception as e:
        logger.exception("Ошибка сборки дашборда: %s", e)
        sys.exit(1)

    if send_telegram_message(token, chat_id, text):
        logger.info("Дашборд отправлен")
    else:
        logger.error("Не удалось отправить дашборд")
        sys.exit(1)


if __name__ == "__main__":
    main()
