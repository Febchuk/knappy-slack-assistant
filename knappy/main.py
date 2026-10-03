"""Socket Mode entrypoint: python -m knappy.main"""

from __future__ import annotations

import asyncio
import logging
import os

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from knappy.config import Settings, load_dotenv
from knappy.db.factory import open_repository
from knappy.heartbeat.schedule import cadence_due
from knappy.llm.client import GeminiClient, ModelIds
from knappy.runtime import KnappyRuntime, usage_recorder
from knappy.slack.actions import register_actions
from knappy.slack.app import create_app
from knappy.slack.egress import build_say
from knappy.slack.executor import SlackActionExecutor

logger = logging.getLogger("knappy")
FALLBACK_TEXT = "I hit an error answering that."


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def dispatch_event(runtime, client, event) -> None:
    try:
        await runtime.handle_event(event)
    except Exception as exc:
        logger.exception("handle_event failed: %s: %s", type(exc).__name__, exc)
        channel = event.get("channel")
        if channel and client is not None:
            try:
                await client.chat_postMessage(channel=channel, text=FALLBACK_TEXT)
            except Exception:
                logger.exception("fallback post failed")
            else:
                logger.info("deliver postMessage channel=%s", channel)


async def _workspace_id(client) -> str:
    fallback = os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws")
    try:
        auth = await client.auth_test()
    except Exception:
        return fallback
    team_id = auth.get("team_id") if hasattr(auth, "get") else None
    return team_id or fallback


async def _heartbeat_loop(engine) -> None:
    from datetime import datetime

    last_digest = None
    while True:
        now = datetime.now()
        include = cadence_due(now, last_digest)
        if include:
            last_digest = now.date()
        try:
            await engine.run_tick(include_cadence=include, deliver_digest=include)
        except Exception as exc:
            print(f"Knappy heartbeat failed: {exc}")
        await asyncio.sleep(30 * 60)


async def _serve() -> None:
    load_dotenv()
    settings = Settings.from_env()
    repo = await open_repository(settings.database_url)
    await repo.init_schema()
    holder: dict[str, KnappyRuntime] = {}

    async def process(event):
        runtime = holder.get("runtime")
        if runtime is not None:
            await dispatch_event(runtime, app.client, event)

    app = create_app(settings, processor=process)
    workspace_id = await _workspace_id(app.client)
    await repo.upsert_workspace(workspace_id, "Knappy", settings.slack_bot_token)
    say = build_say(app.client)
    model = GeminiClient(
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
        executor=SlackActionExecutor(app.client),
        history=app.client,
    )
    holder["runtime"] = runtime
    register_actions(app, runtime)
    tick = asyncio.create_task(_heartbeat_loop(runtime.heartbeat))
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    print("⚡️ Knappy is connected via Socket Mode!")
    try:
        await handler.start_async()
    finally:
        tick.cancel()
        try:
            await tick
        except asyncio.CancelledError:
            pass
        await repo.close()


def main() -> None:
    configure_logging()
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
