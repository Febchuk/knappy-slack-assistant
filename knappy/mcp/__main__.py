"""python -m knappy.mcp: set up MCP servers.

`probe <url>` prints a mcp_servers.toml entry from a server's live OAuth metadata (Spec 19 §7).
`tools <server> --user <id>` lists a server's tools through a stored connection, to finish the entry (Spec 21 §3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import types as mcp_types

from knappy.agent.tools import app_tool_problem
from knappy.config import load_dotenv
from knappy.db.factory import open_repository
from knappy.db.repository import SqliteRepository
from knappy.mcp.auth import HTTP_TIMEOUT_S, AuthServer, DiscoveryError, discover
from knappy.mcp.hub import McpHub, NotConnected
from knappy.mcp.servers import DEFAULT_PATH, ServerConfig, classify, load_servers

GENERIC_LABELS = {"mcp", "api", "www", "app", "googleapis"}


def _labels(url: str) -> list[str]:
    return (urlsplit(url).hostname or "").lower().split(".")[:-1]


def server_name(url: str) -> str:
    label = next((label for label in _labels(url) if label not in GENERIC_LABELS), "server")
    label = re.sub(r"[^a-z0-9]+", "_", label.removesuffix("mcp")).strip("_")
    return label if label and label[0].isalpha() else f"server_{label}"


def entry(url: str, meta: AuthServer) -> str:
    name = server_name(url)
    lines = [
        "[[server]]",
        f"name = {json.dumps(name)}",
        f"title = {json.dumps(meta.resource_name or name.replace('_', ' ').title())}",
        f"url = {json.dumps(url)}",
    ]
    if meta.registration_endpoint:
        lines.append('auth = "oauth_dcr"')
    else:
        group = _labels(meta.issuer)[-1] if _labels(meta.issuer) else name
        env = group.upper()
        lines += [
            "# No dynamic client registration: create an OAuth client and put its id and secret in .env.",
            'auth = "oauth_static"',
            f"auth_group = {json.dumps(group)}",
            f'client_id_env = "{env}_OAUTH_CLIENT_ID"',
            f'client_secret_env = "{env}_OAUTH_CLIENT_SECRET"',
        ]
    if meta.scopes:
        lines.append("# Every scope the server advertises. Trim to what Knappy needs.")
        lines.append(f"scopes = {json.dumps(list(meta.scopes))}")
    if not meta.refreshes:
        lines.append("# No refresh_token grant advertised: users reconnect when the token expires.")
    lines += [
        '# tools = { allow = ["*"], deny = [] }',
        "# read = []        # tool globs that only read, whatever their annotations say",
        "# write = []       # tool globs that write even though they claim readOnlyHint",
        '# body_field = { "send_*" = "body" }',
    ]
    return "\n".join(lines) + "\n"


async def probe(url: str, http: httpx.AsyncClient | None = None) -> str:
    if http is not None:
        return entry(url, await discover(url, http))
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        return entry(url, await discover(url, client))


READ_VERBS = {"get", "list", "search", "find", "read", "query", "describe", "fetch", "lookup", "show", "count"}
WRITE_VERBS = {
    "create", "update", "delete", "send", "post", "reply", "add", "remove", "set", "close", "assign", "archive",
    "cancel", "execute", "write", "edit", "move", "merge", "trash", "submit", "upload", "invite", "run",
}
BODY_ARGS = ("body", "text", "message", "content", "comment", "note", "reply")


def _words(tool: str) -> list[str]:
    return [word.lower() for word in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", tool)]


def _args(schema: dict[str, Any]) -> str:
    required = set(schema.get("required") or [])
    names = [f"{name}*" if name in required else name for name in schema.get("properties") or {}]
    return ", ".join(names) or "-"


def _hint(tool: mcp_types.Tool) -> bool | None:
    return tool.annotations.read_only_hint if tool.annotations else None


def _toml_list(names: list[str]) -> str:
    return "[" + ", ".join(json.dumps(name) for name in names) + "]"


def _toml_table(pairs: dict[str, str]) -> str:
    inner = ", ".join(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in pairs.items())
    return f"{{ {inner} }}" if inner else "{}"


def tools_table(config: ServerConfig, listed: list[mcp_types.Tool]) -> str:
    """One row per listed tool, then suggested read, write and body_field lines for the entry (Spec 21 §3)."""
    rows = [("tool", "hint", "access", "gemini", "args")]
    reads, writes, bodies = list(config.read), list(config.write), dict(config.body_field)
    for tool in listed:
        hint = _hint(tool)
        exposed = config.exposes(tool.name)
        access = classify(config, tool.name, hint) if exposed else "denied"
        problem = app_tool_problem(f"{config.name}__{tool.name}", tool.input_schema)
        rows.append((tool.name, "-" if hint is None else str(hint).lower(), access, problem or "ok", _args(tool.input_schema)))
        if not exposed:
            continue
        words = _words(tool.name)
        if access == "write" and words and words[0] in READ_VERBS and not WRITE_VERBS & set(words):
            reads.append(tool.name)
        if access == "read" and hint is True and WRITE_VERBS & set(words) and "readonly" not in words:
            writes.append(tool.name)
        properties = tool.input_schema.get("properties") or {}
        body = next((arg for arg in BODY_ARGS if (properties.get(arg) or {}).get("type") == "string"), None)
        if access == "write" and body and config.body_field_for(tool.name) is None:
            bodies[tool.name] = body
    widths = [max(len(row[col]) for row in rows) for col in range(4)]
    lines = ["  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row[:4])) + "  " + row[4] for row in rows]
    lines += [
        "",
        f"# Suggested for the {config.name!r} entry. Check each tool's description before pasting.",
        "# read: named like reads but not annotated readOnlyHint. write: annotated read-only but named like a change.",
        f"read = {_toml_list(reads)}",
        f"write = {_toml_list(writes)}",
        f"body_field = {_toml_table(bodies)}",
    ]
    return "\n".join(lines) + "\n"


async def _workspace(
    repo: SqliteRepository, owner: str, auth_group: str, given: str | None, fallback: str | None
) -> str | None:
    """--workspace, else the one workspace holding this connection, else KNAPPY_WORKSPACE_ID.

    The running Knappy uses Slack's team id, which KNAPPY_WORKSPACE_ID only stands in for, so the stored row wins.
    """
    if given:
        return given
    cursor = await repo.connection.execute(
        "SELECT DISTINCT workspace_id FROM mcp_connections WHERE owner_user_id = ? AND auth_group = ?", (owner, auth_group)
    )
    found = [row[0] for row in await cursor.fetchall()]
    return found[0] if len(found) == 1 else (fallback if not found else None)


async def tools_main(
    server: str, user: str, *, workspace: str | None = None, env: Mapping[str, str], config: Path = DEFAULT_PATH
) -> int:
    servers = {entry.name: entry for entry in load_servers(config)}
    if server not in servers:
        print(f"no server {server!r} in {config.name}; configured: {', '.join(servers) or 'none'}", file=sys.stderr)
        return 2
    public_url, secret = env.get("KNAPPY_PUBLIC_URL"), env.get("KNAPPY_SECRET_KEY")
    if not (public_url and secret):
        print("KNAPPY_PUBLIC_URL and KNAPPY_SECRET_KEY must be set, as for the running Knappy", file=sys.stderr)
        return 2
    repo = await open_repository(env.get("KNAPPY_DATABASE_URL") or "sqlite:///knappy.db")
    try:
        await repo.init_schema()
        owner_workspace = await _workspace(
            repo, user, servers[server].auth_group, workspace, env.get("KNAPPY_WORKSPACE_ID")
        )
        if owner_workspace is None:
            print(f"{user} has no single stored {servers[server].auth_group!r} connection; pass --workspace", file=sys.stderr)
            return 1
        hub = McpHub(repo, owner_workspace, tuple(servers.values()), public_url=public_url, secret_key=secret, env=env)
        listed = await hub.list_server(user, server)
    finally:
        await repo.close()
    if isinstance(listed, NotConnected):
        fix = (
            f"Set {', '.join(servers[server].required_env())} first."
            if listed.status == "unavailable"
            else "Connect from a Slack DM first."
        )
        print(f"{user} is {listed.status} for {listed.title}. {fix}", file=sys.stderr)
        return 1
    print(f"{server}: {len(listed)} tools listed for {user} in {owner_workspace}\n")
    print(tools_table(servers[server], listed), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m knappy.mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    probe_cmd = commands.add_parser("probe", help="print a mcp_servers.toml entry for a remote MCP server")
    probe_cmd.add_argument("url")
    tools_cmd = commands.add_parser("tools", help="list a server's tools through a user's stored connection")
    tools_cmd.add_argument("server")
    tools_cmd.add_argument("--user", required=True, help="the Slack user id whose connection to use")
    tools_cmd.add_argument("--workspace", help="the Slack team id; defaults to KNAPPY_WORKSPACE_ID or the only match")
    tools_cmd.add_argument("--config", type=Path, default=DEFAULT_PATH, help="mcp_servers.toml to read")
    args = parser.parse_args(argv)
    if args.command == "tools":
        load_dotenv()
        return asyncio.run(tools_main(args.server, args.user, workspace=args.workspace, env=os.environ, config=args.config))
    try:
        print(asyncio.run(probe(args.url)), end="")
    except DiscoveryError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
