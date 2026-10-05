"""python -m knappy.mcp probe <url>: print a mcp_servers.toml entry from a server's live OAuth metadata (Spec 19 §7)."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit

import httpx

from knappy.mcp.auth import HTTP_TIMEOUT_S, AuthServer, DiscoveryError, discover

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m knappy.mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    probe_cmd = commands.add_parser("probe", help="print a mcp_servers.toml entry for a remote MCP server")
    probe_cmd.add_argument("url")
    args = parser.parse_args(argv)
    try:
        print(asyncio.run(probe(args.url)), end="")
    except DiscoveryError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
