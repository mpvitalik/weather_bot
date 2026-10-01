import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

import aiohttp

from config import OPENWEATHER_API_KEY

logger = logging.getLogger(__name__)

HPA_TO_MMHG = 0.750062
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
RAIN_THRESHOLD_MM = 0.1
FORECAST_HORIZON_MINUTES = 60
CACHE_TTL_SECONDS = 120
CACHE_MAX_ENTRIES = 2000
MET_BASE = "https://api.met.no/weatherapi"
# MET Norway requires an identifying User-Agent
MET_HEADERS = {"User-Agent": "RainAlertTelegramBot/1.0 github.com/mpvitalik/weather_bot"}

OM_CURRENT_FIELDS = (
    "temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,"
    "precipitation,weather_code,apparent_temperature"
)

WMO_WEATHER_CODES = {
    0: "Ясно ☀️",
    1: "В основном ясно 🌤",
    2: "Переменная облачность ⛅️",
    3: "Пасмурно ☁️",
    45: "Туман 🌫",
    48: "Ледяной туман 🌫",
    51: "Слабая морось 🌦",
    53: "Умеренная морось 🌦",
    55: "Плотная морось 🌧",
    56: "Слабая ледяная морось 🌨",
    57: "Сильная ледяная морось 🌨",
    61: "Небольшой дождь 🌦",
    63: "Умеренный дождь 🌧",
    65: "Сильный дождь 🌧🌧",
    66: "Слабый ледяной дождь 🌨",
    67: "Сильный ледяной дождь 🌨",
    71: "Небольшой снегопад 🌨",
    73: "Умеренный снегопад ❄️",
    75: "Сильный снегопад ❄️❄️",
    77: "Снежные зерна ❄️",
    80: "Слабый ливень 🌦",
    81: "Умеренный ливень 🌧",
    82: "Шквалистый ливень ⛈",
    85: "Слабый снежный ливень 🌨",
    86: "Сильный снежный ливень ❄️",
    95: "Гроза ⚡️",
    96: "Гроза с небольшим градом ⛈",
    99: "Гроза с сильным градом ⛈⚡️"
}


def get_rain_intensity_description(precipitation_mm: float) -> str:
    """Returns human-readable rain intensity."""
    if precipitation_mm < 0.2:
        return "Очень слабый дождь / морось 🌦"
    elif precipitation_mm < 2.5:
        return "Небольшой дождь 🌦"
    elif precipitation_mm < 7.6:
        return "Умеренный дождь 🌧"
    elif precipitation_mm < 15.0:
        return "Сильный дождь 🌧🌧"
    else:
        return "Ливень / шквал ⛈"


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_rain(precip: Any, code: Any) -> bool:
    return (_is_num(precip) and precip >= RAIN_THRESHOLD_MM) or code in RAIN_CODES


def _parse_local_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _format_place(item: Dict[str, Any], name_ru: str) -> Tuple[str, str]:
    """Builds (display_name, country) for a geocoding item."""
    state = item.get("state")
    country = item.get("country", "")
    parts = [name_ru]
    if state and state != name_ru:
        parts.append(state)
    if country:
        parts.append(country)
    return ", ".join(parts), country


