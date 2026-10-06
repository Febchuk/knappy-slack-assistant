"""Recompile memory from retained turns: python -m knappy.memory rebuild --owner U123 [--since 2026-09-01]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import date, datetime, time, timezone

from knappy.config import DEFAULT_MODEL_AGENT, DEFAULT_MODEL_LIGHT, ConfigError, load_dotenv
from knappy.db.factory import open_repository
from knappy.llm.client import GeminiClient, ModelIds
from knappy.memory.engine import MemoryConfig, MemoryEngine
from knappy.memory.store import MemoryStore
from knappy.runtime import usage_recorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m knappy.memory", description="Knappy memory maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    rebuild = commands.add_parser("rebuild", help="Wipe compiled memory and rerun the reconciler over retained turns")
    rebuild.add_argument("--owner", required=True, help="Slack user id whose memory to rebuild")
    rebuild.add_argument("--since", type=date.fromisoformat, help="Only rebuild from this date (YYYY-MM-DD)")
    rebuild.add_argument("--database", default=os.environ.get("KNAPPY_DATABASE_URL", "sqlite:///knappy.db"))
    rebuild.add_argument("--workspace", default=os.environ.get("KNAPPY_WORKSPACE_ID", "default_ws"))
    return parser


async def rebuild(database: str, workspace_id: str, owner: str, since: date | None) -> None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ConfigError("Missing required environment variables: GEMINI_API_KEY")
    repo = await open_repository(database)
    await repo.init_schema()
    try:
        model = GeminiClient(
            key,
            ModelIds(
                agent=os.environ.get("KNAPPY_MODEL_AGENT") or DEFAULT_MODEL_AGENT,
                light=os.environ.get("KNAPPY_MODEL_LIGHT") or DEFAULT_MODEL_LIGHT,
            ),
            on_usage=usage_recorder(repo, workspace_id),
        )
        config = MemoryConfig(
            admission_threshold=float(os.environ.get("KNAPPY_ADMISSION_THRESHOLD") or 0.4),
            raw_retention_days=int(os.environ.get("KNAPPY_RAW_RETENTION_DAYS") or 90),
        )
        start = datetime.combine(since, time(0), timezone.utc) if since else None
        await MemoryEngine(MemoryStore(repo, workspace_id), model, config).rebuild(owner, start)
    finally:
        await repo.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    asyncio.run(rebuild(args.database, args.workspace, args.owner, args.since))
    print(f"Rebuilt memory for {args.owner}" + (f" since {args.since}" if args.since else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
