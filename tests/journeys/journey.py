"""Spec 17 §2: drive Knappy the way a person and the clock would, across restarts, and observe only behavior."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from fakes import FakeApp, FakeClock, FakeSlack, only
from knappy.config import Settings
from knappy.files.service import SlackDownloader
from knappy.llm.types import Model
from knappy.awareness.ingest import Pacing
from knappy.main import heartbeat_tick, open_runtime
from knappy.slack.installations import Installation
from knappy.runtime import KnappyRuntime
from knappy.slack.actions import register_actions
from knappy.slack.egress import PLACEHOLDER
from knappy.slack.events import on_message
from knappy.web import WebFetcher

WORKSPACE = "T_JOURNEY"
# Monday. Journeys that say "by Thursday" mean 2026-10-08.
START = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
HEARTBEAT_EVERY = timedelta(minutes=30)


@dataclass(frozen=True)
class FakeFile:
    name: str
    mimetype: str
    content: bytes
    # The size Slack reports, when it should differ from the content's (a 30 MB file without 30 MB of test data).
    size: int | None = None


@dataclass(frozen=True)
class ToolUse:
    owner: str
    name: str
    args: dict[str, Any]
    result: Any


@dataclass
class SlackCapture:
    """Every Slack call made during one step, and how the owner's DM now reads."""

    slack: FakeSlack
    posts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    ephemerals: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    post_ts: list[str] = field(default_factory=list)
    tools: list[ToolUse] = field(default_factory=list)
    # For a workspace message: the event as Slack delivered it.
    event: dict[str, Any] | None = None

    @property
    def reply(self) -> dict[str, Any]:
        """The answer as the user sees it: the first post of the step, overlaid by its latest update."""
        assert self.post_ts, "nothing was posted"
        return self.slack.shown(self.post_ts[0])

    @property
    def text(self) -> str:
        shown = self.reply
        blocks = " ".join(json.dumps(block) for block in shown.get("blocks") or [])
        return f"{shown['text']} {blocks}".strip()

    def to(self, channel: str) -> list[dict[str, Any]]:
        return [post for post in self.posts if post["channel"] == channel]

    def called(self, name: str) -> list[ToolUse]:
        return [use for use in self.tools if use.name == name]


