import asyncio
import html
import time
from typing import Any, Dict, List, Optional

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup

from config import ALLOWED_ADVANCE_MINUTES
from database import (
    get_or_create_user,
    update_user_city,
    toggle_user_active,
    update_user_advance_minutes
)
from weather_service import WeatherService
from keyboards import (
    BTN_WEATHER,
    BTN_CITY,
    BTN_ALERTS,
    BTN_HELP,
    BTN_CANCEL,
    MENU_BUTTONS,
    get_main_keyboard,
    get_city_input_keyboard,
    get_city_selection_inline_keyboard,
    get_alerts_settings_keyboard
)

router = Router()

class CityStates(StatesGroup):
    waiting_for_city_input = State()

# Temporary in-memory cache for multi-match city selections: user_id -> (created_at, cities)
CITY_CACHE_TTL_SECONDS = 15 * 60
CITY_CACHE_MAX_ENTRIES = 1000
city_search_cache: Dict[int, tuple] = {}


def _cache_cities(user_id: int, cities: List[Dict[str, Any]]) -> None:
    now = time.monotonic()
    if len(city_search_cache) >= CITY_CACHE_MAX_ENTRIES:
        for uid in [u for u, (ts, _) in city_search_cache.items() if now - ts > CITY_CACHE_TTL_SECONDS]:
            city_search_cache.pop(uid, None)
        if len(city_search_cache) >= CITY_CACHE_MAX_ENTRIES:
            city_search_cache.clear()
    city_search_cache[user_id] = (now, cities)


def _get_cached_cities(user_id: int) -> List[Dict[str, Any]]:
    entry = city_search_cache.get(user_id)
    if not entry or time.monotonic() - entry[0] > CITY_CACHE_TTL_SECONDS:
        city_search_cache.pop(user_id, None)
        return []
    return entry[1]


async def _get_user(message_or_callback) -> Dict[str, Any]:
    u = message_or_callback.from_user
    return await get_or_create_user(user_id=u.id, username=u.username, first_name=u.first_name)


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Edits the callback's message, ignoring 'message is not modified' and inaccessible messages."""
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _alerts_text(user: Dict[str, Any], footer: str = "") -> str:
    is_active = bool(user.get("is_active"))
    status = "✅ Включены" if is_active else "🔕 Выключены"
    text = (
        f"🔔 <b>Настройки предупреждений о дожде</b>\n\n"
        f"📍 <b>Город:</b> {html.escape(user.get('city_name') or 'Не указан')}\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"⏱ <b>Время предупреждения:</b> за {user.get('advance_minutes') or 30} минут до дождя\n\n"
    )
    return text + (footer or "Вы можете настроить параметры кнопками ниже:")


async def _apply_city(user_id: int, city: Dict[str, Any]) -> None:
    await update_user_city(
        user_id=user_id,
        city_name=city["city_name"],
        country_code=city.get("country"),
        latitude=city["latitude"],
        longitude=city["longitude"]
    )