def _parse_open_meteo_current(curr: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Extracts a source record from an Open-Meteo `current` block; None if any key value is missing."""
    try:
        temp = curr["temperature_2m"]
        humidity = curr["relative_humidity_2m"]
        pressure = curr["surface_pressure"]
        wind = curr["wind_speed_10m"]
    except (KeyError, TypeError):
        return None
    if not all(_is_num(v) for v in (temp, humidity, pressure, wind)):
        return None
    feels = curr.get("apparent_temperature")
    return {
        "temp": temp,
        "feels_like": feels if _is_num(feels) else temp,
        "humidity": humidity,
        "pressure_mmhg": round(pressure * HPA_TO_MMHG),
        "wind_speed": round(wind / 3.6, 1),  # km/h -> m/s
    }


def _analyze_minutely(data: Dict[str, Any]) -> Tuple[bool, Optional[int], float, Optional[float]]:
    """
    Analyzes an Open-Meteo response with `minutely_15` data.
    Returns (is_raining_now, onset_minutes, onset_amount_mm, temperature).
    onset_minutes is one of 15/30/45/60 or None.
    """
    curr = data.get("current") or {}
    temp = curr.get("temperature_2m")
    raining_now = _is_rain(curr.get("precipitation"), curr.get("weather_code"))

    minutely = data.get("minutely_15") or {}
    times = minutely.get("time") or []
    precip = minutely.get("precipitation") or []
    codes = minutely.get("weather_code") or []

    curr_time = curr.get("time")
    start_idx = times.index(curr_time) if curr_time in times else 0

    for step in range(1, FORECAST_HORIZON_MINUTES // 15 + 1):
        idx = start_idx + step
        if idx >= len(precip):
            break
        p_val = precip[idx] if _is_num(precip[idx]) else 0.0
        c_val = codes[idx] if idx < len(codes) else None
        if _is_rain(p_val, c_val):
            return raining_now, step * 15, p_val, temp if _is_num(temp) else None
    return raining_now, None, 0.0, temp if _is_num(temp) else None


def _analyze_hourly(data: Dict[str, Any]) -> Tuple[bool, Optional[int], float, Optional[float]]:
    """
    Same as _analyze_minutely but for hourly-only models (GFS): rain onset within the next hour
    is estimated as the time to the next hourly slot (at least 15 minutes).
    """
    curr = data.get("current") or {}
    temp = curr.get("temperature_2m")
    temp = temp if _is_num(temp) else None
    raining_now = _is_rain(curr.get("precipitation"), curr.get("weather_code"))

    now_dt = _parse_local_time(curr.get("time"))
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    precip = hourly.get("precipitation") or []
    codes = hourly.get("weather_code") or []
    if now_dt is None:
        return raining_now, None, 0.0, temp

    for idx, t in enumerate(times):
        slot = _parse_local_time(t)
        if slot is None or idx >= len(precip):
            continue
        minutes = (slot - now_dt).total_seconds() / 60
        if 0 < minutes <= FORECAST_HORIZON_MINUTES:
            p_val = precip[idx] if _is_num(precip[idx]) else 0.0
            c_val = codes[idx] if idx < len(codes) else None
            if _is_rain(p_val, c_val):
                return raining_now, max(15, int(round(minutes))), p_val, temp
    return raining_now, None, 0.0, temp


def _parse_utc(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _met_entries(data: Any) -> List[Dict[str, Any]]:
    props = data.get("properties") if isinstance(data, dict) else None
    return (props or {}).get("timeseries") or []


def _met_symbol_ru(symbol: Optional[str]) -> Optional[str]:
    """Maps a MET Norway symbol_code (e.g. 'partlycloudy_day', 'lightrain') to a Russian description."""
    if not symbol:
        return None
    base = symbol.split("_")[0]
    if "thunder" in base:
        return "Гроза ⚡️"
    for key, text in (
        ("sleet", "Дождь со снегом 🌨"), ("snow", "Снег ❄️"),
        ("heavyrain", "Сильный дождь 🌧🌧"), ("lightrain", "Небольшой дождь 🌦"), ("rain", "Дождь 🌧"),
    ):
        if key in base:
            return text
    return {
        "clearsky": "Ясно ☀️", "fair": "В основном ясно 🌤", "partlycloudy": "Переменная облачность ⛅️",
        "cloudy": "Пасмурно ☁️", "fog": "Туман 🌫",
    }.get(base)


def _parse_met_current(data: Any) -> Optional[Tuple[Dict[str, float], Optional[str]]]:
    """Current conditions from MET Norway locationforecast. Sea-level pressure is skipped on purpose."""
    entries = _met_entries(data)
    if not entries:
        return None
    first = entries[0].get("data") or {}
    details = (first.get("instant") or {}).get("details") or {}
    temp, humidity, wind = (details.get(k) for k in ("air_temperature", "relative_humidity", "wind_speed"))
    if not all(_is_num(v) for v in (temp, humidity, wind)):
        return None
    symbol = ((first.get("next_1_hours") or {}).get("summary") or {}).get("symbol_code")
    return {"temp": temp, "humidity": humidity, "wind_speed": round(wind, 1)}, _met_symbol_ru(symbol)


def _analyze_met_nowcast(data: Any, now: datetime) -> Tuple[bool, Optional[int], float, Optional[float]]:
    """
    MET Norway nowcast (5-minute radar-based precipitation_rate in mm/h).
    Returns (is_raining_now, onset_minutes, rate_mm_h, temperature) like the other analyzers.
    """
    raining_now, onset, amount, temp = False, None, 0.0, None
    for entry in _met_entries(data):
        t = _parse_utc(entry.get("time"))
        details = ((entry.get("data") or {}).get("instant") or {}).get("details") or {}
        if temp is None and _is_num(details.get("air_temperature")):
            temp = details["air_temperature"]
        rate = details.get("precipitation_rate")
        if t is None or not _is_num(rate):
            continue
        minutes = (t - now).total_seconds() / 60
        if minutes > FORECAST_HORIZON_MINUTES:
            break
        if rate >= RAIN_THRESHOLD_MM:
            if minutes <= 5:
                raining_now = True
            elif onset is None:
                onset, amount = max(5, 5 * round(minutes / 5)), rate
    return raining_now, (None if raining_now else onset), amount, temp


def _analyze_met_hourly(data: Any, now: datetime) -> Tuple[bool, Optional[int], float, Optional[float]]:
    """
    MET Norway hourly forecast, used where there is no radar nowcast. If the current hour is already wet
    no onset is reported (we cannot tell when exactly it started), so no false "rain is coming" alerts.
    """
    slots = []
    for entry in _met_entries(data):
        t = _parse_utc(entry.get("time"))
        if t is None:
            continue
        d = entry.get("data") or {}
        amount = ((d.get("next_1_hours") or {}).get("details") or {}).get("precipitation_amount")
        temp = ((d.get("instant") or {}).get("details") or {}).get("air_temperature")
        slots.append((t, amount if _is_num(amount) else None, temp if _is_num(temp) else None))
    current = [s for s in slots if s[0] <= now]
    temp = current[-1][2] if current else None
    if current and (current[-1][1] or 0) >= RAIN_THRESHOLD_MM:
        return False, None, 0.0, temp
    for t, amount, _ in slots:
        minutes = (t - now).total_seconds() / 60
        if 0 < minutes <= FORECAST_HORIZON_MINUTES and (amount or 0) >= RAIN_THRESHOLD_MM:
            return False, max(15, int(round(minutes))), amount, temp
    return False, None, 0.0, temp


class WeatherService:
    _session: Optional[aiohttp.ClientSession] = None
    _cache: Dict[Tuple[str, float, float], Tuple[float, Any]] = {}

    # ---------- infrastructure ----------

    @classmethod
    def _get_session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession()
        return cls._session

    @classmethod
    async def close(cls) -> None:
        if cls._session is not None and not cls._session.closed:
            await cls._session.close()
        cls._session = None

    @classmethod
    async def _fetch_json(
        cls, url: str, params: Dict[str, Any], timeout: float = 6, headers: Optional[Dict[str, str]] = None
    ) -> Optional[Any]:
        try:
            async with cls._get_session().get(
                url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                logger.warning("GET %s returned HTTP %s", url, resp.status)
        except Exception as e:
            logger.warning("GET %s failed: %s", url, e)
        return None

    @classmethod
    def _cache_get(cls, kind: str, lat: float, lon: float) -> Optional[Any]:
        entry = cls._cache.get((kind, round(lat, 2), round(lon, 2)))
        if entry and time.monotonic() - entry[0] < CACHE_TTL_SECONDS:
            return entry[1]
        return None

    @classmethod
    def _cache_put(cls, kind: str, lat: float, lon: float, value: Any) -> None:
        if len(cls._cache) >= CACHE_MAX_ENTRIES:
            now = time.monotonic()
            cls._cache = {k: v for k, v in cls._cache.items() if now - v[0] < CACHE_TTL_SECONDS}
            if len(cls._cache) >= CACHE_MAX_ENTRIES:
                cls._cache.clear()
        cls._cache[(kind, round(lat, 2), round(lon, 2))] = (time.monotonic(), value)

    @classmethod
    async def _met_forecast(cls, lat: float, lon: float) -> Optional[Any]:
        """MET Norway hourly locationforecast (global), cached."""
        cached = cls._cache_get("met_hourly", lat, lon)
        if cached is not None:
            return cached
        data = await cls._fetch_json(
            f"{MET_BASE}/locationforecast/2.0/compact",
            {"lat": round(lat, 4), "lon": round(lon, 4)}, headers=MET_HEADERS,
        )
        if data:
            cls._cache_put("met_hourly", lat, lon, data)
        return data

    @classmethod
    async def _met_nowcast(cls, lat: float, lon: float) -> Optional[Any]:
        """MET Norway 5-minute radar nowcast (Nordic/Baltic area). Returns None when there is no radar coverage."""
        cached = cls._cache_get("met_now", lat, lon)
        if cached is not None:
            return cached or None
        data = await cls._fetch_json(
            f"{MET_BASE}/nowcast/2.0/complete",
            {"lat": round(lat, 4), "lon": round(lon, 4)}, headers=MET_HEADERS,
        )
        if data is None:  # HTTP 422 = outside radar coverage; remember it to avoid re-requesting
            cls._cache_put("met_now", lat, lon, {})
            return None
        coverage = ((data.get("properties") or {}).get("meta") or {}).get("radar_coverage")
        usable = data if coverage == "ok" else {}
        cls._cache_put("met_now", lat, lon, usable)
        return usable or None

    # ---------- geocoding ----------

    @staticmethod
    async def search_city(query: str) -> List[Dict[str, Any]]:
        """Searches for city matches using OpenWeatherMap Direct Geocoding. Raises nothing; returns [] on failure."""
        data = await WeatherService._fetch_json(
            "https://api.openweathermap.org/geo/1.0/direct",
            {"q": query.strip(), "limit": 5, "appid": OPENWEATHER_API_KEY},
            timeout=8,
        )
        results = []
        for item in data if isinstance(data, list) else []:
            if not _is_num(item.get("lat")) or not _is_num(item.get("lon")):
                continue
            name_ru = (item.get("local_names") or {}).get("ru") or item.get("name")
            if not name_ru:
                continue
            display_name, country = _format_place(item, name_ru)
            results.append({
                "city_name": name_ru,
                "display_name": display_name,
                "country": country,
                "state": item.get("state"),
                "latitude": item["lat"],
                "longitude": item["lon"],
            })
        return results

    @staticmethod
    async def reverse_geocode(lat: float, lon: float) -> Dict[str, Any]:
        """Finds city name by latitude and longitude; falls back to raw coordinates."""
        data = await WeatherService._fetch_json(
            "https://api.openweathermap.org/geo/1.0/reverse",
            {"lat": lat, "lon": lon, "limit": 1, "appid": OPENWEATHER_API_KEY},
            timeout=8,
        )
        if isinstance(data, list) and data:
            item = data[0]
            name_ru = (item.get("local_names") or {}).get("ru") or item.get("name") or "Неизвестное место"
            display_name, country = _format_place(item, name_ru)
            return {
                "city_name": name_ru,
                "display_name": display_name,
                "country": country,
                "latitude": lat,
                "longitude": lon,
            }
        return {
            "city_name": f"{lat:.2f}, {lon:.2f}",
            "display_name": f"Координаты: {lat:.2f}, {lon:.2f}",
            "country": "",
            "latitude": lat,
            "longitude": lon,
        }

    # ---------- current weather ----------

    @staticmethod
    async def get_ensemble_current_weather(lat: float, lon: float) -> Optional[Dict[str, Any]]:
        """
        Fetches current weather from several independent models and returns a consensus average.

        Sources: OpenWeatherMap, Open-Meteo (best match), NOAA GFS, DWD ICON.
        """
        cached = WeatherService._cache_get("current", lat, lon)
        if cached is not None:
            return cached

        om_params = {
            "latitude": lat, "longitude": lon,
            "current": OM_CURRENT_FIELDS, "timezone": "auto",
        }
        r_owm, r_om, r_gfs, r_dwd, r_met = await asyncio.gather(
            WeatherService._fetch_json(
                "https://api.openweathermap.org/data/2.5/weather",
                {"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"},
            ),
            WeatherService._fetch_json("https://api.open-meteo.com/v1/forecast", om_params),
            WeatherService._fetch_json("https://api.open-meteo.com/v1/gfs", om_params),
            WeatherService._fetch_json("https://api.open-meteo.com/v1/dwd-icon", om_params),
            WeatherService._met_forecast(lat, lon),
        )

        sources: Dict[str, Dict[str, float]] = {}
        descriptions: List[str] = []

        # OpenWeatherMap
        try:
            main = r_owm["main"]
            pressure = main.get("grnd_level", main["pressure"])  # station-level, comparable with Open-Meteo
            record = {
                "temp": main["temp"],
                "feels_like": main.get("feels_like", main["temp"]),
                "humidity": main["humidity"],
                "pressure_mmhg": round(pressure * HPA_TO_MMHG),
                "wind_speed": (r_owm.get("wind") or {}).get("speed", 0.0),
            }
            if all(_is_num(v) for v in record.values()):
                sources["OpenWeatherMap"] = record
                w_list = r_owm.get("weather") or []
                if w_list and w_list[0].get("description"):
                    descriptions.append(w_list[0]["description"].capitalize())
        except (KeyError, TypeError):
            pass

        # Open-Meteo family
        for name, resp in (
            ("Open-Meteo", r_om),
            ("NOAA GFS (США)", r_gfs),
            ("DWD ICON (Германия)", r_dwd),
        ):
            if not isinstance(resp, dict):
                continue
            curr = resp.get("current") or {}
            record = _parse_open_meteo_current(curr)
            if record is None:
                continue
            sources[name] = record
            if name == "Open-Meteo" and curr.get("weather_code") in WMO_WEATHER_CODES:
                descriptions.append(WMO_WEATHER_CODES[curr["weather_code"]])

        met = _parse_met_current(r_met)
        if met is not None:
            sources["MET Norway"] = met[0]
            if not descriptions and met[1]:
                descriptions.append(met[1])

        if not sources:
            return None

        def avg(key: str, digits: Optional[int] = None) -> Optional[float]:
            values = [s.get(key, s["temp"] if key == "feels_like" else None) for s in sources.values()]
            values = [v for v in values if v is not None]
            if not values:
                return None
            return round(sum(values) / len(values), digits) if digits is not None else round(sum(values) / len(values))

        result = {
            "temp": avg("temp", 1),
            "feels_like": avg("feels_like", 1),
            "humidity": avg("humidity"),
            "pressure_mmhg": avg("pressure_mmhg"),
            "wind_speed": avg("wind_speed", 1),
            "description": descriptions[0] if descriptions else "Переменная облачность",
            "sources_count": len(sources),
            "sources_list": list(sources.keys()),
            "sources_data": sources,
        }
        WeatherService._cache_put("current", lat, lon, result)
        return result

    # ---------- rain forecast ----------

    @staticmethod
    async def check_ensemble_rain_forecast(lat: float, lon: float) -> Dict[str, Any]:
        """
        Multi-model rain forecast for the next hour (Open-Meteo 15-min, DWD ICON 15-min, NOAA GFS hourly).

        Note: outside Central Europe / North America the 15-minute data is interpolated
        from hourly model output, so the onset time is approximate.

        Returned `data_available` is False when no model responded; callers must not
        interpret the (empty) result as "no rain".
        """
        cached = WeatherService._cache_get("rain", lat, lon)
        if cached is not None:
            return cached

        common = {
            "latitude": lat, "longitude": lon,
            "current": "precipitation,weather_code,temperature_2m",
            "forecast_days": 2,  # so the window that crosses midnight is covered
            "timezone": "auto",
        }
        minutely_params = {**common, "minutely_15": "precipitation,weather_code"}
        hourly_params = {**common, "hourly": "precipitation,weather_code"}

        r_om, r_dwd, r_gfs, r_met_now, r_met_hourly = await asyncio.gather(
            WeatherService._fetch_json("https://api.open-meteo.com/v1/forecast", minutely_params),
            WeatherService._fetch_json("https://api.open-meteo.com/v1/dwd-icon", minutely_params),
            WeatherService._fetch_json("https://api.open-meteo.com/v1/gfs", hourly_params),
            WeatherService._met_nowcast(lat, lon),
            WeatherService._met_forecast(lat, lon),
        )

        models_checked = 0
        rain_now_votes = 0
        onsets: List[int] = []
        amounts: List[float] = []
        temperatures: List[float] = []

        now_utc = datetime.now(timezone.utc)
        models = [
            (r_om, _analyze_minutely, "minutely_15"),
            (r_dwd, _analyze_minutely, "minutely_15"),
            (r_gfs, _analyze_hourly, "hourly"),
        ]
        # MET Norway counts as one model: radar nowcast where covered, otherwise hourly forecast
        if r_met_now:
            models.append((r_met_now, lambda d: _analyze_met_nowcast(d, now_utc), "properties"))
        elif r_met_hourly:
            models.append((r_met_hourly, lambda d: _analyze_met_hourly(d, now_utc), "properties"))

        for resp, analyzer, key in models:
            if not isinstance(resp, dict) or key not in resp:
                continue
            models_checked += 1
            raining_now, onset, amount, temp = analyzer(resp)
            if raining_now:
                rain_now_votes += 1
            if onset is not None:
                onsets.append(onset)
                amounts.append(amount)
            if temp is not None:
                temperatures.append(temp)

        result: Dict[str, Any] = {
            "data_available": models_checked > 0,
            "is_raining_now": rain_now_votes >= 1,
            "minutes_until_rain": None,
            "rain_amount_mm": 0.0,
            "intensity_desc": "Без осадков",
            "temperature": round(sum(temperatures) / len(temperatures), 1) if temperatures else 0.0,
            "models_checked": models_checked,
            "models_predicting_rain": len(onsets),
            "consensus_probability": round(len(onsets) / models_checked * 100) if models_checked else 0,
        }

        if onsets:
            avg_amount = round(sum(amounts) / len(amounts), 2)
            result["minutes_until_rain"] = round(sum(onsets) / len(onsets))
            result["rain_amount_mm"] = avg_amount
            result["intensity_desc"] = get_rain_intensity_description(avg_amount)

        if models_checked > 0:
            WeatherService._cache_put("rain", lat, lon, result)
        return result

    # Aliases for backwards compatibility
    get_current_weather = get_ensemble_current_weather
    check_rain_forecast_high_precision = check_ensemble_rain_forecast
