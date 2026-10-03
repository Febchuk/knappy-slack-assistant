"""Per-user Slack profile facts the prompt needs, cached."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("knappy")

DEFAULT_TIMEZONE = "UTC"
CACHE_TTL_S = 24 * 3600


class UserDirectory:
    def __init__(self, client: Any | None, ttl_s: float = CACHE_TTL_S) -> None:
        self.client = client
        self.ttl_s = ttl_s
        self._timezones: dict[str, tuple[str, float]] = {}

    async def timezone(self, user_id: str) -> str:
        cached = self._timezones.get(user_id)
        if cached is not None and time.monotonic() - cached[1] < self.ttl_s:
            return cached[0]
        zone = await self._fetch_timezone(user_id)
        self._timezones[user_id] = (zone, time.monotonic())
        return zone

    async def _fetch_timezone(self, user_id: str) -> str:
        if self.client is None or not user_id:
            return DEFAULT_TIMEZONE
        try:
            response = await self.client.users_info(user=user_id)
        except Exception as exc:
            logger.info("users.info failed user=%s error=%s", user_id, type(exc).__name__)
            return DEFAULT_TIMEZONE
        return (response.get("user") or {}).get("tz") or DEFAULT_TIMEZONE
