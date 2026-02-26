#!/usr/bin/env python3
"""
Скрипт для локального запуска Telegram бота в режиме polling
Для разработки и тестирования
"""

import os
import sys
from pathlib import Path

# httpx (используется python-telegram-bot) не поддерживает прокси socks:// без httpx[socks].
# Убираем socks-прокси из окружения, чтобы избежать ValueError: Unknown scheme for proxy URL.
for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    val = os.environ.pop(key, None)
    if val and "socks" not in val.lower():
        os.environ[key] = val  # восстанавливаем не-socks прокси

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from config_loader import get_config_value
from services.telegram_bot import LSETelegramBot

# Прокси для Telegram API (если api.telegram.org недоступен напрямую)
_telegram_proxy = get_config_value("TELEGRAM_PROXY_URL", "").strip()
if _telegram_proxy:
    os.environ["HTTPS_PROXY"] = _telegram_proxy
    os.environ["HTTP_PROXY"] = _telegram_proxy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """Запуск бота в режиме polling"""
    bot_token = get_config_value('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        logger.error("❌ TELEGRAM_BOT_TOKEN не найден в config.env")
        logger.info("   Создайте бота через @BotFather и добавьте токен в config.env")
        sys.exit(1)
    
    # Получаем список разрешенных пользователей (опционально)
    allowed_users_str = get_config_value('TELEGRAM_ALLOWED_USERS', '')
    allowed_users = None
    if allowed_users_str:
        try:
            allowed_users = [int(uid.strip()) for uid in allowed_users_str.split(',') if uid.strip()]
            logger.info(f"✅ Доступ ограничен для пользователей: {allowed_users}")
        except ValueError:
            logger.warning("⚠️ Неверный формат TELEGRAM_ALLOWED_USERS, доступ для всех")
    
    # Создаем и запускаем бота
    bot = LSETelegramBot(token=bot_token, allowed_users=allowed_users)
    
    logger.info("=" * 60)
    logger.info("🤖 LSE Telegram Bot запущен в режиме polling")
    logger.info("   Для остановки нажмите Ctrl+C")
    logger.info("=" * 60)
    
    if _telegram_proxy:
        logger.info("Прокси для Telegram: %s", _telegram_proxy.split("@")[-1] if "@" in _telegram_proxy else _telegram_proxy)
    try:
        bot.run_polling()
    except KeyboardInterrupt:
        logger.info("\n👋 Остановка бота...")
    except Exception as e:
        err_str = str(e).lower()
        if "connect" in err_str or "connection" in err_str or "network" in err_str:
            logger.error("❌ Нет соединения с api.telegram.org: %s", e)
            logger.info("   Проверьте интернет и файрвол. Если Telegram заблокирован, задайте в config.env:")
            logger.info("   TELEGRAM_PROXY_URL=http://proxy:port  (или https://...)")
        else:
            logger.error("❌ Критическая ошибка: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
