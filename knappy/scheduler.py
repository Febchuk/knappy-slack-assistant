"""Heartbeat entrypoint: python -m knappy.scheduler --run-now"""

from __future__ import annotations

import argparse
import asyncio

from knappy.db.repository import SqliteRepository
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.runtime import heuristic_triage


def sqlite_path(database_url: str) -> str:
    if database_url in {"sqlite:///:memory:", ":memory:"}:
        return ":memory:"
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        return database_url[len(prefix) :]
    return database_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Knappy's proactive heartbeat")
    parser.add_argument("--run-now", action="store_true")
    parser.add_argument("--database", default="sqlite:///knappy.db")
    parser.add_argument("--workspace", default="default_ws")
    parser.add_argument("--user", default="U_OWNER")
    return parser


async def run_now(database: str, workspace_id: str, user_id: str) -> dict[str, int]:
    repo = SqliteRepository(sqlite_path(database))
    await repo.connect()
    await repo.init_schema()
    await repo.upsert_workspace(workspace_id, "Knappy", "local")
    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(heuristic_triage),
        workspace_id=workspace_id,
        user_id=user_id,
    )
    try:
        return await engine.run_tick(include_cadence=True, deliver_digest=True)
    finally:
        await repo.close()


def main(argv: list[str] | None = None) -> int:
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
