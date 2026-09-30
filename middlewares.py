import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject


class ThrottlingMiddleware(BaseMiddleware):
    """Drops updates from a user arriving faster than `interval` seconds (protects external API quotas)."""

    MAX_TRACKED_USERS = 10000

    def __init__(self, interval: float = 0.7):
        self.interval = interval
        self._last_seen: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            if now - self._last_seen.get(user.id, 0.0) < self.interval:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                return None
            if len(self._last_seen) >= self.MAX_TRACKED_USERS:
                self._last_seen = {
                    uid: ts for uid, ts in self._last_seen.items() if now - ts < self.interval
                }
            self._last_seen[user.id] = now
        return await handler(event, data)
