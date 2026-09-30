import aiosqlite
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from config import DB_PATH, DEFAULT_ADVANCE_MINUTES

async def init_db():
    """Initializes the database schema and enables WAL mode for high concurrency."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                city_name TEXT,
                country_code TEXT,
                latitude REAL,
                longitude REAL,
                is_active INTEGER DEFAULT 1,
                advance_minutes INTEGER DEFAULT 30,
                last_rain_alert_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_active_coords ON users(is_active, latitude, longitude);")
        await db.commit()

async def get_or_create_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> Dict[str, Any]:
    """Retrieves user profile or creates a new entry if not exists (safe under concurrent calls)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        now = datetime.now(timezone.utc).isoformat()
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, is_active, advance_minutes, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
        """, (user_id, username, first_name, DEFAULT_ADVANCE_MINUTES, now, now))
        await db.commit()

        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row)

async def update_user_city(
    user_id: int,
    city_name: str,
    country_code: Optional[str],
    latitude: float,
    longitude: float
) -> None:
    """
    Updates user's selected city and coordinates.
    Does not touch is_active (a user who disabled alerts keeps them disabled),
    but resets the alert cooldown because the location changed.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET city_name = ?, country_code = ?, latitude = ?, longitude = ?,
                last_rain_alert_at = NULL, updated_at = ?
            WHERE user_id = ?
        """, (city_name, country_code, latitude, longitude, now, user_id))
        await db.commit()

async def toggle_user_active(user_id: int, is_active: bool) -> None:
    """Enables or disables rain alerts for a specific user."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET is_active = ?, updated_at = ?
            WHERE user_id = ?
        """, (1 if is_active else 0, now, user_id))
        await db.commit()

async def update_user_advance_minutes(user_id: int, advance_minutes: int) -> None:
    """Updates how many minutes in advance the user wants to be notified (e.g. 15, 30, 45, 60 min)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET advance_minutes = ?, updated_at = ?
            WHERE user_id = ?
        """, (advance_minutes, now, user_id))
        await db.commit()

async def update_last_alert_time(user_id: int, alert_time: Optional[datetime] = None) -> None:
    """Updates the timestamp when the last rain alert was sent to the user."""
    dt = alert_time or datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users
            SET last_rain_alert_at = ?
            WHERE user_id = ?
        """, (dt.isoformat(), user_id))
        await db.commit()

async def get_distinct_subscribed_locations() -> List[Dict[str, Any]]:
    """
    Returns unique locations (rounded to 2 decimals) where active users are subscribed.
    This enables batching weather requests so we don't duplicate calls for thousands of users in the same city.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                ROUND(latitude, 2) AS lat_group,
                ROUND(longitude, 2) AS lon_group,
                AVG(latitude) AS avg_lat,
                AVG(longitude) AS avg_lon,
                MIN(city_name) AS city_name,
                COUNT(*) as user_count
            FROM users
            WHERE is_active = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL
            GROUP BY ROUND(latitude, 2), ROUND(longitude, 2)
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_active_users_for_location(lat_group: float, lon_group: float) -> List[Dict[str, Any]]:
    """Returns all active users located within the given coordinate group."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM users
            WHERE is_active = 1
              AND ROUND(latitude, 2) = ?
              AND ROUND(longitude, 2) = ?
        """, (lat_group, lon_group)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_all_active_users() -> List[Dict[str, Any]]:
    """Returns all active users with configured coordinates."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM users
            WHERE is_active = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
