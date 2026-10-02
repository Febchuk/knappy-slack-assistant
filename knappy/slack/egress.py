"""Post Knappy replies and reminder DMs through a Slack Web API client."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

Say = Callable[..., Awaitable[None]]
logger = logging.getLogger("knappy")


def build_say(client: Any) -> Say:
    async def say(
        *,
        text: str,
        channel: str | None = None,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        user: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        if not channel:
            return
        body = text or " "
        if ephemeral and user:
            kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": body}
            if blocks:
                kwargs["blocks"] = blocks
            try:
                await client.chat_postEphemeral(**kwargs)
            except Exception as exc:
                logger.info("deliver failed ephemeral channel=%s error=%s: %s", channel, type(exc).__name__, exc)
            else:
                logger.info("deliver ephemeral channel=%s", channel)
                return
        kwargs = {"channel": channel, "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await client.chat_postMessage(**kwargs)
        logger.info("deliver postMessage channel=%s", channel)

    return say


def delivery_kwargs(event: dict[str, Any], text: str, blocks: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Choose a DM post, a thread reply, or an ephemeral channel answer."""
    channel = event.get("channel")
    user = event.get("user")
    channel_id = str(channel or "")
    is_dm = event.get("channel_type") == "im" or channel_id.startswith("D")
    kwargs: dict[str, Any] = {
        "text": text,
        "channel": channel,
        "blocks": blocks,
        "user": user,
        "ephemeral": False,
        "thread_ts": None,
    }
    if not is_dm:
        kwargs["ephemeral"] = True
        kwargs["thread_ts"] = event.get("thread_ts") or event.get("ts")
        return kwargs
    if event.get("thread_ts"):
        kwargs["thread_ts"] = event.get("thread_ts")
    elif event.get("type") == "app_mention":
        kwargs["thread_ts"] = event.get("ts")
    return kwargs
