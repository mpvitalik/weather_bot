import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, ErrorEvent

from config import BOT_TOKEN, OPENWEATHER_API_KEY
from middlewares import ThrottlingMiddleware
from weather_service import WeatherService
from database import init_db
from handlers import router
from scheduler import start_weather_monitor

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("WeatherBot")

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("❌ TELEGRAM BOT TOKEN is missing!")
        logger.error("Please set BOT_TOKEN in your .env file (obtain one from @BotFather in Telegram).")
        sys.exit(1)

    if not OPENWEATHER_API_KEY:
        logger.error("❌ OPENWEATHER_API_KEY is missing! Set it in your .env file.")
        sys.exit(1)

    logger.info("Initializing SQLite database...")
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.message.middleware(ThrottlingMiddleware(0.7))
    dp.callback_query.middleware(ThrottlingMiddleware(0.4))
    dp.include_router(router)

    @dp.error()
    async def on_error(event: ErrorEvent):
        logger.error("Unhandled error while processing update: %s", event.exception, exc_info=event.exception)
        update = event.update
        target = update.message or (update.callback_query.message if update.callback_query else None)
        if target is not None and hasattr(target, "answer"):
            try:
                await target.answer("⚠️ Произошла ошибка. Попробуйте ещё раз чуть позже.")
            except Exception:
                pass
        return True

    # Set bot commands in Telegram UI menu
    commands = [
        BotCommand(command="start", description="Запустить бота / Главное меню"),
        BotCommand(command="weather", description="Текущая погода и радар осадков"),
        BotCommand(command="city", description="Выбрать / изменить город"),
        BotCommand(command="alerts", description="Настройки предупреждений о дожде"),
        BotCommand(command="help", description="Справка и возможности"),
    ]
    await bot.set_my_commands(commands)

    # Launch background weather monitoring loop
    monitor_task = asyncio.create_task(start_weather_monitor(bot))

    # Start lightweight HTTP server for Render Free Web Service healthchecks
    http_runner = None
    try:
        import os
        from aiohttp import web
        port = int(os.getenv("PORT", "8080"))
        
        async def health_check(request):
            return web.Response(text="Weather Rain Alert Bot is running 24/7! 🌧", status=200)
            
        app = web.Application()
        app.router.add_get("/", health_check)
        app.router.add_get("/health", health_check)
        
        http_runner = web.AppRunner(app)
        await http_runner.setup()
        site = web.TCPSite(http_runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"Health-check HTTP server listening on port {port}")
    except Exception as http_err:
        logger.warning(f"Could not start HTTP health server: {http_err}")

    logger.info("🤖 Weather Rain Alert Telegram Bot is starting...")
    try:
        # Drop pending updates to prevent processing old messages on restart
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down bot and canceling background tasks...")
        if http_runner:
            await http_runner.cleanup()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await WeatherService.close()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
