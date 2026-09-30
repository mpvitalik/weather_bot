import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from config import DB_PATH, DATABASE_URL, DEFAULT_ADVANCE_MINUTES

logger = logging.getLogger(__name__)

# Global asyncpg connection pool if PostgreSQL is used
_pg_pool = None

def is_postgres() -> bool:
    return bool(DATABASE_URL)

async def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        import asyncpg
        logger.info("Creating PostgreSQL connection pool (Neon.tech / Cloud Postgres)...")
        _pg_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=15
        )
    return _pg_pool

async def close_db():
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None

async def init_db():
    """Initializes the database schema for PostgreSQL (Neon) or local SQLite."""
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    city_name TEXT,
                    country_code TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    is_active INTEGER DEFAULT 1,
                    advance_minutes INTEGER DEFAULT 30,
                    last_rain_alert_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active_coords ON users(is_active, latitude, longitude);")
            logger.info("PostgreSQL database schema initialized successfully.")
    else:
        import aiosqlite
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
            logger.info("SQLite database schema initialized successfully.")

async def get_or_create_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> Dict[str, Any]:
    """Retrieves user profile or creates a new entry if not exists."""
    now = datetime.now(timezone.utc)
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, is_active, advance_minutes, created_at, updated_at)
                VALUES ($1, $2, $3, 1, $4, $5, $5)
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, updated_at = EXCLUDED.updated_at;
            """, user_id, username, first_name, DEFAULT_ADVANCE_MINUTES, now)
            
            row = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1;", user_id)
            return dict(row) if row else {}
    else:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            now_iso = now.isoformat()
            await db.execute("""
                INSERT OR IGNORE INTO users (user_id, username, first_name, is_active, advance_minutes, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, ?)
            """, (user_id, username, first_name, DEFAULT_ADVANCE_MINUTES, now_iso, now_iso))
            await db.commit()

            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else {}

async def update_user_city(
    user_id: int,
    city_name: str,
    country_code: Optional[str],
    latitude: float,
    longitude: float
) -> None:
    now = datetime.now(timezone.utc)
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET city_name = $1, country_code = $2, latitude = $3, longitude = $4,
                    last_rain_alert_at = NULL, updated_at = $5
                WHERE user_id = $6;
            """, city_name, country_code, latitude, longitude, now, user_id)
    else:
        import aiosqlite
        now_iso = now.isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE users
                SET city_name = ?, country_code = ?, latitude = ?, longitude = ?,
                    last_rain_alert_at = NULL, updated_at = ?
                WHERE user_id = ?
            """, (city_name, country_code, latitude, longitude, now_iso, user_id))
            await db.commit()

async def toggle_user_active(user_id: int, is_active: bool) -> None:
    now = datetime.now(timezone.utc)
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET is_active = $1, updated_at = $2
                WHERE user_id = $3;
            """, 1 if is_active else 0, now, user_id)
    else:
        import aiosqlite
        now_iso = now.isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE users
                SET is_active = ?, updated_at = ?
                WHERE user_id = ?
            """, (1 if is_active else 0, now_iso, user_id))
            await db.commit()

async def update_user_advance_minutes(user_id: int, advance_minutes: int) -> None:
    now = datetime.now(timezone.utc)
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET advance_minutes = $1, updated_at = $2
                WHERE user_id = $3;
            """, advance_minutes, now, user_id)
    else:
        import aiosqlite
        now_iso = now.isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE users
                SET advance_minutes = ?, updated_at = ?
                WHERE user_id = ?
            """, (advance_minutes, now_iso, user_id))
            await db.commit()

async def update_last_alert_time(user_id: int, alert_time: Optional[datetime] = None) -> None:
    dt = alert_time or datetime.now(timezone.utc)
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users
                SET last_rain_alert_at = $1
                WHERE user_id = $2;
            """, dt, user_id)
    else:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                UPDATE users
                SET last_rain_alert_at = ?
                WHERE user_id = ?
            """, (dt.isoformat(), user_id))
            await db.commit()

async def get_distinct_subscribed_locations() -> List[Dict[str, Any]]:
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    ROUND(CAST(latitude AS numeric), 2) AS lat_group,
                    ROUND(CAST(longitude AS numeric), 2) AS lon_group,
                    AVG(latitude) AS avg_lat,
                    AVG(longitude) AS avg_lon,
                    MIN(city_name) AS city_name,
                    COUNT(*) as user_count
                FROM users
                WHERE is_active = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL
                GROUP BY ROUND(CAST(latitude AS numeric), 2), ROUND(CAST(longitude AS numeric), 2);
            """)
            return [
                {
                    "lat_group": float(r["lat_group"]),
                    "lon_group": float(r["lon_group"]),
                    "avg_lat": float(r["avg_lat"]),
                    "avg_lon": float(r["avg_lon"]),
                    "city_name": r["city_name"],
                    "user_count": r["user_count"]
                }
                for r in rows
            ]
    else:
        import aiosqlite
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
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM users
                WHERE is_active = 1
                  AND ROUND(CAST(latitude AS numeric), 2) = $1
                  AND ROUND(CAST(longitude AS numeric), 2) = $2;
            """, lat_group, lon_group)
            return [dict(r) for r in rows]
    else:
        import aiosqlite
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
    if is_postgres():
        pool = await _get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM users
                WHERE is_active = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL;
            """)
            return [dict(r) for r in rows]
    else:
        import aiosqlite
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT * FROM users
                WHERE is_active = 1 AND latitude IS NOT NULL AND longitude IS NOT NULL
            """) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
