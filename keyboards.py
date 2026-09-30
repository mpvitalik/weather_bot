from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from typing import List, Dict, Any

from config import ALLOWED_ADVANCE_MINUTES

BTN_WEATHER = "🌤 Погода сейчас"
BTN_CITY = "📍 Мой город"
BTN_ALERTS = "🔔 Статус уведомлений"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "❌ Отмена"
MENU_BUTTONS = {BTN_WEATHER, BTN_CITY, BTN_ALERTS, BTN_HELP}

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Returns persistent main menu keyboard."""
    keyboard = [
        [
            KeyboardButton(text=BTN_WEATHER),
            KeyboardButton(text=BTN_CITY)
        ],
        [
            KeyboardButton(text=BTN_ALERTS),
            KeyboardButton(text=BTN_HELP)
        ]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_city_input_keyboard() -> ReplyKeyboardMarkup:
    """Returns keyboard with option to send live GPS location or cancel."""
    keyboard = [
        [KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
        [KeyboardButton(text=BTN_CANCEL)]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=True)

def get_city_selection_inline_keyboard(cities: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """Returns inline buttons for selecting between multiple city search results."""
    buttons = []
    for idx, city in enumerate(cities):
        display_name = city["display_name"]
        # Limit text length on inline buttons
        if len(display_name) > 35:
            display_name = display_name[:32] + "..."
        buttons.append([
            InlineKeyboardButton(
                text=f"📍 {display_name}",
                callback_data=f"select_city:{idx}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_city_selection")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_alerts_settings_keyboard(is_active: bool, advance_minutes: int) -> InlineKeyboardMarkup:
    """Returns inline settings for alerts and lead time."""
    toggle_text = "🔕 Выключить уведомления" if is_active else "🔔 Включить уведомления"
    toggle_data = "toggle_alerts:0" if is_active else "toggle_alerts:1"
    
    buttons = [
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if advance_minutes == m else ''}{m} мин",
                callback_data=f"set_lead:{m}"
            )
            for m in ALLOWED_ADVANCE_MINUTES
        ],
        [InlineKeyboardButton(text="📍 Сменить город", callback_data="change_city_prompt")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
