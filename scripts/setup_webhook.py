#!/usr/bin/env python3
"""
Скрипт для настройки Telegram Bot webhook
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
import requests
from config_loader import get_config_value

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def setup_webhook(webhook_url: str):
    """
    Настраивает webhook для Telegram бота
    
    Args:
        webhook_url: Полный URL webhook endpoint (например, https://your-service.run.app/webhook)
    """
    bot_token = get_config_value('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в config.env")
    
    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    data = {
        "url": webhook_url,
        "allowed_updates": ["message", "callback_query"]
    }
    
    logger.info(f"🔗 Настройка webhook: {webhook_url}")
    
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        
        if result.get('ok'):
            logger.info("✅ Webhook успешно настроен")
            logger.info(f"   URL: {webhook_url}")
            
            # Проверяем информацию о webhook
            info_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
            info_response = requests.get(info_url, timeout=10)
            if info_response.status_code == 200:
                info = info_response.json()
                if info.get('ok'):
                    webhook_info = info.get('result', {})
                    logger.info(f"   Pending updates: {webhook_info.get('pending_update_count', 0)}")
                    if webhook_info.get('last_error_message'):
                        logger.warning(f"   Last error: {webhook_info.get('last_error_message')}")
        else:
            logger.error(f"❌ Ошибка настройки webhook: {result.get('description', 'Unknown error')}")
            return False
        
        return True
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Telegram API: {e}")
        return False


def delete_webhook():
    """Удаляет webhook (переключает на polling режим)"""
    bot_token = get_config_value('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в config.env")
    
    url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    
    logger.info("🗑️ Удаление webhook...")
    
    try:
        response = requests.post(url, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        
        if result.get('ok'):
            logger.info("✅ Webhook удален")
            return True
        else:
            logger.error(f"❌ Ошибка удаления webhook: {result.get('description', 'Unknown error')}")
            return False
        
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к Telegram API: {e}")
        return False


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Настройка Telegram Bot webhook')
    parser.add_argument('--url', type=str, help='URL webhook endpoint')
    parser.add_argument('--delete', action='store_true', help='Удалить webhook')
    
    args = parser.parse_args()
    
    if args.delete:
        delete_webhook()
    elif args.url:
        setup_webhook(args.url)
    else:
        # Пытаемся получить URL из переменной окружения
        webhook_url = get_config_value('TELEGRAM_WEBHOOK_URL')
        if webhook_url:
            setup_webhook(webhook_url)
        else:
            print("❌ Укажите --url или установите TELEGRAM_WEBHOOK_URL в config.env")
            print("\nПример:")
            print("  python scripts/setup_webhook.py --url https://your-service.run.app/webhook")
            print("\nИли установите в config.env:")
            print("  TELEGRAM_WEBHOOK_URL=https://your-service.run.app/webhook")
