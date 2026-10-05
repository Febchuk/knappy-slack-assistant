"""Spec 22: one KnappyRuntime per installed workspace, built on first use and rebuilt when the workspace reinstalls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from knappy.runtime import KnappyRuntime
from knappy.slack.installations import Installation

Find = Callable[[str], Awaitable[Installation | None]]
Build = Callable[[Installation], Awaitable[KnappyRuntime]]


class Fleet:
    def __init__(self, find: Find, build: Build) -> None:
        self._find = find
        self._build = build
        self._runtimes: dict[str, KnappyRuntime] = {}
        self._lock = asyncio.Lock()

    async def get(self, team_id: str | None) -> KnappyRuntime | None:
        if not team_id:
            return None
        if runtime := self._runtimes.get(team_id):
            return runtime
        async with self._lock:
            if runtime := self._runtimes.get(team_id):
                return runtime
            installation = await self._find(team_id)
            if installation is None:
                return None
            self._runtimes[team_id] = await self._build(installation)
            return self._runtimes[team_id]

    async def install(self, installation: Installation) -> KnappyRuntime:
        """Replace the workspace's runtime, so new tokens and a new installer take effect without a restart."""
        async with self._lock:
            self._runtimes[installation.team_id] = await self._build(installation)
            return self._runtimes[installation.team_id]

    def drop(self, team_id: str) -> None:
        self._runtimes.pop(team_id, None)

    def all(self) -> list[KnappyRuntime]:
        return list(self._runtimes.values())
