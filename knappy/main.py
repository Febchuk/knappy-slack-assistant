"""Socket Mode entrypoint: python -m knappy.main. One process serves every installed workspace (Spec 22)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.authorization import AuthorizeResult
from slack_sdk.web.async_client import AsyncWebClient

from knappy.awareness.ingest import Pacing
from knappy.config import Settings, load_dotenv
from knappy.db.factory import open_repository
from knappy.db.repository import SqliteRepository, utc_now
from knappy.fleet import Fleet
from knappy.files.service import SlackDownloader
from knappy.files.store import DocumentStore
from knappy.ingestion.embed import semantic
from knappy.llm.client import GeminiClient, ModelIds
from knappy.llm.types import Model
from knappy.mcp.callback import add_callback, serve
from knappy.mcp.hub import McpHub, state_workspace
from knappy.mcp.servers import load_servers
from knappy.memory import MemoryConfig
from knappy.runtime import KnappyRuntime, usage_recorder
from knappy.slack.actions import register_actions
from knappy.slack.app import create_app
from knappy.slack.egress import build_say
from knappy.slack.executor import SlackActionExecutor
from knappy.slack.install import InstallFlow, add_install_routes
from knappy.slack.installations import Installation, Installations
from knappy.web import WebFetcher


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def env_installation(settings: Settings, client: Any) -> Installation:
    """The workspace SLACK_BOT_TOKEN belongs to, with SLACK_USER_TOKEN as its installer's token."""
    team_id = os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws")
    team_name, bot_user_id = "Knappy", None
    try:
        auth = await client.auth_test()
        team_id = auth.get("team_id") or team_id
        team_name = auth.get("team") or team_name
        bot_user_id = auth.get("user_id")
    except Exception as exc:
        logging.getLogger("knappy").warning("auth.test failed error=%s", type(exc).__name__)
    return Installation(
        team_id=team_id, team_name=team_name, bot_token=settings.slack_bot_token or "", bot_user_id=bot_user_id,
        user_token=settings.slack_user_token,
    )


HEARTBEAT_EVERY_S = 15 * 60


async def heartbeat_tick(engine) -> None:
    """One heartbeat pass. Each owner's brief runs at 08:00 in their own timezone (Spec 16 PRO-BUG-3)."""
    try:
        await engine.run_tick()
    except Exception:
        logging.getLogger("knappy").exception("heartbeat failed")


async def _heartbeat_loop(fleet: Fleet) -> None:
    while True:
        for runtime in fleet.all():
            await heartbeat_tick(runtime.heartbeat)
        await asyncio.sleep(HEARTBEAT_EVERY_S)


async def _memory_loop(fleet: Fleet) -> None:
    """Idle reconciles and the nightly pass (Spec 13 §3.3). Every minute, so idleness is noticed promptly."""
    while True:
        for runtime in fleet.all():
            try:
                await runtime.memory_engine.tick()
            except Exception:
                logging.getLogger("knappy").exception("memory tick failed team=%s", runtime.workspace_id)
        await asyncio.sleep(60)


AWARENESS_EVERY_S = 30


async def _awareness_loop(fleet: Fleet) -> None:
    """Catch-up and buffer flushes (Spec 18 §2-§3). Every 30 seconds, so a quiet conversation flushes on time."""
    while True:
        for runtime in fleet.all():
            if runtime.awareness is None:
                continue
            try:
                await runtime.awareness.tick()
            except Exception:
                logging.getLogger("knappy").exception("awareness tick failed team=%s", runtime.workspace_id)
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
    installation: Installation,
    client: Any,
    repo: SqliteRepository | None = None,
    model: Model | None = None,
    clock: Callable[[], datetime] = utc_now,
    fetcher: WebFetcher | None = None,
    downloader: SlackDownloader | None = None,
    user_client: Any | None = None,
    awareness_pacing: Pacing | None = None,
) -> KnappyRuntime:
    """Build one workspace's runtime. Without a model, Gemini with usage recording. Without a repo, open the database.

    With the installer's user token (or a `user_client`), workspace awareness reads their conversations (Spec 18).
    """
    log = logging.getLogger("knappy")
    workspace_id = installation.team_id
    if user_client is None and installation.user_token:
        user_client = user_web_client(installation.user_token)
    owner = await _identity(user_client) if user_client is not None else None
    if owner is None:
        user_client = None
        log.info("workspace awareness off team=%s: no valid installer user token", workspace_id)
    else:
        log.info("workspace awareness on team=%s owner=%s", workspace_id, owner)
    if repo is None:
        repo = await open_repository(settings.database_url)
        await repo.init_schema()
    await repo.ensure_workspace(workspace_id, installation.team_name)
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
        executor=SlackActionExecutor(
            client, DocumentStore(repo, workspace_id), user_client=user_client, user_id=owner, mcp=mcp
        ),
        slack=client,
        memory_config=MemoryConfig(
            admission_threshold=settings.admission_threshold, raw_retention_days=settings.raw_retention_days
        ),
        clock=clock,
        fetcher=fetcher,
        downloader=downloader or SlackDownloader(installation.bot_token),
        user_client=user_client,
        awareness_owner=owner,
        bot_user_id=(installation.bot_user_id or await _identity(client)) if owner else None,
        awareness_threshold=settings.awareness_threshold,
        awareness_pacing=awareness_pacing,
        mcp=mcp,
    )
    migrated = await runtime.store.migrate_contacts(clock())
    if migrated:
        logging.getLogger("knappy").info("memory migrated contacts=%d", migrated)
    return runtime


