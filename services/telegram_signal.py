"""
Общая отправка сигналов в Telegram (cron-скрипты и уведомления о сделках).
Используется: send_sndk_signal_cron (GAME_5M), trading_cycle_cron (портфельная игра).
"""
import logging
import urllib.error
import urllib.parse
import urllib.request

from config_loader import get_config_value

logger = logging.getLogger(__name__)

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def get_telegram_urllib_opener() -> urllib.request.OpenerDirector:
    """
    Opener для HTTPS к api.telegram.org. Учитывает TELEGRAM_PROXY_URL (http/https),
    как и run_telegram_bot.py для polling. SOCKS в urllib без PySocks не поддерживается —
    для кронов используйте HTTP-прокси (например локальный 8081 рядом с SOCKS).
    """
    raw = (get_config_value("TELEGRAM_PROXY_URL") or "").strip()
    if raw and "socks" not in raw.lower():
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": raw, "http": raw})
        )
    if raw:
        logger.debug(
            "TELEGRAM_PROXY_URL=socks:// — кроны отправляют через urllib; "
            "задайте TELEGRAM_PROXY_URL=http://... (HTTP-прокси к Telegram), см. run_telegram_bot.py."
        )
    return urllib.request.build_opener()


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


TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def send_telegram_message(
    token: str, chat_id: str, text: str, parse_mode: str | None = "Markdown"
) -> bool:
    """Отправить сообщение в Telegram. Возвращает True при успехе. parse_mode=None — обычный текст."""
    if len(text) > TELEGRAM_MAX_MESSAGE_LENGTH:
        text = text[: TELEGRAM_MAX_MESSAGE_LENGTH - 80] + "\n\n… (сообщение обрезано)"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode("utf-8", errors="replace")
    req = urllib.request.Request(
        TELEGRAM_SEND_URL.format(token=token), data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    try:
        with get_telegram_urllib_opener().open(req, timeout=30) as resp:
            if resp.status != 200:
                logger.error("Telegram API error: %s %s", resp.status, resp.read())
                return False
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.error("Telegram HTTP %s (chat_id=%s): %s", e.code, chat_id, body[:500])
        return False
    except Exception as e:
        logger.exception("Send failed: %s", e)
        return False
