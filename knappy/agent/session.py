"""Conversation keys, the turn log seam, and per-conversation ordering (Spec 12 §5)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from knappy.llm.types import Message, ModelTurn, UserMessage

WINDOW_TURNS = 20


def conversation_key(event: dict[str, Any]) -> str:
    channel = str(event.get("channel") or "")
    thread_ts = event.get("thread_ts")
    if is_dm(event) and not thread_ts:
        return f"dm:{channel}"
    return f"thread:{channel}:{thread_ts or event.get('ts') or ''}"


def is_dm(event: dict[str, Any]) -> bool:
    return event.get("channel_type") == "im" or str(event.get("channel") or "").startswith("D")


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str


def as_messages(turns: list[Turn]) -> list[Message]:
    return [UserMessage(turn.text) if turn.role == "user" else ModelTurn(text=turn.text) for turn in turns]


class ConversationLog(Protocol):
    """Persisted by knappy.memory.store.MemoryStore (Spec 13 §3.1)."""

    async def append(self, owner: str, key: str, turn: Turn, slack_ts: str | None = None) -> str: ...

    async def window(self, owner: str, key: str, limit: int = WINDOW_TURNS) -> list[Turn]: ...


class ConversationLocks:
    """One asyncio.Lock per conversation key, dropped once nobody holds or waits on it."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._holders: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._holders[key] = self._holders.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._holders[key] -= 1
            if not self._holders[key]:
                del self._holders[key]
                del self._locks[key]
