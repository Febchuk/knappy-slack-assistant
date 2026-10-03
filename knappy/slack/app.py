"""Bolt application factory."""

from __future__ import annotations

from slack_bolt.async_app import AsyncApp

from knappy.config import Settings
from knappy.slack.events import EventDeduplicator, Listener, on_app_mention, on_message


def create_app(settings: Settings, processor=None, awareness: dict[str, Listener] | None = None) -> AsyncApp:
    """`awareness` is filled in once the runtime exists: {"listener": Awareness}. Empty means awareness is off."""
    app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
    deduper = EventDeduplicator()
    holder = awareness if awareness is not None else {}

    @app.event("message")
    async def handle_message(event, ack, body):
        await on_message(
            event, ack, processor=processor, deduper=deduper, awareness=holder.get("listener"),
            authorizations=body.get("authorizations"),
        )

    @app.event("app_mention")
    async def handle_app_mention(event, ack):
        await on_app_mention(event, ack, processor=processor, deduper=deduper)

    return app
