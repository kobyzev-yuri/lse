#!/usr/bin/env python3
"""
Скрипт для локального запуска Telegram бота в режиме polling
Для разработки и тестирования
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from services.telegram_bot import LSETelegramBot
from config_loader import get_config_value

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
    
    try:
        bot.run_polling()
    except KeyboardInterrupt:
        logger.info("\n👋 Остановка бота...")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
