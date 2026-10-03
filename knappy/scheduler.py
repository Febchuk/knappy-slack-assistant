"""Heartbeat entrypoint: python -m knappy.scheduler --run-now"""

from __future__ import annotations

import argparse
import asyncio
import os

from knappy.config import DEFAULT_MODEL_AGENT, DEFAULT_MODEL_LIGHT, ConfigError, load_dotenv
from knappy.db.factory import open_repository, sqlite_path
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager, model_triage
from knappy.llm.client import GeminiClient, ModelIds
from knappy.runtime import usage_recorder
from knappy.slack.egress import build_say


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Knappy's proactive heartbeat")
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--database", default=os.environ.get("KNAPPY_DATABASE_URL", "sqlite:///knappy.db"))
    parser.add_argument("--workspace", default=os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws"))
    parser.add_argument("--user", default="U_OWNER")
    return parser


async def _slack_sender():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None, None
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=token)
    return build_say(client), client


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ConfigError("Missing required environment variables: GEMINI_API_KEY")
    return key


async def run_now(database: str, workspace_id: str, user_id: str) -> dict[str, int]:
    api_key = _gemini_key()
    repo = await open_repository(database)
    await repo.init_schema()
    await repo.upsert_workspace(workspace_id, "Knappy", "local")
    sender, client = await _slack_sender()
    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(model_triage(GeminiClient(
            api_key,
            ModelIds(
                agent=os.environ.get("KNAPPY_MODEL_AGENT") or DEFAULT_MODEL_AGENT,
                light=os.environ.get("KNAPPY_MODEL_LIGHT") or DEFAULT_MODEL_LIGHT,
            ),
            on_usage=usage_recorder(repo, workspace_id),
        ))),
        workspace_id=workspace_id,
        user_id=user_id,
        sender=sender,
    )
    try:
        return await engine.run_tick(include_cadence=True, deliver_digest=True)
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
    counts = asyncio.run(run_now(args.database, args.workspace, args.user))
    print(
        "Knappy heartbeat "
        f"scanned={counts['scanned']} immediate={counts['immediate']} queued={counts['queued']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
