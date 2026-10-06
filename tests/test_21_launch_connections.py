"""Spec 21: the `tools` lever, the negative list cache, and the dangling-$ref guard."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import types as mcp_types

from knappy.agent.tools import app_tool_spec
from knappy.db.repository import SqliteRepository
from knappy.mcp import hub as hub_module
from knappy.mcp.__main__ import tools_main, tools_table
from knappy.mcp.hub import McpTool
from knappy.mcp.servers import parse_servers
from mcp_fakes import World


@pytest.fixture
async def file_world(world: World, tmp_path: Path) -> World:
    """The fake servers over a SQLite file, so the CLI can open the same database by URL."""
    repo = SqliteRepository(str(tmp_path / "knappy.db"))
    await repo.connect()
    await repo.init_schema()
    world.repo = repo
    yield world
    await repo.close()


def write_config(path: Path, world: World) -> Path:
    path.write_text(
        f'[[server]]\nname = "fake"\ntitle = "Fake"\nurl = "{world.mcp_url}"\nauth = "oauth_dcr"\n'
        'tools = { allow = ["*"], deny = ["secret_admin"] }\n'
    )
    return path


def table_rows(out: str) -> dict[str, list[str]]:
    """{tool: [hint, access, gemini, args]} from the table between the header row and the blank line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("tool "))
    end = lines.index("", start)
    return {line.split()[0]: line.split(None, 4)[1:] for line in lines[start + 1 : end]}


def cli_env(tmp_path: Path) -> dict[str, str]:
    return {
        "KNAPPY_DATABASE_URL": f"sqlite:///{tmp_path / 'knappy.db'}",
        "KNAPPY_PUBLIC_URL": "http://127.0.0.1:1",
        "KNAPPY_SECRET_KEY": "test-secret",
    }


async def test_tools_lists_a_connected_server(file_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path / "servers.toml", file_world)
    hub = await file_world.hub(file_world.entry())
    await file_world.connect(hub, "U_A")

    code = await tools_main("fake", "U_A", env=cli_env(tmp_path), config=config)

    out = capsys.readouterr().out
    rows = table_rows(out)
    assert code == 0
    assert rows["whoami"] == ["true", "read", "ok", "-"]
    assert rows["send_note"] == ["-", "write", "ok", "body*"]
    assert rows["create_ticket"] == ["false", "write", "ok", "subject*, body*, priority"]
    assert rows["secret_admin"] == ["true", "denied", "ok", "-"]
    assert 'body_field = { "send_note" = "body", "create_ticket" = "body" }' in out
    assert "at-U_A" not in out


async def test_tools_refuses_an_unconnected_user(file_world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path / "servers.toml", file_world)
    await file_world.hub(file_world.entry())

    code = await tools_main("fake", "U_NOBODY", workspace="T_TEST", env=cli_env(tmp_path), config=config)

    assert code == 1
    assert "not_connected" in capsys.readouterr().err


def tool(name: str, read_only: bool | None, **properties: str) -> mcp_types.Tool:
    annotations = None if read_only is None else mcp_types.ToolAnnotations(read_only_hint=read_only)
    schema = {"type": "object", "properties": {key: {"type": kind} for key, kind in properties.items()}}
    return mcp_types.Tool(name=name, input_schema=schema, annotations=annotations)


def test_tools_table_suggests_overrides() -> None:
    (config,) = parse_servers({"server": [{"name": "crm", "title": "CRM", "url": "https://crm.example", "auth": "oauth_dcr"}]})
    listed = [
        tool("get_ticket", None, ticket_id="string"),
        tool("delete_everything", True),
        tool("execute_sql_readonly", True, query="string"),
        tool("replyToTicket", False, ticket_id="string", message="string"),
        tool("x" * 70, True),
    ]

    out = tools_table(config, listed)

    assert 'read = ["get_ticket"]' in out
    assert 'write = ["delete_everything"]' in out
    assert 'body_field = { "replyToTicket" = "message" }' in out
    assert table_rows(out)["x" * 70][:3] == ["true", "read", "name"]


@pytest.fixture
async def flaky() -> dict[str, object]:
    """An MCP endpoint that fails (500) or hangs, counting every request."""
    state: dict[str, object] = {"hits": 0, "hang": False}

    async def handle(request: web.Request) -> web.Response:
        state["hits"] = int(state["hits"]) + 1
        if state["hang"]:
            await asyncio.sleep(30)
        return web.Response(status=500)

    app = web.Application()
    app.router.add_route("*", "/mcp", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    state["url"] = str(server.make_url("/mcp"))
    yield state
    await server.close()


def service(name: str, url: str, token_env: str) -> dict[str, str]:
    return {"name": name, "title": name.title(), "url": url, "auth": "service", "token_env": token_env}


async def test_failed_listing_is_cached_for_two_minutes(world: World, flaky: dict[str, object]) -> None:
    hub = await world.hub(service("down", str(flaky["url"]), "DOWN_TOKEN"), env={"DOWN_TOKEN": "t"})

    assert await hub.tools("U_A") == []
    tried = flaky["hits"]
    assert tried
    await hub.tools("U_A")
    assert flaky["hits"] == tried

    world.clock.advance(minutes=2, seconds=1)
    await hub.tools("U_A")
    assert flaky["hits"] > tried


async def test_slow_server_times_out_alone(world: World, flaky: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "LIST_TIMEOUT_S", 0.5)
    flaky["hang"] = True
    world.extra_tokens.add("svc")
    hub = await world.hub(
        service("slow", str(flaky["url"]), "SLOW_TOKEN"), service("fake", world.mcp_url, "FAKE_TOKEN"),
        env={"SLOW_TOKEN": "t", "FAKE_TOKEN": "svc"},
    )

    started = time.monotonic()
    tools = await hub.tools("U_A")
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert {t.name for t in tools if t.server == "fake"} >= {"whoami", "send_note"}
    assert not [t for t in tools if t.server == "slow"]
    hits = flaky["hits"]
    await hub.tools("U_A")
    assert flaky["hits"] == hits


def app_tool(schema: dict[str, object]) -> McpTool:
    return McpTool(server="crm", name="lookup", title=None, description="", input_schema=schema, access="read")


def test_dangling_refs_are_skipped(caplog: pytest.LogCaptureFixture) -> None:
    dangling = {"type": "object", "properties": {"a": {"$ref": "#/$defs/Missing"}}}
    external = {"type": "object", "properties": {"a": {"$ref": "https://example.com/schema.json"}}}
    resolved = {"type": "object", "$defs": {"N": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/N"}}}

    assert app_tool_spec(app_tool(dangling), "CRM") is None
    assert app_tool_spec(app_tool(external), "CRM") is None
    assert app_tool_spec(app_tool(resolved), "CRM") is not None
    assert caplog.text.count("reason=schema") == 2
