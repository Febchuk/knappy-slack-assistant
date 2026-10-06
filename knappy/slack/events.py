"""Slack event handlers. Acknowledgement always happens before downstream work."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

Processor = Callable[[dict[str, Any]], Awaitable[None]]
# Subtypes that are a person's new message. Edits, deletions, joins, and bot posts are not.
USER_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})
logger = logging.getLogger("knappy")


class Listener(Protocol):
    """Workspace awareness (Spec 18): takes the owner's conversations the agent loop never sees."""

    async def wants(self, event: dict[str, Any], authorizations: list[dict[str, Any]] | None = None) -> bool: ...

    async def accept(self, event: dict[str, Any]) -> bool: ...


class EventDeduplicator:
    def __init__(self, maxlen: int = 1000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.maxlen = maxlen

    def seen(self, event_id: str) -> bool:
        if event_id in self._seen:
            return True
        self._seen[event_id] = None
        if len(self._seen) > self.maxlen:
            self._seen.popitem(last=False)
        return False


def event_key(event: dict[str, Any]) -> str | None:
    for field in ("client_msg_id", "event_ts", "ts"):
        value = event.get(field)
        if value:
            return str(value)
    return None


async def on_message(
    event: dict[str, Any],
    ack: Callable[[], Awaitable[None]],
    *,
    processor: Processor | None = None,
    deduper: EventDeduplicator | None = None,
    awareness: Listener | None = None,
    authorizations: list[dict[str, Any]] | None = None,
) -> float:
    """Acknowledge, then route: the owner's own conversations to awareness, Knappy's DMs to the processor.

    Returns seconds spent before ack returns.
    """
    started = time.perf_counter()
    await ack()
    elapsed = time.perf_counter() - started
    if awareness is not None and not _from_bot(event) and await awareness.wants(event, authorizations):
        await awareness.accept(event)
        return elapsed
    if event.get("channel_type") not in (None, "im"):
        return elapsed
    if _from_bot(event) or event.get("subtype") not in USER_SUBTYPES:
        return elapsed
    key = event_key(event)
    if deduper is not None and key is not None and deduper.seen(key):
        return elapsed
    if processor is not None:
        _log_received("message", event)
        await processor(event)
    return elapsed


async def on_app_mention(
    event: dict[str, Any],
    ack: Callable[[], Awaitable[None]],
    *,
    processor: Processor | None = None,
    deduper: EventDeduplicator | None = None,
) -> float:
    started = time.perf_counter()
    await ack()
    elapsed = time.perf_counter() - started
    key = event_key(event)
    if _from_bot(event):
        return elapsed
    if deduper is not None and key is not None and deduper.seen(key):
        return elapsed
    if processor is not None:
        _log_received("app_mention", event)
        await processor(event)
    return elapsed


def _from_bot(event: dict[str, Any]) -> bool:
    return bool(event.get("bot_id") or event.get("subtype") == "bot_message")


def _log_received(event_type: str, event: dict[str, Any]) -> None:
    from knappy.runtime import strip_address

    logger.info("event type=%s channel=%s user=%s", event_type, event.get("channel"), event.get("user"))
    logger.debug("event text=%s", strip_address(str(event.get("text") or "")))