class Journey:
    def __init__(
        self,
        tmp_path: Path,
        *,
        model: Model,
        clock: FakeClock,
        slack: FakeSlack | None = None,
        fetcher: WebFetcher | None = None,
        owner: str | None = None,
        first_run: bool = False,
    ) -> None:
        self.model = model
        # Spec 18: with an owner, Knappy holds that person's user token and reads their conversations.
        self.owner = owner
        # Spec 23 §6: journeys start from an install whose first-run DM already went out, unless they test it.
        self.first_run = first_run
        self.fetcher = fetcher
        self.clock = clock
        self.slack = slack or FakeSlack(tz="UTC")
        self.settings = Settings(
            slack_bot_token="xoxb-journey",
            slack_app_token="xapp-journey",
            slack_signing_secret="secret",
            gemini_api_key="unused",
            database_url=f"sqlite:///{tmp_path / 'knappy.db'}",
        )
        self.tools: list[ToolUse] = []
        # Slack's file server: download URL -> bytes, and every request it received.
        self.files: dict[str, bytes] = {}
        self.downloads: list[httpx.Request] = []
        self.runtime: KnappyRuntime | None = None
        self.app = FakeApp()
        self._ts = itertools.count(1)
        self._heartbeat_at: datetime | None = None
        self.user_slack = FakeSlack(tz="UTC", user_id=owner, members=self.slack.members) if owner else None

    async def start(self) -> Journey:
        downloader = SlackDownloader(self.settings.slack_bot_token, transport=httpx.MockTransport(self._file_server))
        runtime = await open_runtime(
            self.settings, installation=Installation(WORKSPACE, "Journey", self.settings.slack_bot_token or ""),
            client=self.slack, model=self.model, clock=self.clock,
            fetcher=self.fetcher, downloader=downloader, user_client=self.user_slack, awareness_pacing=Pacing(call_gap_s=0),
        )
        if self.owner and not self.first_run:
            await runtime.store.mark_first_run(self.owner, self.clock())
        self._record_tools(runtime)
        self.app = FakeApp()
        register_actions(self.app, only(runtime))
        self.runtime = runtime
        return self

    async def restart(self) -> None:
        """Close the runtime and its database, then reopen from the same file as `python -m knappy.main` would."""
        await self.close()
        self._heartbeat_at = None
        await self.start()

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.repo.close()
            self.runtime = None

    @property
    def repo(self):
        assert self.runtime is not None, "journey is not running"
        return self.runtime.repo

    async def dm(
        self, user: str, text: str, *, thread: str | None = None, files: tuple[FakeFile, ...] = ()
    ) -> SlackCapture:
        """A DM from `user`. `thread` is the parent ts to reply under, or "new" to start a thread at this message.

        `files` arrive the way Slack shares them: a file_share message whose download links need the bot token.
        """
        ts = f"{int(self.clock().timestamp())}.{next(self._ts):06d}"
        event = {"text": text, "channel": dm_channel(user), "channel_type": "im", "user": user, "ts": ts}
        self.slack.visible.add(dm_channel(user))
        if files:
            event["subtype"] = "file_share"
            event["files"] = [self._share(file) for file in files]
        if thread:
            event["thread_ts"] = ts if thread == "new" else thread
        with self._capture() as capture:
            await self._running.handle_event(event)
        return capture.result

    async def workspace(
        self, user: str, channel: str, text: str, *, thread: str | None = None, edit_of: str | None = None,
        bot: bool = False,
    ) -> SlackCapture:
        """A message in one of the owner's conversations, delivered as a user-scoped event over Socket Mode.

        Channel ids name their kind: C_DESIGN is #design, G_... a private channel, D_SAM the owner's DM with
        U_SAM, M_... a group DM. The message also lands in the conversation's history, for catch-up to read.
        """
        assert self.user_slack is not None and self.owner, "this journey has no user token"
        ts = edit_of or f"{int(self.clock().timestamp())}.{next(self._ts):06d}"
        kind = {"C": "channel", "G": "group", "D": "im", "M": "mpim"}[channel[0]]
        message: dict[str, Any] = {"type": "message", "user": user, "text": text, "ts": ts}
        if thread:
            message["thread_ts"] = thread
        if bot:
            message = {**message, "subtype": "bot_message", "bot_id": "B_OTHER"}
        self._register(channel, kind, user)
        history = self.user_slack.history.setdefault(channel, [])
        history[:] = [old for old in history if old["ts"] != ts] + [message]
        event = {**message, "channel": channel, "channel_type": kind}
        if edit_of:
            event = {"type": "message", "subtype": "message_changed", "channel": channel, "channel_type": kind,
                     "message": message, "ts": f"{ts}9"}
        with self._capture() as capture:
            await self.deliver(event)
        capture.result.event = {**message, "channel": channel, "channel_type": kind}
        return capture.result

    async def deliver(self, event: dict[str, Any], *, is_bot: bool = False) -> None:
        """Slack's delivery of one message event to the app, through the same routing the Bolt handler uses."""
        async def ack() -> None:
            return None

        runtime = self._running
        authorization = {"is_bot": is_bot, "user_id": "UBOT" if is_bot else self.owner}
        await on_message(event, ack, processor=runtime.handle_event, awareness=runtime.awareness, authorizations=[authorization])

    def _register(self, channel: str, kind: str, user: str) -> None:
        assert self.user_slack is not None
        if any(known["id"] == channel for known in self.user_slack.conversations):
            return
        name = channel.split("_", 1)[-1].lower()
        described: dict[str, Any] = {"id": channel}
        if kind == "im":
            described.update(is_im=True, user=user if user != self.owner else f"U_{channel.split('_', 1)[-1]}")
        elif kind == "mpim":
            described.update(is_mpim=True, name=f"mpdm-{name}")
        else:
            described.update(name=name, is_private=kind == "group")
        self.user_slack.conversations.append(described)

    async def click(self, user: str, action_id: str, value: str) -> SlackCapture:
        """Press the button carrying `value`, on the message where it was last shown."""
        channel, message_ts = self._message_with(action_id, value)
        body = {
            "user": {"id": user},
            "channel": {"id": channel},
            "message": {"ts": message_ts},
            "trigger_id": "trigger",
            "actions": [{"action_id": action_id, "value": value}],
        }

        async def ack() -> None:
            return None

        with self._capture() as capture:
            await self.app.handlers[action_id](ack=ack, body=body, client=self.slack)
        return capture.result

    async def advance(self, *, step: timedelta = HEARTBEAT_EVERY, **delta: float) -> SlackCapture:
        """Move the clock, running the heartbeat and memory work the process would have run on the way."""
        target = self.clock() + timedelta(**delta)
        with self._capture() as capture:
            while self.clock() < target:
                self.clock.now = min(self.clock() + step, target)
                await self.tick()
        return capture.result

    async def tick(self) -> None:
        runtime = self._running
        now = self.clock()
        if runtime.awareness is not None:
            await runtime.awareness.tick()
        if self._heartbeat_at is None or now - self._heartbeat_at >= HEARTBEAT_EVERY:
            await heartbeat_tick(runtime.heartbeat)
            self._heartbeat_at = now
        await runtime.memory_engine.tick()

    async def rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cursor = await self.repo.connection.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    async def active_memory(self, owner: str) -> list[str]:
        """Every active record and the profile, as text, for checking what the assistant still believes."""
        records = await self.rows(
            "SELECT title || ' ' || aliases || ' ' || body AS text FROM memory_records WHERE owner_user_id = ? AND status = 'ACTIVE'",
            (owner,),
        )
        profile = await self.rows("SELECT body FROM user_profile WHERE owner_user_id = ?", (owner,))
        return [row["text"] for row in records] + [row["body"] for row in profile]

    def called(self, name: str, owner: str | None = None) -> list[ToolUse]:
        return [use for use in self.tools if use.name == name and (owner is None or use.owner == owner)]

    @property
    def _running(self) -> KnappyRuntime:
        assert self.runtime is not None, "call start() first"
        return self.runtime

    def _share(self, file: FakeFile) -> dict[str, Any]:
        file_id = f"F{len(self.files) + 1:04d}"
        url = f"https://files.slack.com/files-pri/{WORKSPACE}-{file_id}/download/{file.name}"
        self.files[url] = file.content
        return {
            "id": file_id, "name": file.name, "mimetype": file.mimetype,
            "size": len(file.content) if file.size is None else file.size, "url_private_download": url,
        }

    def _file_server(self, request: httpx.Request) -> httpx.Response:
        self.downloads.append(request)
        if request.headers.get("authorization") != f"Bearer {self.settings.slack_bot_token}":
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>Sign in</html>")
        content = self.files.get(str(request.url))
        if content is None:
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=content)

    def _record_tools(self, runtime: KnappyRuntime) -> None:
        from knappy.agent.tools import current_owner

        registry = runtime.tools
        call = registry.call

        async def recording(name: str, arguments: dict[str, Any]) -> Any:
            result = await call(name, arguments)
            self.tools.append(ToolUse(current_owner.get() or "", name, arguments, result))
            return result

        registry.call = recording  # type: ignore[method-assign]

    def _capture(self) -> _Capturing:
        return _Capturing(self)

    def _message_with(self, action_id: str, value: str) -> tuple[str, str]:
        """The newest message that ever showed the button. A stale client can still press a replaced card."""
        for index in range(len(self.slack.posts), 0, -1):
            ts = f"100.{index}"
            post = self.slack.posts[index - 1]
            versions = [post, *(update for update in self.slack.updates if update["ts"] == ts)]
            for version in versions:
                for block in version.get("blocks") or []:
                    for element in block.get("elements") or []:
                        if element.get("action_id") == action_id and element.get("value") == value:
                            return post["channel"], ts
        raise AssertionError(f"no message showed {action_id}={value}")


class _Capturing:
    def __init__(self, journey: Journey) -> None:
        self.journey = journey
        self.result = SlackCapture(journey.slack)

    def __enter__(self) -> _Capturing:
        slack = self.journey.slack
        self.marks = (len(slack.posts), len(slack.updates), len(slack.ephemerals), len(slack.reactions), len(self.journey.tools))
        return self

    def __exit__(self, *exc: object) -> None:
        slack = self.journey.slack
        posts, updates, ephemerals, reactions, tools = self.marks
        self.result.posts = slack.posts[posts:]
        self.result.post_ts = [f"100.{index}" for index in range(posts + 1, len(slack.posts) + 1)]
        self.result.updates = slack.updates[updates:]
        self.result.ephemerals = slack.ephemerals[ephemerals:]
        self.result.reactions = slack.reactions[reactions:]
        self.result.tools = self.journey.tools[tools:]


def dm_channel(user: str) -> str:
    return f"D{user}"


def placeholder_replaced(capture: SlackCapture) -> bool:
    return capture.posts[0]["text"] == PLACEHOLDER and capture.reply["text"] != PLACEHOLDER
