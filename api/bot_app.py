"""
FastAPI приложение для Telegram Bot webhook
Развертывается на Google Cloud Run
"""

import sys
from pathlib import Path

# Добавляем корневую директорию проекта в путь
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.error import TelegramError

from services.telegram_bot import LSETelegramBot
from config_loader import get_config_value

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _get_bot_config():
    token = get_config_value('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не найден в config.env")
    allowed_users = None
    allowed_users_str = get_config_value('TELEGRAM_ALLOWED_USERS', '')
    if allowed_users_str:
        try:
            allowed_users = [int(uid.strip()) for uid in allowed_users_str.split(',') if uid.strip()]
        except ValueError:
            logger.warning("⚠️ Неверный формат TELEGRAM_ALLOWED_USERS, доступ для всех")
    return token, allowed_users


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Создание бота и инициализация Application в том же event loop, что и uvicorn."""
    token, allowed_users = _get_bot_config()
    bot = LSETelegramBot(token=token, allowed_users=allowed_users)
    app.state.bot = bot
    await bot.application.initialize()
    logger.info("✅ LSE Telegram Bot API инициализирован (webhook)")
    try:
        yield
    finally:
        await bot.application.shutdown()


app = FastAPI(title="LSE Telegram Bot API", lifespan=lifespan)


@app.get("/")
async def root():
    """Health check endpoint"""
    return {
        "status": "ok",
        "service": "LSE Telegram Bot API",
        "version": "1.0.0"
    }


@app.get("/health")
async def health():
    """Health check для Cloud Run"""
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Webhook endpoint для Telegram Bot API
    
    Telegram отправляет updates на этот endpoint
    """
    bot = request.app.state.bot
    try:
        data = await request.json()
        update = Update.de_json(data, bot.application.bot)
        await bot.application.process_update(update)
        return JSONResponse({"ok": True})
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/webhook/info")
async def webhook_info(request: Request):
    """Информация о webhook"""
    bot = request.app.state.bot
    try:
        wh_info = await bot.application.bot.get_webhook_info()
        return {
            "url": wh_info.url,
            "has_custom_certificate": wh_info.has_custom_certificate,
            "pending_update_count": wh_info.pending_update_count,
            "last_error_date": wh_info.last_error_date.isoformat() if wh_info.last_error_date else None,
            "last_error_message": wh_info.last_error_message,
            "max_connections": wh_info.max_connections,
            "allowed_updates": wh_info.allowed_updates
        }
    except Exception as e:
        logger.error(f"Error getting webhook info: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    
    port = int(get_config_value('PORT', '8080'))
    host = get_config_value('HOST', '0.0.0.0')
    
    logger.info(f"🚀 Запуск LSE Telegram Bot API на {host}:{port}")
    uvicorn.run(app, host=host, port=port)
