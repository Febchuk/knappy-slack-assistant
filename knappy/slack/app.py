"""Bolt application factory."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from slack_bolt.async_app import AsyncApp

from knappy.config import Settings
from knappy.slack.events import EventDeduplicator, Listener, on_app_mention, on_message

# Spec 22: every handler is told which workspace the event came from.
TeamProcessor = Callable[[dict[str, Any], str], Awaitable[None]]
AwarenessFor = Callable[[str], Awaitable[Listener | None]]
OnUninstalled = Callable[[str], Awaitable[None]]


async def _no_awareness(team_id: str) -> None:
    return None


def create_app(
    settings: Settings,
    processor: TeamProcessor | None = None,
    awareness: AwarenessFor = _no_awareness,
    *,
    authorize: Callable[..., Awaitable[Any]] | None = None,
    on_uninstalled: OnUninstalled | None = None,
) -> AsyncApp:
    """With `authorize`, each event runs with its own workspace's token. Without it, SLACK_BOT_TOKEN's."""
    if authorize is not None:
        app = AsyncApp(authorize=authorize, signing_secret=settings.slack_signing_secret)
    else:
        app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
    deduper = EventDeduplicator()

    def bind(team_id: str):
        if processor is None:
            return None

        async def process(event: dict[str, Any]) -> None:
            await processor(event, team_id)

        return process

    @app.event("message")
    async def handle_message(event, ack, body, context):
        await on_message(
            event, ack, processor=bind(context.team_id), deduper=deduper, awareness=await awareness(context.team_id),
            authorizations=body.get("authorizations"),
        )

    @app.event("app_mention")
    async def handle_app_mention(event, ack, context):
        await on_app_mention(event, ack, processor=bind(context.team_id), deduper=deduper)

    @app.event("app_uninstalled")
    async def handle_uninstalled(ack, context):
        await ack()
        if on_uninstalled is not None:
            await on_uninstalled(context.team_id)

    return app
