"""Heartbeat entrypoint: python -m knappy.scheduler --run-now"""

from __future__ import annotations

import argparse
import asyncio
import os

from slack_sdk.web.async_client import AsyncWebClient

from knappy.config import DEFAULT_MODEL_AGENT, DEFAULT_MODEL_LIGHT, ConfigError, load_dotenv
from knappy.db.factory import open_repository
from knappy.heartbeat.engine import OwnerTick
from knappy.llm.client import GeminiClient, ModelIds
from knappy.runtime import KnappyRuntime, usage_recorder
from knappy.slack.egress import build_say
from knappy.slack.executor import SlackActionExecutor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one pass of Knappy's proactive heartbeat")
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--database", default=os.environ.get("KNAPPY_DATABASE_URL", "sqlite:///knappy.db"))
    parser.add_argument("--workspace", default=os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws"))
    return parser


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ConfigError("Missing required environment variables: GEMINI_API_KEY")
    return key


async def run_now(database: str, workspace_id: str) -> list[OwnerTick]:
    """One heartbeat pass for every owner, posting through Slack when SLACK_BOT_TOKEN is set."""
    api_key = _gemini_key()
    repo = await open_repository(database)
    await repo.init_schema()
    await repo.ensure_workspace(workspace_id, "Knappy")
    token = os.environ.get("SLACK_BOT_TOKEN")
    client = AsyncWebClient(token=token) if token else None
    model = GeminiClient(
        api_key,
        ModelIds(
            agent=os.environ.get("KNAPPY_MODEL_AGENT") or DEFAULT_MODEL_AGENT,
            light=os.environ.get("KNAPPY_MODEL_LIGHT") or DEFAULT_MODEL_LIGHT,
        ),
        on_usage=usage_recorder(repo, workspace_id),
    )
    say = build_say(client) if client is not None else None
    runtime = KnappyRuntime(
        repo, workspace_id=workspace_id, model=model, say=say, sender=say, slack=client,
        executor=SlackActionExecutor(client) if client is not None else None,
    )
    try:
        return await runtime.heartbeat.run_tick()
    finally:
        session = getattr(client, "session", None) if client is not None else None
        if session is not None and not getattr(session, "closed", True):
            await session.close()
        await repo.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if not args.run_now:
        build_parser().print_help()
        return 0
    ticks = asyncio.run(run_now(args.database, args.workspace))
    print("Knappy heartbeat " + (" ".join(f"{tick.owner}={tick.outcome}" for tick in ticks) or "no owners"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
