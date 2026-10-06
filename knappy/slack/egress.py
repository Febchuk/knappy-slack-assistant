"""Post, update, and react through a Slack Web API client, and the per-message reply surfaces (Spec 12 §6)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from knappy.agent.session import is_dm

logger = logging.getLogger("knappy")

PLACEHOLDER = "_thinking…_"
WORKING_REACTION = "eyes"
# A section block holds at most 3,000 characters, and an answer's text may need to become one.
CHUNK_CHARS = 3_000


class SlackEgress:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def __call__(
        self,
        *,
        text: str,
        channel: str | None = None,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        user: str | None = None,
        ephemeral: bool = False,
    ) -> str | None:
        """Post a message. Returns its ts, or None for ephemeral posts."""
        if not channel:
            return None
        body = text or " "
        if ephemeral and user:
            kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": body}
            if blocks:
                kwargs["blocks"] = blocks
            try:
                await self.client.chat_postEphemeral(**kwargs)
            except Exception as exc:
                logger.info("deliver failed ephemeral channel=%s error=%s: %s", channel, type(exc).__name__, exc)
            else:
                logger.info("deliver ephemeral channel=%s", channel)
                return None
        kwargs = {"channel": channel, "text": body}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        response = await self.client.chat_postMessage(**kwargs)
        logger.info("deliver postMessage channel=%s", channel)
        return response.get("ts") if response is not None else None

    async def update(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]] | None = None
    ) -> None:
        # An empty blocks list clears any blocks a previous update set.
        await self.client.chat_update(channel=channel, ts=ts, text=text or " ", blocks=blocks or [])
        logger.info("deliver update channel=%s", channel)

    async def react(self, channel: str, ts: str, name: str, on: bool) -> None:
        method = self.client.reactions_add if on else self.client.reactions_remove
        try:
            await method(channel=channel, timestamp=ts, name=name)
        except Exception as exc:
            logger.info("react failed channel=%s name=%s error=%s: %s", channel, name, type(exc).__name__, exc)


def build_say(client: Any) -> SlackEgress:
    return SlackEgress(client)


class Reply(Protocol):
    """The lifecycle of one answer in Slack: visible acknowledgement, progress, then the answer."""

    async def start(self) -> None: ...

    async def status(self, text: str) -> None: ...

    async def finish(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None: ...


def open_reply(egress: SlackEgress | None, event: dict[str, Any]) -> Reply:
    channel = str(event.get("channel") or "")
    if egress is None or not channel:
        return SilentReply()
    if is_dm(event):
        thread_ts = event.get("thread_ts") or (event.get("ts") if event.get("type") == "app_mention" else None)
        return PlaceholderReply(egress, channel, thread_ts)
    return EphemeralReply(
        egress,
        channel,
        user=str(event.get("user") or ""),
        message_ts=str(event.get("ts") or ""),
        thread_ts=event.get("thread_ts") or event.get("ts"),
    )


class SilentReply:
    async def start(self) -> None:
        return None

    async def status(self, text: str) -> None:
        return None

    async def finish(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        return None


@dataclass
class PlaceholderReply:
    """DMs: post a placeholder, update it while working, replace it with the answer."""

    egress: SlackEgress
    channel: str
    thread_ts: str | None
    ts: str | None = None

    async def start(self) -> None:
        self.ts = await self.egress(text=PLACEHOLDER, channel=self.channel, thread_ts=self.thread_ts)

    async def status(self, text: str) -> None:
        if self.ts is None:
            return
        try:
            await self.egress.update(self.channel, self.ts, f"_{text}…_")
        except Exception as exc:
            logger.info("status update failed channel=%s error=%s", self.channel, type(exc).__name__)

    async def finish(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        messages = outgoing_messages(text, blocks)
        first_text, first_blocks = messages[0]
        if self.ts is None:
            await self.egress(text=first_text, channel=self.channel, thread_ts=self.thread_ts, blocks=first_blocks)
        else:
            try:
                await self.egress.update(self.channel, self.ts, first_text, first_blocks)
            except Exception as exc:
                logger.info("update failed channel=%s error=%s; posting instead", self.channel, type(exc).__name__)
                await self.egress(text=first_text, channel=self.channel, thread_ts=self.thread_ts, blocks=first_blocks)
        for chunk_text, chunk_blocks in messages[1:]:
            await self.egress(text=chunk_text, channel=self.channel, thread_ts=self.thread_ts, blocks=chunk_blocks)


@dataclass
class EphemeralReply:
    """Channels: ephemeral messages cannot be updated, so mark the user's message while working."""

    egress: SlackEgress
    channel: str
    user: str
    message_ts: str
    thread_ts: str | None

    async def start(self) -> None:
        if self.message_ts:
            await self.egress.react(self.channel, self.message_ts, WORKING_REACTION, on=True)

    async def status(self, text: str) -> None:
        return None

    async def finish(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        try:
            for chunk_text, chunk_blocks in outgoing_messages(text, blocks):
                await self.egress(
                    text=chunk_text,
                    channel=self.channel,
                    thread_ts=self.thread_ts,
                    blocks=chunk_blocks,
                    user=self.user,
                    ephemeral=True,
                )
        finally:
            if self.message_ts:
                await self.egress.react(self.channel, self.message_ts, WORKING_REACTION, on=False)


def outgoing_messages(
    text: str, blocks: list[dict[str, Any]] | None
) -> list[tuple[str, list[dict[str, Any]] | None]]:
    """Split long text into messages. Cards ride on the last one, under its text."""
    chunks = split_text(text)
    messages: list[tuple[str, list[dict[str, Any]] | None]] = [(chunk, None) for chunk in chunks]
    if blocks:
        last = chunks[-1]
        # Slack renders blocks instead of text, so the text must also be a block.
        lead = [{"type": "section", "text": {"type": "mrkdwn", "text": last}}] if last.strip() else []
        messages[-1] = (last, lead + blocks)
    return messages


def split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    remaining = text.strip()
    if len(remaining) <= limit:
        return [remaining]
    chunks: list[str] = []
    while len(remaining) > limit:
        cut = max(remaining.rfind("\n\n", 0, limit), remaining.rfind("\n", 0, limit))
        if cut <= 0:
            cut = remaining.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