def _city_set_text(display_name: str, user: Dict[str, Any]) -> str:
    text = f"✅ <b>Город установлен:</b> {html.escape(display_name)}\n\n"
    if user.get("is_active"):
        text += f"🔔 Я пришлю предупреждение за ~{user.get('advance_minutes') or 30} мин до начала дождя!"
    else:
        text += "🔕 Уведомления сейчас выключены — включить их можно в разделе «Статус уведомлений»."
    return text


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Handles /start command."""
    await state.clear()
    user = await _get_user(message)

    welcome_text = (
        f"👋 <b>Здравствуйте, {html.escape(message.from_user.first_name or 'друг')}!</b>\n\n"
        f"Я бот раннего оповещения о дожде 🌧\n"
        f"Я отслеживаю прогноз и заранее предупреждаю о дожде в вашем городе "
        f"(время предупреждения настраивается: 15–60 минут, по умолчанию 30), "
        f"чтобы вы успели взять зонт!\n\n"
    )

    if user.get("city_name"):
        welcome_text += (
            f"📍 Ваш текущий город: <b>{html.escape(user['city_name'])}</b>\n"
            f"🔔 Уведомления: <b>{'Включены ✅' if user.get('is_active') else 'Выключены 🔕'}</b>\n\n"
            f"Используйте меню ниже для управления."
        )
        await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    else:
        welcome_text += (
            "📍 <b>Давайте настроим ваш город.</b>\n"
            "Напишите название вашего города (например, <i>Рига</i>, <i>Киев</i>, <i>Осло</i>) "
            "или отправьте текущую геопозицию с помощью кнопки ниже."
        )
        await state.set_state(CityStates.waiting_for_city_input)
        await message.answer(welcome_text, reply_markup=get_city_input_keyboard(), parse_mode="HTML")

@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext):
    """Handles help and FAQ."""
    await state.clear()
    help_text = (
        "🤖 <b>Возможности погодного бота:</b>\n\n"
        "1. <b>🌧 Предупреждение о дожде:</b>\n"
        "   Бот регулярно анализирует прогноз осадков и присылает сообщение заранее (по умолчанию за ~30 минут) до начала дождя в вашем городе. "
        "Вне Центральной Европы и Северной Америки минутный прогноз получается интерполяцией из почасовых моделей, поэтому время начала приблизительное.\n\n"
        "2. <b>🌤 Текущая погода:</b>\n"
        "   Нажмите «Погода сейчас», чтобы получить актуальную сводку (температура, влажность, давление, ветер).\n\n"
        "3. <b>📍 Смена локации:</b>\n"
        "   Вы можете в любой момент изменить свой город через меню или отправив точку на карте.\n\n"
        "4. <b>🔔 Настройки:</b>\n"
        f"   Включайте/отключайте уведомления и настраивайте время предупреждения ({', '.join(map(str, ALLOWED_ADVANCE_MINUTES))} мин).\n\n"
        "<b>Команды бота:</b>\n"
        "/start — Перезапуск и главное меню\n"
        "/weather — Погода прямо сейчас\n"
        "/city — Изменить город\n"
        "/alerts — Настройки уведомлений\n"
        "/help — Справка"
    )
    await message.answer(help_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

@router.message(Command("city"))
@router.message(F.text == BTN_CITY)
async def prompt_change_city(message: Message, state: FSMContext):
    """Prompts user to change or enter city name/location."""
    user = await _get_user(message)
    current_city = user.get("city_name")

    text = "📍 "
    if current_city:
        text += f"Ваш текущий город: <b>{html.escape(current_city)}</b>.\n\n"
    text += "Напишите название нового города или нажмите <b>«📍 Отправить геопозицию»</b>:"

    await state.set_state(CityStates.waiting_for_city_input)
    await message.answer(text, reply_markup=get_city_input_keyboard(), parse_mode="HTML")

@router.message(Command("weather"))
@router.message(F.text == BTN_WEATHER)
async def cmd_current_weather(message: Message, state: FSMContext):
    """Fetches and displays real-time weather and precipitation status."""
    await state.clear()
    user = await _get_user(message)

    lat = user.get("latitude")
    lon = user.get("longitude")
    city_name = user.get("city_name")

    if lat is None or lon is None:
        await message.answer(
            "⚠️ Вы еще не указали свой город.\nНажмите <b>«📍 Мой город»</b> в меню для настройки.",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        return

    loading = await message.answer("⏳ Загружаю данные о погоде...")

    weather, rain_check = await asyncio.gather(
        WeatherService.get_current_weather(lat, lon),
        WeatherService.check_rain_forecast_high_precision(lat, lon),
    )

    try:
        await loading.delete()
    except TelegramBadRequest:
        pass

    if not weather:
        await message.answer(
            "❌ Не удалось получить данные о погоде. Попробуйте чуть позже.",
            reply_markup=get_main_keyboard()
        )
        return

    # Rain status summary
    if not rain_check.get("data_available"):
        rain_status = "❔ Данные прогноза осадков сейчас недоступны."
    elif rain_check.get("is_raining_now"):
        rain_status = "🌧 <b>Сейчас идет дождь / осадки!</b>"
    elif rain_check.get("minutes_until_rain"):
        rain_status = (
            f"⏱ Дождь ожидается через <b>~{rain_check['minutes_until_rain']} мин</b> "
            f"({html.escape(rain_check['intensity_desc'])})"
        )
    else:
        rain_status = "☀️ В ближайший час дождя не ожидается."

    sources_str = ", ".join(weather.get("sources_list", []))
    text = (
        f"🌤 <b>Погода в г. {html.escape(city_name or '—')}:</b>\n\n"
        f"🌡 <b>Температура:</b> {weather['temp']}°C (ощущается как {weather['feels_like']}°C)\n"
        f"📖 <b>Состояние:</b> {html.escape(weather['description'])}\n"
        f"💧 <b>Влажность:</b> {weather['humidity']}%\n"
        f"💨 <b>Ветер:</b> {weather['wind_speed']} м/с\n"
        f"🧭 <b>Давление:</b> {weather['pressure_mmhg']} мм рт. ст.\n\n"
        f"🌧 <b>Прогноз осадков:</b>\n{rain_status}\n\n"
        f"🔬 <i>Ансамблевый расчет по {weather.get('sources_count', 1)} источникам ({html.escape(sources_str)})</i>"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")

@router.message(Command("alerts"))
@router.message(F.text == BTN_ALERTS)
async def cmd_alerts_status(message: Message, state: FSMContext):
    """Displays notification status and settings."""
    await state.clear()
    user = await _get_user(message)
    await message.answer(
        _alerts_text(user),
        reply_markup=get_alerts_settings_keyboard(bool(user.get("is_active")), user.get("advance_minutes") or 30),
        parse_mode="HTML"
    )

@router.message(F.text == BTN_CANCEL)
async def process_cancel_city_input(message: Message, state: FSMContext):
    """Cancels city selection."""
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=get_main_keyboard())

@router.message(F.location)
async def process_location_input(message: Message, state: FSMContext):
    """Processes location sent by user via GPS (works both inside and outside of the city-input state)."""
    await state.clear()
    lat = message.location.latitude
    lon = message.location.longitude

    loading_msg = await message.answer("🔍 Определяем город по координатам...")
    geo_data = await WeatherService.reverse_geocode(lat, lon)
    await _apply_city(message.from_user.id, geo_data)
    user = await _get_user(message)

    try:
        await loading_msg.delete()
    except TelegramBadRequest:
        pass

    await message.answer(
        _city_set_text(geo_data["display_name"], user),
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

def _is_city_query(message: Message) -> bool:
    text = message.text or ""
    return not text.startswith("/") and text not in MENU_BUTTONS and text != BTN_CANCEL

@router.message(CityStates.waiting_for_city_input, F.text, F.func(_is_city_query))
async def process_text_city_input(message: Message, state: FSMContext):
    """Processes city name typed by user."""
    query = message.text.strip()
    if len(query) < 2 or len(query) > 100:
        await message.answer("Название города должно содержать от 2 до 100 символов. Попробуйте еще раз:")
        return

    loading_msg = await message.answer("🔍 Поиск города...")
    cities = await WeatherService.search_city(query)
    try:
        await loading_msg.delete()
    except TelegramBadRequest:
        pass

    if not cities:
        await message.answer(
            f"❌ Город «<b>{html.escape(query)}</b>» не найден (или сервис поиска временно недоступен).\n"
            f"Проверьте правильность написания или попробуйте указать на английском языке / отправить геопозицию.",
            parse_mode="HTML"
        )
        return

    await state.clear()
    if len(cities) == 1:
        city = cities[0]
        await _apply_city(message.from_user.id, city)
        user = await _get_user(message)
        await message.answer(
            _city_set_text(city["display_name"], user),
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
    else:
        _cache_cities(message.from_user.id, cities)
        await message.answer(
            "Найдено несколько совпадений. Выберите нужный город из списка:",
            reply_markup=get_city_selection_inline_keyboard(cities)
        )

@router.callback_query(F.data.startswith("select_city:"))
async def callback_select_city(callback: CallbackQuery):
    """Handles city picked from inline keyboard."""
    user_id = callback.from_user.id
    try:
        idx = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return

    cities = _get_cached_cities(user_id)
    if not (0 <= idx < len(cities)):
        await callback.answer("Срок действия выбора истек. Попробуйте еще раз.", show_alert=True)
        return

    city = cities[idx]
    await _apply_city(user_id, city)
    city_search_cache.pop(user_id, None)
    user = await _get_user(callback)
    await callback.answer()

    await _safe_edit(callback, _city_set_text(city["display_name"], user))
    if isinstance(callback.message, Message):
        await callback.message.answer("Главное меню:", reply_markup=get_main_keyboard())

@router.callback_query(F.data == "cancel_city_selection")
async def callback_cancel_city_selection(callback: CallbackQuery):
    """Handles inline cancel."""
    city_search_cache.pop(callback.from_user.id, None)
    if isinstance(callback.message, Message):
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await callback.answer("Отменено")

@router.callback_query(F.data.startswith("toggle_alerts:"))
async def callback_toggle_alerts(callback: CallbackQuery):
    """Toggles alerts on/off."""
    value = callback.data.split(":", 1)[1]
    if value not in ("0", "1"):
        await callback.answer()
        return
    active_val = value == "1"
    user_id = callback.from_user.id

    await get_or_create_user(user_id, callback.from_user.username, callback.from_user.first_name)
    await toggle_user_active(user_id, active_val)
    user = await _get_user(callback)

    await callback.answer(f"Уведомления {'включены' if active_val else 'выключены'}")
    await _safe_edit(
        callback,
        _alerts_text(user, "Параметры обновлены!"),
        get_alerts_settings_keyboard(active_val, user.get("advance_minutes") or 30)
    )

@router.callback_query(F.data.startswith("set_lead:"))
async def callback_set_lead_time(callback: CallbackQuery):
    """Sets lead time (one of ALLOWED_ADVANCE_MINUTES)."""
    try:
        minutes = int(callback.data.split(":", 1)[1])
    except ValueError:
        minutes = 0
    if minutes not in ALLOWED_ADVANCE_MINUTES:
        await callback.answer()
        return
    user_id = callback.from_user.id

    await get_or_create_user(user_id, callback.from_user.username, callback.from_user.first_name)
    await update_user_advance_minutes(user_id, minutes)
    user = await _get_user(callback)

    await callback.answer(f"Предупреждать за {minutes} мин")
    await _safe_edit(
        callback,
        _alerts_text(user, "Время предупреждения обновлено!"),
        get_alerts_settings_keyboard(bool(user.get("is_active")), minutes)
    )

@router.callback_query(F.data == "change_city_prompt")
async def callback_change_city_prompt(callback: CallbackQuery, state: FSMContext):
    """Prompts city change from inline settings."""
    await callback.answer()
    await state.set_state(CityStates.waiting_for_city_input)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "📍 Напишите название вашего города или отправьте геопозицию:",
            reply_markup=get_city_input_keyboard()
        )

@router.message(F.text)
async def handle_any_text(message: Message):
    """Fallback for plain text outside of the city-input flow: never changes settings, only guides."""
    await _get_user(message)
    await message.answer(
        "👋 Чтобы начать или посмотреть погоду, используйте кнопки меню ниже, команду /start "
        "или «📍 Мой город», чтобы изменить город.",
        reply_markup=get_main_keyboard()
    )
