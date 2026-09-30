import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

from config import CHECK_INTERVAL_SECONDS, ALERT_COOLDOWN_MINUTES, DEFAULT_ADVANCE_MINUTES
from database import (
    get_distinct_subscribed_locations,
    get_active_users_for_location,
    update_last_alert_time,
    toggle_user_active
)
from weather_service import WeatherService

logger = logging.getLogger(__name__)


def _in_cooldown(last_alert: Any, now: datetime) -> bool:
    if not last_alert:
        return False
    try:
        last_dt = datetime.fromisoformat(last_alert)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt) < timedelta(minutes=ALERT_COOLDOWN_MINUTES)
    except Exception as parse_err:
        logger.warning(f"Error parsing last_rain_alert_at: {parse_err}")
        return False


async def _send_alert(bot: Bot, user_id: int, text: str) -> None:
    """Sends a message, retrying once if Telegram asks to slow down."""
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except TelegramRetryAfter as ex:
        await asyncio.sleep(ex.retry_after + 1)
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")


async def _process_location(bot: Bot, loc: Dict[str, Any]) -> None:
    lat, lon = loc["avg_lat"], loc["avg_lon"]
    city_name = loc["city_name"] or f"({lat:.2f}, {lon:.2f})"

    forecast = await WeatherService.check_rain_forecast_high_precision(lat, lon)
    if not forecast.get("data_available"):
        logger.warning(f"No forecast data for {city_name}, skipping this cycle")
        return

    minutes_until_rain = forecast.get("minutes_until_rain")
    if minutes_until_rain is None or forecast.get("is_raining_now"):
        return

    users = await get_active_users_for_location(loc["lat_group"], loc["lon_group"])
    now = datetime.now(timezone.utc)

    for user in users:
        user_id = user["user_id"]
        # Each user is warned once rain is expected within THEIR chosen lead time
        advance = user.get("advance_minutes") or DEFAULT_ADVANCE_MINUTES
        if minutes_until_rain > advance or _in_cooldown(user.get("last_rain_alert_at"), now):
            continue

        alert_text = (
            f"🌧 <b>Внимание! Приближается дождь!</b>\n\n"
            f"📍 <b>Город:</b> {html.escape(city_name)}\n"
            f"⏱ <b>Начало:</b> примерно через <b>~{minutes_until_rain} мин</b>\n"
            f"💧 <b>Осадки:</b> {html.escape(forecast.get('intensity_desc', 'Дождь'))}\n"
            f"🌡 <b>Температура:</b> {forecast.get('temperature', 0)}°C\n"
            f"🎯 <b>Консенсус моделей:</b> {forecast.get('consensus_probability', 0)}%\n\n"
            f"☔️ <i>Не забудьте взять с собой зонт!</i>"
        )

        try:
            await _send_alert(bot, user_id, alert_text)
            await update_last_alert_time(user_id, now)
            logger.info(f"Rain alert successfully sent to user {user_id} in {city_name}")
        except TelegramForbiddenError:
            logger.warning(f"User {user_id} blocked bot. Deactivating user.")
            await toggle_user_active(user_id, False)
        except TelegramBadRequest as ex:
            logger.error(f"Bad request sending to {user_id}: {ex}")
        except Exception as ex:
            logger.error(f"Unexpected error sending alert to {user_id}: {ex}")

        # Stay well below Telegram's broadcast limits (~30 msg/sec)
        await asyncio.sleep(0.05)


async def start_weather_monitor(bot: Bot):
    """
    Background worker that continuously monitors weather for all subscribed cities
    and sends proactive alerts before expected rain (respecting each user's lead time).
    """
    logger.info("Starting weather rain monitoring background task...")

    while True:
        try:
            locations = await get_distinct_subscribed_locations()
            logger.info(f"Checking rain forecast for {len(locations)} active location clusters...")

            for loc in locations:
                try:
                    await _process_location(bot, loc)
                except Exception as loc_err:
                    logger.error(f"Error checking weather for location {loc.get('city_name')}: {loc_err}", exc_info=True)

                # Small pause between distinct city checks to be gentle with network/rate limits
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error in weather monitor loop: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