async def _serve() -> None:
    load_dotenv()
    settings = Settings.from_env()
    log = logging.getLogger("knappy")
    repo = await open_repository(settings.database_url)
    await repo.init_schema()
    stored = Installations(repo, settings.secret_key) if settings.distributes else None
    manual = await env_installation(settings, AsyncWebClient(token=settings.slack_bot_token)) if settings.slack_bot_token else None

    async def find(team_id: str) -> Installation | None:
        # An OAuth install wins over SLACK_BOT_TOKEN: it is newer, and it carries the installer's user token.
        installation = await stored.get(team_id) if stored is not None else None
        if installation is None and manual is not None and manual.team_id == team_id:
            installation = manual
        return installation

    async def build(installation: Installation) -> KnappyRuntime:
        runtime = await open_runtime(
            settings, installation=installation, client=AsyncWebClient(token=installation.bot_token), repo=repo
        )
        log.info("serving team=%s name=%s", installation.team_id, installation.team_name)
        return runtime

    fleet = Fleet(find, build)

    async def authorize(enterprise_id, team_id, **_) -> AuthorizeResult | None:
        installation = await find(team_id)
        if installation is None:
            return None
        return AuthorizeResult(
            enterprise_id=enterprise_id, team_id=team_id, bot_token=installation.bot_token,
            bot_user_id=installation.bot_user_id, user_token=installation.user_token,
        )

    async def process(event, team_id: str) -> None:
        if runtime := await fleet.get(team_id):
            await runtime.handle_event(event)

    async def awareness(team_id: str):
        runtime = await fleet.get(team_id)
        return runtime.awareness if runtime is not None else None

    async def uninstalled(team_id: str) -> None:
        log.info("uninstalled team=%s", team_id)
        if stored is not None:
            await stored.revoke(team_id)
        fleet.drop(team_id)

    async def runtime_for(body: dict) -> KnappyRuntime | None:
        return await fleet.get((body.get("team") or {}).get("id"))

    app = create_app(settings, processor=process, awareness=awareness, authorize=authorize, on_uninstalled=uninstalled)
    register_actions(app, runtime_for)
    semantic()  # loads the embedder now, and logs a warning if it cannot
    teams = [installation.team_id for installation in await stored.all()] if stored is not None else []
    if manual is not None and manual.team_id not in teams:
        teams.append(manual.team_id)
    for team_id in teams:
        await fleet.get(team_id)
    tasks = [
        asyncio.create_task(_heartbeat_loop(fleet)),
        asyncio.create_task(_memory_loop(fleet)),
        asyncio.create_task(_awareness_loop(fleet)),
    ]
    web_runner = await _start_web(settings, fleet, stored)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    print(f"⚡️ Knappy is connected via Socket Mode! workspaces={len(fleet.all())}")
    try:
        await handler.start_async()
    finally:
        if web_runner is not None:
            await web_runner.cleanup()
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await repo.close()


async def _start_web(settings: Settings, fleet: Fleet, stored: Installations | None) -> web.AppRunner | None:
    """The HTTPS side: the MCP OAuth callback (Spec 19) and the Slack install routes (Spec 22)."""
    log = logging.getLogger("knappy")
    web_app = web.Application()
    routes = False
    if settings.public_url and settings.secret_key:
        secret_key = settings.secret_key

        async def resolve(state: str):
            runtime = await fleet.get(state_workspace(secret_key, state, utc_now()))
            if runtime is None or runtime.mcp is None:
                return None
            return runtime.mcp, runtime.app_connected

        add_callback(web_app, resolve)
        routes = True
    if stored is not None:
        flow = InstallFlow(
            client_id=settings.slack_client_id or "", client_secret=settings.slack_client_secret or "",
            public_url=settings.public_url or "", secret_key=settings.secret_key or "",
        )

        async def installed(installation: Installation) -> None:
            await stored.save(installation)
            runtime = await fleet.install(installation)
            if installation.installer_user_id and runtime.direct is not None:
                try:
                    channel = await runtime.direct.open(installation.installer_user_id)
                    await runtime.direct.post(channel, WELCOME, [])
                except Exception:
                    log.exception("welcome DM failed team=%s", installation.team_id)

        add_install_routes(web_app, flow, installed)
        log.info("install link %s/slack/install", (settings.public_url or "").rstrip("/"))
        routes = True
    if not routes:
        log.info("web off: KNAPPY_PUBLIC_URL or KNAPPY_SECRET_KEY is not set")
        return None
    return await serve(web_app, settings.callback_port)


WELCOME = (
    "Hi, I'm Knappy. Thanks for installing me. Message me here to ask questions, keep track of commitments, "
    "or draft messages for you to approve. I'll send you a short brief each morning."
)


def main() -> None:
    configure_logging()
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
