"""Socket Mode entrypoint: python -m knappy.main"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from knappy.awareness.ingest import Awareness, Pacing
from knappy.config import Settings, load_dotenv
from knappy.db.factory import open_repository
from knappy.db.repository import utc_now
from knappy.files.service import SlackDownloader
from knappy.files.store import DocumentStore
from knappy.ingestion.embed import semantic
from knappy.llm.client import GeminiClient, ModelIds
from knappy.llm.types import Model
from knappy.mcp.callback import start_callback
from knappy.mcp.hub import McpHub
from knappy.mcp.servers import load_servers
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


HEARTBEAT_EVERY_S = 15 * 60


async def heartbeat_tick(engine) -> None:
    """One heartbeat pass. Each owner's brief runs at 08:00 in their own timezone (Spec 16 PRO-BUG-3)."""
    try:
        await engine.run_tick()
    except Exception:
        logging.getLogger("knappy").exception("heartbeat failed")


async def _heartbeat_loop(engine) -> None:
    while True:
        await heartbeat_tick(engine)
        await asyncio.sleep(HEARTBEAT_EVERY_S)


async def _memory_loop(engine) -> None:
    """Idle reconciles and the nightly pass (Spec 13 §3.3). Every minute, so idleness is noticed promptly."""
    while True:
        try:
            await engine.tick()
        except Exception:
            logging.getLogger("knappy").exception("memory tick failed")
        await asyncio.sleep(60)


AWARENESS_EVERY_S = 30


async def _awareness_loop(awareness: Awareness) -> None:
    """Catch-up and buffer flushes (Spec 18 §2-§3). Every 30 seconds, so a quiet conversation flushes on time."""
    while True:
        try:
            await awareness.tick()
        except Exception:
            logging.getLogger("knappy").exception("awareness tick failed")
        await asyncio.sleep(AWARENESS_EVERY_S)


def user_web_client(token: str) -> Any:
    """The owner's own Web API client. Retries on rate limits, since catch-up reads many conversations."""
    from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=token)
    client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))
    return client


async def _identity(client: Any) -> str | None:
    try:
        auth = await client.auth_test()
    except Exception as exc:
        logging.getLogger("knappy").warning("auth.test failed error=%s", type(exc).__name__)
        return None
    return auth.get("user_id")


async def open_runtime(
    settings: Settings,
    *,
    workspace_id: str,
    client: Any,
    model: Model | None = None,
    clock: Callable[[], datetime] = utc_now,
    fetcher: WebFetcher | None = None,
    downloader: SlackDownloader | None = None,
    user_client: Any | None = None,
    awareness_pacing: Pacing | None = None,
) -> KnappyRuntime:
    """Open the database and build the runtime the process serves. Without a model, Gemini with usage recording.

    With a user token (or a `user_client`), workspace awareness reads the token owner's conversations (Spec 18).
    """
    log = logging.getLogger("knappy")
    if user_client is None and settings.slack_user_token:
        user_client = user_web_client(settings.slack_user_token)
    owner = await _identity(user_client) if user_client is not None else None
    if owner is None:
        user_client = None
        log.info("workspace awareness off: SLACK_USER_TOKEN is not set or not valid")
    else:
        log.info("workspace awareness on owner=%s", owner)
    repo = await open_repository(settings.database_url)
    await repo.init_schema()
    await repo.upsert_workspace(workspace_id, "Knappy", settings.slack_bot_token)
    mcp = None
    if settings.public_url and settings.secret_key:
        mcp = McpHub(
            repo, workspace_id, load_servers(), public_url=settings.public_url, secret_key=settings.secret_key, clock=clock
        )
        log.info("mcp on servers=%d", len(mcp.servers))
    else:
        log.info("mcp off: KNAPPY_PUBLIC_URL or KNAPPY_SECRET_KEY is not set")
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
        executor=SlackActionExecutor(client, DocumentStore(repo, workspace_id), user_client=user_client, user_id=owner),
        slack=client,
        memory_config=MemoryConfig(
            admission_threshold=settings.admission_threshold, raw_retention_days=settings.raw_retention_days
        ),
        clock=clock,
        fetcher=fetcher,
        downloader=downloader or SlackDownloader(settings.slack_bot_token),
        user_client=user_client,
        awareness_owner=owner,
        bot_user_id=await _identity(client) if owner else None,
        awareness_threshold=settings.awareness_threshold,
        awareness_pacing=awareness_pacing,
        mcp=mcp,
    )
    migrated = await runtime.store.migrate_contacts(clock())
    if migrated:
        logging.getLogger("knappy").info("memory migrated contacts=%d", migrated)
    return runtime


async def _connected(user_id: str, auth_group: str) -> None:
    """Spec 20 replaces this with a Slack DM."""
    logging.getLogger("knappy").info("mcp connection ready owner=%s auth_group=%s", user_id, auth_group)


async def _serve() -> None:
    load_dotenv()
    settings = Settings.from_env()
    holder: dict[str, KnappyRuntime] = {}
    listener: dict = {}

    async def process(event):
        runtime = holder.get("runtime")
        if runtime is not None:
            await runtime.handle_event(event)

    app = create_app(settings, processor=process, awareness=listener)
    workspace_id = await _workspace_id(app.client)
    runtime = await open_runtime(settings, workspace_id=workspace_id, client=app.client)
    semantic()  # loads the embedder now, and logs a warning if it cannot
    holder["runtime"] = runtime
    register_actions(app, runtime)
    tasks = [asyncio.create_task(_heartbeat_loop(runtime.heartbeat)), asyncio.create_task(_memory_loop(runtime.memory_engine))]
    if runtime.awareness is not None:
        listener["listener"] = runtime.awareness
        tasks.append(asyncio.create_task(_awareness_loop(runtime.awareness)))
    callback = None
    if runtime.mcp is not None:
        callback = await start_callback(runtime.mcp, _connected, settings.callback_port)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    print("⚡️ Knappy is connected via Socket Mode!")
    try:
        await handler.start_async()
    finally:
        if callback is not None:
            await callback.cleanup()
        for task in tasks:
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
