"""Reading the workspace for the owner (Spec 18 §2-§3): routing, the structural filter, buffers, and catch-up.

Messages live only in memory, per conversation, until a flush hands them to the relevance pass. A conversation
flushes after 5 minutes of quiet or at 30 messages. At start, the owner's most recent conversations are read from
their cursors (at most 7 days back); after that, live events carry everything (Spec 23 §5). An hourly re-read cannot
fit Slack's 1-a-minute history limit for apps outside the Marketplace.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from knappy.awareness.relevance import Conversation, RelevancePass, SlackMessage
from knappy.awareness.store import AwarenessStore
from knappy.db.repository import format_ts
from knappy.memory.engine import MemoryEngine
from knappy.slack.errors import retry_after, slack_error
from knappy.slack.users import UserDirectory

logger = logging.getLogger("knappy")

# A person's new message. Joins, leaves, topic changes, bot posts, and thread-broadcast notices are not.
USER_SUBTYPES = frozenset({None, "file_share", "thread_broadcast"})
CONVERSATION_TYPES = "public_channel,private_channel,mpim,im"
_EMOJI_ONLY = re.compile(r"^(?:\s*:[a-z0-9_+\-']+:\s*)+$")
_REF = re.compile(r"^<#([CGD][A-Z0-9]+)(?:\|([^>]*))?>$")


@dataclass(frozen=True)
class Pacing:
    quiet: timedelta = timedelta(minutes=5)
    batch: int = 30
    history: timedelta = timedelta(days=7)
    # Spec 23 §5: one catch-up at start, DMs first, capped so it ends in about half an hour at 1 call a minute.
    catch_up_conversations: int = 30
    catch_up_pages: int = 1
    # Tier 3 methods allow about 50 calls a minute; stay under it. A rate limit waits for Slack's Retry-After.
    call_gap_s: float = 1.2
    rate_limit_retries: int = 3


class Awareness:
    def __init__(
        self,
        *,
        owner: str,
        user_client: Any,
        bot_client: Any,
        bot_user_id: str | None,
        store: AwarenessStore,
        relevance: RelevancePass,
        memory: MemoryEngine,
        clock: Callable[[], datetime],
        over_budget: Callable[[str], Awaitable[bool]],
        pacing: Pacing | None = None,
        on_first_run: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.owner = owner
        self.user_client = user_client
        self.bot_client = bot_client
        self.bot_user_id = bot_user_id
        self.store = store
        self.relevance = relevance
        self.memory = memory
        self.clock = clock
        self.over_budget = over_budget
        self.pacing = pacing or Pacing()
        self.on_first_run = on_first_run
        self._first_run_pending = False
        self._buffers: dict[str, OrderedDict[str, SlackMessage]] = {}
        self._seen: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._conversations: dict[str, Conversation] = {}
        self._knappy_dm: dict[str, bool] = {}
        self._excluded: set[str] | None = None
        self._caught_up_at: datetime | None = None
        self._retry_at: dict[str, tuple[int, datetime]] = {}

    async def wants(self, event: dict[str, Any], authorizations: list[dict[str, Any]] | None = None) -> bool:
        """Spec 18 §2.2: does this message event belong to awareness rather than the agent loop?

        Slack delivers an event once per app, naming one installation that can see it, so the authorization alone
        cannot tell the owner's DM with Knappy from their DM with a colleague. The bot can see only the former.
        """
        if event.get("type") == "app_mention":
            return False
        authorization = (authorizations or [{}])[0]
        if authorization.get("user_id") and not authorization.get("is_bot") and authorization["user_id"] != self.owner:
            return False
        if self.bot_user_id and f"<@{self.bot_user_id}" in _text(event):
            return False
        if event.get("channel_type") == "im":
            return not (authorization.get("is_bot") or await self._is_knappy_dm(str(event.get("channel") or "")))
        return True

    async def accept(self, event: dict[str, Any]) -> bool:
        """The structural filter (Spec 18 §3.1), then the conversation's buffer. True when buffered."""
        channel = str(event.get("channel") or "")
        if not channel or channel in await self.excluded():
            return False
        edited = event.get("subtype") == "message_changed"
        message = _message(event, channel)
        if message is None or not self._worth_reading(message):
            return False
        key = (channel, message.ts)
        buffer = self._buffers.setdefault(channel, OrderedDict())
        if key in self._seen:
            if not edited:
                return False
            if message.ts not in buffer and not _meaning_changed(self._seen[key], message.text):
                return False
        self._seen[key] = message.text
        self._seen.move_to_end(key)
        while len(self._seen) > 50_000:
            self._seen.popitem(last=False)
        buffer[message.ts] = message
        return True

    def _worth_reading(self, message: SlackMessage) -> bool:
        if message.user == self.owner:
            return True
        if _EMOJI_ONLY.match(message.text):
            return False
        return len(message.text.split()) >= 3 or f"<@{self.owner}" in message.text

    async def tick(self) -> None:
        """Catch up once, then flush every conversation that went quiet or filled up."""
        now = self.clock()
        if self._caught_up_at is None:
            self._caught_up_at = now
            try:
                await self.catch_up(now)
                self._first_run_pending = self.on_first_run is not None
            except Exception:
                logger.exception("awareness catch-up failed owner=%s", self.owner)
        for channel in list(self._buffers):
            await self._flush(channel, now)
        if self._first_run_pending and not self._buffers:
            self._first_run_pending = False
            try:
                await self.on_first_run(self.owner)  # type: ignore[misc]
            except Exception:
                logger.exception("first run failed owner=%s", self.owner)

    async def _flush(self, channel: str, now: datetime) -> None:
        buffer = self._buffers.get(channel)
        while buffer:
            ordered = sorted(buffer.values(), key=lambda message: float(message.ts))
            if len(ordered) < self.pacing.batch and now - ordered[-1].at < self.pacing.quiet:
                return
            failures, retry_at = self._retry_at.get(channel, (0, now))
            if now < retry_at:
                return
            if await self.over_budget(self.owner):
                for message in ordered[: -self.pacing.batch]:
                    del buffer[message.ts]
                logger.info("awareness over budget owner=%s channel=%s held=%d", self.owner, channel, len(buffer))
                return
            batch = ordered[: self.pacing.batch]
            try:
                await self.relevance.process(self.owner, await self.conversation(channel), batch, now)
            except Exception:
                delay = timedelta(minutes=min(2 ** (failures + 1), 60))
                self._retry_at[channel] = (failures + 1, now + delay)
                logger.exception("awareness flush failed owner=%s channel=%s; retry in %s", self.owner, channel, delay)
                return
            self._retry_at.pop(channel, None)
            for message in batch:
                del buffer[message.ts]
        self._buffers.pop(channel, None)

    async def catch_up(self, now: datetime | None = None) -> int:
        """Read what the owner's conversations said since their cursors, at most 7 days back (Spec 18 §2.3)."""
        now = now or self.clock()
        floor = (now - self.pacing.history).timestamp()
        cursors = await self.store.cursors(self.owner)
        excluded = await self.excluded()
        await self._list_conversations()
        await self._refresh_directory(now)
        read = 0
        candidates = [
            conversation for conversation in self._conversations.values()
            if conversation.id not in excluded and not self._knappy_dm.get(conversation.id)
        ]
        # DMs first, then whatever was read most recently before; the rest arrive as live events.
        candidates.sort(key=lambda conversation: (not conversation.direct, -float(cursors.get(conversation.id, 0))))
        for conversation in candidates[: self.pacing.catch_up_conversations]:
            oldest = max(float(cursors.get(conversation.id, 0)), floor)
            for message in await self._history(conversation.id, oldest):
                read += await self.accept({**message, "channel": conversation.id})
        logger.info("awareness catch-up owner=%s conversations=%d buffered=%d", self.owner, len(self._conversations), read)
        return read

    async def _refresh_directory(self, now: datetime) -> None:
        """Cache Slack ids and names for this owner. Message text is not stored."""
        repo = self.memory.store.repo
        workspace = self.memory.store.workspace_id
        refreshed = format_ts(now)
        for conversation in self._conversations.values():
            if conversation.direct:
                continue
            name = (conversation.name or "").removeprefix("#")
            if not name:
                continue
            await repo.upsert_directory_channel(
                workspace, self.owner, conversation.id, name=name, refreshed_at=refreshed
            )
        directory = UserDirectory(self.user_client)
        await repo.upsert_directory_users(
            workspace, self.owner,
            [
                {
                    "slack_user_id": member["id"],
                    "display_name": (member.get("profile") or {}).get("display_name") or "",
                    "real_name": (member.get("profile") or {}).get("real_name") or member.get("real_name") or "",
                    "handle": member.get("name") or "",
                }
                for member in await directory._list_members()
            ],
            refreshed_at=refreshed,
        )

    async def _history(self, channel: str, oldest: float) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor = None
        for _page in range(self.pacing.catch_up_pages):
            response = await self._call(
                self.user_client.conversations_history, channel=channel, oldest=f"{oldest:.6f}", limit=200,
                **({"cursor": cursor} if cursor else {}),
            )
            if response is None:
                break
            messages += response.get("messages") or []
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        replies: list[dict[str, Any]] = []
        for parent in messages:
            if parent.get("reply_count") and float(parent.get("latest_reply") or 0) > oldest:
                response = await self._call(
                    self.user_client.conversations_replies, channel=channel, ts=parent["ts"], oldest=f"{oldest:.6f}", limit=200
                )
                replies += [
                    reply for reply in (response or {}).get("messages") or []
                    if reply.get("ts") != parent["ts"] and float(reply.get("ts") or 0) > oldest
                ]
        return sorted(messages + replies, key=lambda message: float(message.get("ts") or 0))

    async def _list_conversations(self) -> list[Conversation]:
        listed: list[Conversation] = []
        cursor = None
        while True:
            response = await self._call(
                self.user_client.users_conversations, types=CONVERSATION_TYPES, exclude_archived=True, limit=200,
                **({"cursor": cursor} if cursor else {}),
            )
            if response is None:
                break
            for channel in response.get("channels") or []:
                if channel.get("is_im") and channel.get("user") == self.bot_user_id:
                    self._knappy_dm[channel["id"]] = True
                    continue
                conversation = await self._describe(channel)
                self._conversations[conversation.id] = conversation
                listed.append(conversation)
            cursor = (response.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return listed

    async def conversation(self, channel: str) -> Conversation:
        known = self._conversations.get(channel)
        if known is not None:
            return known
        response = await self._call(self.user_client.conversations_info, channel=channel)
        info = (response or {}).get("channel") or {"id": channel}
        described = await self._describe({**info, "id": channel})
        self._conversations[channel] = described
        return described

    async def _describe(self, channel: dict[str, Any]) -> Conversation:
        if channel.get("is_im"):
            other = await self.relevance.name(channel["user"]) if channel.get("user") else "someone"
            return Conversation(channel["id"], f"DM with {other}", True)
        if channel.get("is_mpim"):
            return Conversation(channel["id"], "a group DM", True)
        name = channel.get("name")
        return Conversation(channel["id"], f"#{name}" if name else None, False)

    async def _is_knappy_dm(self, channel: str) -> bool:
        """The bot can open only its own DMs, so conversations.info with the bot token tells them apart."""
        if channel not in self._knappy_dm:
            try:
                response = await self.bot_client.conversations_info(channel=channel)
                self._knappy_dm[channel] = bool(response.get("ok", True))
            except Exception:
                self._knappy_dm[channel] = False
        return self._knappy_dm[channel]

    async def excluded(self) -> set[str]:
        if self._excluded is None:
            self._excluded = await self.store.excluded(self.owner)
        return self._excluded

    async def stop_watching(self, owner: str, ref: str) -> dict[str, Any]:
        """Spec 18 §7: stop reading a conversation and forget what was learned from it."""
        if owner != self.owner:
            return {"error": "Workspace awareness reads only its owner's conversations."}
        conversation = await self._find(ref)
        if conversation is None:
            return {"error": f"I couldn't find a conversation you're in called {ref!r}."}
        now = self.clock()
        async with self.store.repo.transaction():
            await self.store.exclude(owner, conversation.id, conversation.name, now)
        (await self.excluded()).add(conversation.id)
        self._buffers.pop(conversation.id, None)
        events = await self.store.channel_events(owner, conversation.id)
        result = await self.memory.retract(owner, events, now) if events else {"retracted": 0}
        async with self.store.repo.transaction():
            await self.store.drop_turns(owner, conversation.id)
        logger.info("awareness excluded owner=%s channel=%s retracted=%s", owner, conversation.id, result.get("retracted"))
        return {"stopped_watching": conversation.name or conversation.id, "forgotten_observations": result.get("retracted", 0)}

    async def _find(self, ref: str) -> Conversation | None:
        text = ref.strip()
        linked = _REF.match(text)
        if linked:
            return await self.conversation(linked.group(1))
        if re.fullmatch(r"[CGD][A-Z0-9]{6,}", text):
            return await self.conversation(text)
        wanted = "#" + text.lstrip("#").lower()
        for attempt in range(2):
            for conversation in self._conversations.values():
                if (conversation.name or "").lower() == wanted:
                    return conversation
            if attempt == 0:
                await self._list_conversations()
        return None

    async def _call(self, method: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """One Slack Web API call, paced under the tier limit. A failure is logged and reads as nothing.

        A rate limit waits as long as Slack's Retry-After asks, then tries again, a few times at most.
        """
        name = getattr(method, "__name__", "?")
        try:
            for attempt in range(self.pacing.rate_limit_retries + 1):
                try:
                    return await method(**kwargs)
                except Exception as exc:
                    wait = retry_after(exc)
                    if wait is None or attempt == self.pacing.rate_limit_retries:
                        logger.info("awareness slack call failed method=%s error=%s", name, slack_error(exc))
                        return None
                    logger.info("awareness rate limited method=%s retry_after=%.0f", name, wait)
                    await asyncio.sleep(wait)
            return None
        finally:
            if self.pacing.call_gap_s:
                await asyncio.sleep(self.pacing.call_gap_s)


def _text(event: dict[str, Any]) -> str:
    if event.get("subtype") == "message_changed":
        return str((event.get("message") or {}).get("text") or "")
    return str(event.get("text") or "")


def _message(event: dict[str, Any], channel: str) -> SlackMessage | None:
    """The person's message in an event, or None for anything that is not one."""
    if event.get("subtype") == "message_changed":
        inner = event.get("message") or {}
        if inner.get("subtype") not in USER_SUBTYPES:
            return None
        event = inner
    if event.get("subtype") not in USER_SUBTYPES or event.get("bot_id") or not event.get("user") or not event.get("ts"):
        return None
    text = str(event.get("text") or "").strip()
    if not text:
        return None
    return SlackMessage(channel, str(event["ts"]), str(event["user"]), text, event.get("thread_ts"))


def _meaning_changed(before: str, after: str) -> bool:
    """A fixed typo is the same message; a new date, number, name, or sentence is not."""
    if re.findall(r"\d+|<@\w+>", before) != re.findall(r"\d+|<@\w+>", after):
        return True
    return difflib.SequenceMatcher(None, before.lower(), after.lower()).ratio() < 0.85
