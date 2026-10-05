"""Spec 19 §10: every entry in mcp_servers.toml validates, compiles its globs, and has the env its auth mode needs."""

from __future__ import annotations

import os
import re
from fnmatch import translate

import pytest

from knappy.config import load_dotenv
from knappy.mcp.servers import DEFAULT_PATH, ServerConfig, load_servers

load_dotenv()
SERVERS = load_servers(DEFAULT_PATH)


@pytest.mark.parametrize("server", SERVERS, ids=[server.name for server in SERVERS])
def test_entry_is_usable(server: ServerConfig) -> None:
    for glob in server.globs():
        re.compile(translate(glob))
    assert server.url.startswith("https://"), f"{server.name}: MCP servers are reached over HTTPS"
    missing = server.missing_env(os.environ)
    if missing:
        pytest.skip(f"{server.name}: {server.auth} needs {', '.join(missing)}")
