"""Bolt application factory."""

from __future__ import annotations

from slack_bolt.async_app import AsyncApp

from knappy.config import Settings
from knappy.slack.events import EventDeduplicator, on_app_mention, on_message


def create_app(settings: Settings, processor=None) -> AsyncApp:
    app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
    deduper = EventDeduplicator()

    @app.event("message")
    async def handle_message(event, ack):
        await on_message(event, ack, processor=processor, deduper=deduper)

    @app.event("app_mention")
    async def handle_app_mention(event, ack):
        await on_app_mention(event, ack, processor=processor, deduper=deduper)

    return app
