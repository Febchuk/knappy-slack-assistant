"""Socket Mode entrypoint: python -m knappy.main"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from knappy.config import Settings, load_dotenv
from knappy.db.factory import open_repository
from knappy.db.repository import utc_now
from knappy.files.service import SlackDownloader
from knappy.files.store import DocumentStore
from knappy.heartbeat.schedule import cadence_due
from knappy.ingestion.embed import semantic
from knappy.llm.client import GeminiClient, ModelIds
from knappy.llm.types import Model
from knappy.memory import MemoryConfig
from knappy.runtime import KnappyRuntime, usage_recorder
from knappy.slack.actions import register_actions
from knappy.slack.app import create_app
from knappy.slack.egress import build_say
from knappy.slack.executor import SlackActionExecutor
from knappy.web import WebFetcher


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def _workspace_id(client) -> str:
    fallback = os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws")
    try:
        auth = await client.auth_test()
    except Exception:
        return fallback
    team_id = auth.get("team_id") if hasattr(auth, "get") else None
    return team_id or fallback


async def heartbeat_tick(engine, now: datetime, last_digest: date | None) -> date | None:
    """One heartbeat pass. Returns the date the morning digest last ran."""
    include = cadence_due(now, last_digest)
    try:
        await engine.run_tick(include_cadence=include, deliver_digest=include)
    except Exception:
        logging.getLogger("knappy").exception("heartbeat failed")
    return now.date() if include else last_digest


async def _heartbeat_loop(engine) -> None:
    last_digest = None
    while True:
        last_digest = await heartbeat_tick(engine, datetime.now(), last_digest)
        await asyncio.sleep(30 * 60)


async def _memory_loop(engine) -> None:
    """Idle reconciles and the nightly pass (Spec 13 §3.3). Every minute, so idleness is noticed promptly."""
    while True:
        try:
            await engine.tick()
        except Exception:
            logging.getLogger("knappy").exception("memory tick failed")
        await asyncio.sleep(60)


async def open_runtime(
    settings: Settings,
    *,
    workspace_id: str,
    client: Any,
    model: Model | None = None,
    clock: Callable[[], datetime] = utc_now,
    fetcher: WebFetcher | None = None,
    downloader: SlackDownloader | None = None,
) -> KnappyRuntime:
    """Open the database and build the runtime the process serves. Without a model, Gemini with usage recording."""
    repo = await open_repository(settings.database_url)
    await repo.init_schema()
    await repo.upsert_workspace(workspace_id, "Knappy", settings.slack_bot_token)
    say = build_say(client)
    model = model or GeminiClient(
        settings.gemini_api_key,
        ModelIds(agent=settings.model_agent, light=settings.model_light),
        on_usage=usage_recorder(repo, workspace_id),
    )
    runtime = KnappyRuntime(
        repo,
        workspace_id=workspace_id,
        model=model,
        daily_budget_usd=settings.daily_budget_usd,
        say=say,
        sender=say,
        executor=SlackActionExecutor(client, DocumentStore(repo, workspace_id)),
        slack=client,
        memory_config=MemoryConfig(
            admission_threshold=settings.admission_threshold, raw_retention_days=settings.raw_retention_days
        ),
        clock=clock,
        fetcher=fetcher,
        downloader=downloader or SlackDownloader(settings.slack_bot_token),
    )
    migrated = await runtime.store.migrate_contacts(clock())
    if migrated:
        logging.getLogger("knappy").info("memory migrated contacts=%d", migrated)
    return runtime


async def _serve() -> None:
    load_dotenv()
    settings = Settings.from_env()
    holder: dict[str, KnappyRuntime] = {}

    async def process(event):
        runtime = holder.get("runtime")
        if runtime is not None:
            await runtime.handle_event(event)

    app = create_app(settings, processor=process)
    workspace_id = await _workspace_id(app.client)
    runtime = await open_runtime(settings, workspace_id=workspace_id, client=app.client)
    semantic()  # loads the embedder now, and logs a warning if it cannot
    holder["runtime"] = runtime
    register_actions(app, runtime)
    tick = asyncio.create_task(_heartbeat_loop(runtime.heartbeat))
    memory = asyncio.create_task(_memory_loop(runtime.memory_engine))
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    print("⚡️ Knappy is connected via Socket Mode!")
    try:
        await handler.start_async()
    finally:
        for task in (tick, memory):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await runtime.repo.close()


def main() -> None:
    configure_logging()
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
