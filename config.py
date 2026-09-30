import logging
import os
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    """Reads an integer env var; falls back to the default on garbage or too small values."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below minimum %s, using default %s", name, value, minimum, default)
        return default
    return value


# Telegram Bot Token (obtain from @BotFather in Telegram)
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# OpenWeatherMap API Key (no default: never commit keys to the source code)
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "").strip()

# Path to SQLite database (can be customized via DB_PATH env var, e.g. for mounted volumes)
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "weather_bot.db")))

# Weather monitoring interval in seconds (default: 5 minutes)
CHECK_INTERVAL_SECONDS = _int_env("CHECK_INTERVAL_SECONDS", 300, minimum=30)

# Allowed warning lead times (minutes) offered to users
ALLOWED_ADVANCE_MINUTES = (15, 30, 45, 60)

# Default warning lead time before rain in minutes
DEFAULT_ADVANCE_MINUTES = _int_env("DEFAULT_ADVANCE_MINUTES", 30)
if DEFAULT_ADVANCE_MINUTES not in ALLOWED_ADVANCE_MINUTES:
    DEFAULT_ADVANCE_MINUTES = 30

# Cooldown between rain alerts for the same user (in minutes, default: 3 hours)
ALERT_COOLDOWN_MINUTES = _int_env("ALERT_COOLDOWN_MINUTES", 180, minimum=0)
