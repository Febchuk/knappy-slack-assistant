"""Spec 19 §7: the probe turns recorded live discovery metadata into valid mcp_servers.toml entries."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest

from knappy.mcp.__main__ import probe
from knappy.mcp.servers import parse_servers

RECORDED = json.loads((Path(__file__).parent / "fixtures" / "mcp_discovery.json").read_text())


def recorded(request: httpx.Request) -> httpx.Response:
    body = RECORDED.get(str(request.url))
    return httpx.Response(200, json=body) if body is not None else httpx.Response(404, text="not found")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://mcp.lorikeetcx.ai", {"name": "lorikeetcx", "auth": "oauth_dcr", "auth_group": "lorikeetcx"}),
        ("https://api.grain.com/_/mcp", {"name": "grain", "title": "Grain", "auth": "oauth_dcr"}),
        (
            "https://gmailmcp.googleapis.com/mcp",
            {"name": "gmail", "auth": "oauth_static", "auth_group": "google", "client_id_env": "GOOGLE_OAUTH_CLIENT_ID"},
        ),
    ],
)
async def test_probe_prints_a_valid_entry(url: str, expected: dict[str, str]) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorded)) as http:
        text = await probe(url, http)
    (server,) = parse_servers(tomllib.loads(text))
    assert {key: getattr(server, key) for key in expected} == expected
    assert server.url == url
    refresh_note = "No refresh_token grant advertised" in text
    assert refresh_note == (server.name == "grain")


async def test_lorikeet_scopes_come_from_its_metadata() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorded)) as http:
        (server,) = parse_servers(tomllib.loads(await probe("https://mcp.lorikeetcx.ai", http)))
    assert "tickets:read" in server.scopes and "tickets:write" in server.scopes
