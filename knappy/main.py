"""Socket Mode entrypoint: python -m knappy.main"""

from __future__ import annotations

import asyncio
import os

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from knappy.config import Settings
from knappy.db.repository import SqliteRepository
from knappy.runtime import KnappyRuntime
from knappy.scheduler import sqlite_path
from knappy.slack.actions import register_actions
from knappy.slack.app import create_app


async def _serve() -> None:
    settings = Settings.from_env()
    workspace_id = os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws")
    repo = SqliteRepository(sqlite_path(settings.database_url))
    await repo.connect()
    await repo.init_schema()
    await repo.upsert_workspace(workspace_id, "Knappy", settings.slack_bot_token)
    runtime = KnappyRuntime(repo, workspace_id=workspace_id)
    app = create_app(settings, processor=runtime.handle_event)
    register_actions(app, runtime)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    print("⚡️ Knappy is connected via Socket Mode!")
    await handler.start_async()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
