"""
Общая отправка сигналов в Telegram (cron-скрипты и уведомления о сделках).
Используется: send_sndk_signal_cron (GAME_5M), trading_cycle_cron (портфельная игра).
"""
import logging
import urllib.parse
import urllib.request

from config_loader import get_config_value

logger = logging.getLogger(__name__)

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_signal_chat_ids() -> list[str]:
    """Список chat_id для рассылки сигналов. Без дубликатов."""
    ids_raw = get_config_value("TELEGRAM_SIGNAL_CHAT_IDS", "").strip()
    if ids_raw:
        raw_list = [x.strip() for x in ids_raw.split(",") if x.strip()]
        seen = set()
        return [x for x in raw_list if x not in seen and not seen.add(x)]
    single = get_config_value("TELEGRAM_SIGNAL_CHAT_ID", "").strip()
    if single:
        return [single]
    dashboard = get_config_value("TELEGRAM_DASHBOARD_CHAT_ID", "").strip()
    if dashboard:
        return [dashboard]
    allowed = get_config_value("TELEGRAM_ALLOWED_USERS", "")
    if allowed:
        return [allowed.split(",")[0].strip()]
    return []


def send_telegram_message(
    token: str, chat_id: str, text: str, parse_mode: str = "Markdown"
) -> bool:
    """Отправить сообщение в Telegram. Возвращает True при успехе."""
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    ).encode()
    req = urllib.request.Request(
        TELEGRAM_SEND_URL.format(token=token), data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                logger.error("Telegram API error: %s %s", resp.status, resp.read())
                return False
            return True
    except Exception as e:
        logger.exception("Send failed: %s", e)
        return False
