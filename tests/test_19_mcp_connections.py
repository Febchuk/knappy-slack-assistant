"""Spec 19: connecting users to MCP servers, against an in-process OAuth server and MCP server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import types as mcp_types
from mcp.server.mcpserver import Context, MCPServer

from fakes import FakeClock
from knappy.config import ConfigError
from knappy.db.repository import SqliteRepository
from knappy.mcp.callback import start_callback
from knappy.mcp.hub import McpHub, McpResult, NotConnected
from knappy.mcp.servers import ServerConfig, parse_servers

WORKSPACE = "T_TEST"
STATIC_ID, STATIC_SECRET = "static-client", "static-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class FakeOAuth:
    """An authorization server with metadata, DCR, authorize (auto-consents as `as_user`), and token endpoints."""

    as_user: str = "U_A"
    expires_in: int = 3600
    refuse_refresh: bool = False
    dcr: bool = True
    registrations: int = 0
    refreshes: int = 0
    authorize_params: list[dict[str, str]] = field(default_factory=list)
    token_forms: list[dict[str, str]] = field(default_factory=list)
    access: dict[str, str] = field(default_factory=dict)
    _codes: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    _refresh: dict[str, str] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    base: str = ""

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/.well-known/oauth-authorization-server", self.metadata)
        app.router.add_post("/register", self.register)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/token", self.token)
        return app

    async def metadata(self, request: web.Request) -> web.Response:
        meta = {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.dcr:
            meta["registration_endpoint"] = f"{self.base}/register"
        return web.json_response(meta)

    async def register(self, request: web.Request) -> web.Response:
        self.registrations += 1
        body = await request.json()
        assert body["token_endpoint_auth_method"] == "none"
        return web.json_response({"client_id": f"dcr-{self.registrations}", **body}, status=201)

    async def authorize(self, request: web.Request) -> web.Response:
        params = dict(request.query)
        self.authorize_params.append(params)
        code = f"code-{next(self._ids)}"
        self._codes[code] = (self.as_user, params["code_challenge"], params["redirect_uri"])
        location = f"{params['redirect_uri']}?{urlencode({'code': code, 'state': params['state']})}"
        raise web.HTTPFound(location)

    def _issue(self, user: str) -> dict[str, Any]:
        n = next(self._ids)
        access, refresh = f"at-{user}-{n}", f"rt-{user}-{n}"
        self.access[access] = user
        self._refresh[refresh] = user
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": self.expires_in}

    async def token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_forms.append(form)
        if form["client_id"] == STATIC_ID and form.get("client_secret") != STATIC_SECRET:
            return web.json_response({"error": "invalid_client"}, status=401)
        if form["grant_type"] == "authorization_code":
            user, challenge, redirect = self._codes.pop(form["code"])
            digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
            if digest.decode() != challenge or redirect != form["redirect_uri"]:
                return web.json_response({"error": "invalid_grant"}, status=400)
            return web.json_response(self._issue(user))
        self.refreshes += 1
        user = self._refresh.pop(form["refresh_token"], None)
        if self.refuse_refresh or user is None:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(self._issue(user))


def fake_mcp(oauth: FakeOAuth, extra_tokens: set[str]) -> Any:
    server = MCPServer("fake")

    @server.tool(annotations=mcp_types.ToolAnnotations(read_only_hint=True))
    def whoami(ctx: Context) -> str:
        """Echo the bearer token the request carried."""
        return (ctx.headers or {}).get("authorization", "")

    @server.tool()
    def send_note(body: str) -> str:
        return f"sent {body}"

    @server.tool(annotations=mcp_types.ToolAnnotations(read_only_hint=True))
    def secret_admin() -> str:
        return "hidden"

    inner = server.streamable_http_app(stateless_http=True, json_response=True)

    async def app(scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/.well-known/oauth-protected-resource"):
            host = dict(scope["headers"])[b"host"].decode()
            body = f'{{"resource": "http://{host}/mcp", "authorization_servers": ["{oauth.base}"]}}'.encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] == "http":
            token = dict(scope["headers"]).get(b"authorization", b"").decode().removeprefix("Bearer ")
            if token not in oauth.access and token not in extra_tokens:
                await send({"type": "http.response.start", "status": 401, "headers": [(b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await inner(scope, receive, send)

    return app


@dataclass
class World:
    repo: SqliteRepository
    oauth: FakeOAuth
    mcp_url: str
    clock: FakeClock
    extra_tokens: set[str]
    connected: list[tuple[str, str]] = field(default_factory=list)
    _runners: list[Any] = field(default_factory=list)

    def entry(self, name: str = "fake", **fields: Any) -> dict[str, Any]:
        return {"name": name, "title": name.title(), "url": self.mcp_url, "auth": "oauth_dcr", "scopes": ["notes:read"], **fields}

    async def hub(self, *entries: dict[str, Any], env: dict[str, str] | None = None) -> McpHub:
        port = free_port()
        hub = McpHub(
            self.repo, WORKSPACE, parse_servers({"server": list(entries) or [self.entry()]}),
            public_url=f"http://127.0.0.1:{port}", secret_key="test-secret", env=env or {}, clock=self.clock,
        )

        async def on_connected(user_id: str, auth_group: str) -> None:
            self.connected.append((user_id, auth_group))

        self._runners.append(await start_callback(hub, on_connected, port, host="127.0.0.1"))
        return hub

    async def connect(self, hub: McpHub, user: str, server: str = "fake") -> httpx.Response:
        url = await hub.connect_url(user, server)
        assert url is not None
        self.oauth.as_user = user
        async with httpx.AsyncClient(follow_redirects=True) as browser:
            return await browser.get(url)

    async def close(self) -> None:
        for runner in self._runners:
            await runner.cleanup()


@pytest.fixture
async def world(repo: SqliteRepository) -> AsyncIterator[World]:
    oauth = FakeOAuth()
    oauth_server = TestServer(oauth.app(), host="127.0.0.1")
    await oauth_server.start_server()
    oauth.base = str(oauth_server.make_url("")).rstrip("/")
    extra: set[str] = set()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    mcp_server = uvicorn.Server(uvicorn.Config(fake_mcp(oauth, extra), log_level="warning", lifespan="on"))
    task = asyncio.create_task(mcp_server.serve(sockets=[sock]))
    while not mcp_server.started:
        await asyncio.sleep(0.01)
    clock = FakeClock(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    built = World(repo, oauth, f"http://127.0.0.1:{port}/mcp", clock, extra)
    yield built
    await built.close()
    mcp_server.should_exit = True
    await task
    await oauth_server.close()


async def whoami(hub: McpHub, user: str) -> McpResult | NotConnected:
    return await hub.call(user, "fake", "whoami", {})


async def test_dcr_runs_once_across_two_connects(world: World) -> None:
    hub = await world.hub()
    first = await world.connect(hub, "U_A")
    second = await world.connect(hub, "U_B")
    assert first.status_code == second.status_code == 200
    assert "Connected" in first.text
    assert world.oauth.registrations == 1
    assert world.connected == [("U_A", "fake"), ("U_B", "fake")]
    assert await hub.status("U_A") == {"fake": "connected"}
    params = world.oauth.authorize_params[0]
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == world.mcp_url
    assert params["scope"] == "notes:read"


async def test_forged_and_expired_state_are_rejected(world: World) -> None:
    hub = await world.hub()
    url = await hub.connect_url("U_A", "fake")
    params = parse_qs(urlsplit(url or "").query)
    callback = hub.redirect_uri
    async with httpx.AsyncClient() as browser:
        forged = await browser.get(callback, params={"code": "code-x", "state": params["state"][0][:-4] + "AAAA"})
        world.clock.advance(minutes=11)
        expired = await browser.get(callback, params={"code": "code-x", "state": params["state"][0]})
        refused = await browser.get(callback, params={"error": "access_denied", "state": params["state"][0]})
    assert (forged.status_code, expired.status_code, refused.status_code) == (400, 400, 400)
    assert world.oauth.token_forms == []
    assert world.connected == []
    assert await hub.status("U_A") == {"fake": "not_connected"}


async def test_tokens_are_encrypted_at_rest(world: World) -> None:
    hub = await world.hub()
    await world.connect(hub, "U_A")
    cursor = await world.repo.connection.execute("SELECT * FROM mcp_connections")
    stored = repr([tuple(row) for row in await cursor.fetchall()])
    issued = [token for token in world.oauth.access] + [f"rt-{token.removeprefix('at-')}" for token in world.oauth.access]
    assert issued and all(token not in stored for token in issued)
    result = await whoami(hub, "U_A")
    assert isinstance(result, McpResult) and result.text == f"Bearer {issued[0]}"


async def test_refreshes_when_under_five_minutes_remain(world: World) -> None:
    hub = await world.hub()
    await world.connect(hub, "U_A")
    world.clock.advance(minutes=54)
    first = await whoami(hub, "U_A")
    assert world.oauth.refreshes == 0
    world.clock.advance(minutes=2)
    second = await whoami(hub, "U_A")
    assert world.oauth.refreshes == 1
    assert isinstance(first, McpResult) and isinstance(second, McpResult)
    assert first.text != second.text and second.text.startswith("Bearer at-U_A-")
    assert await hub.status("U_A") == {"fake": "connected"}


async def test_failed_refresh_needs_reauth(world: World) -> None:
    hub = await world.hub()
    await world.connect(hub, "U_A")
    world.oauth.refuse_refresh = True
    world.clock.advance(minutes=56)
    result = await whoami(hub, "U_A")
    assert isinstance(result, NotConnected)
    assert result.status == "needs_reauth"
    assert result.connect_url and result.connect_url.startswith(f"{world.oauth.base}/authorize?")
    assert result.for_model()["error"] == "not_connected"
    assert await hub.status("U_A") == {"fake": "needs_reauth"}
    assert await hub.tools("U_A") == []


async def test_server_rejecting_the_token_needs_reauth(world: World) -> None:
    hub = await world.hub()
    await world.connect(hub, "U_A")
    world.oauth.access.clear()
    result = await whoami(hub, "U_A")
    assert isinstance(result, NotConnected) and result.status == "needs_reauth"
    assert await hub.status("U_A") == {"fake": "needs_reauth"}


async def test_each_user_calls_with_their_own_token(world: World) -> None:
    hub = await world.hub()
    await world.connect(hub, "U_A")
    await world.connect(hub, "U_B")
    a, b = await asyncio.gather(whoami(hub, "U_A"), whoami(hub, "U_B"))
    assert isinstance(a, McpResult) and isinstance(b, McpResult)
    assert world.oauth.access[a.text.removeprefix("Bearer ")] == "U_A"
    assert world.oauth.access[b.text.removeprefix("Bearer ")] == "U_B"
    stranger = await whoami(hub, "U_C")
    assert isinstance(stranger, NotConnected) and stranger.status == "not_connected"


async def test_tools_are_classified_filtered_and_cached(world: World) -> None:
    hub = await world.hub(world.entry(tools={"deny": ["secret_*"]}))
    assert await hub.tools("U_A") == []
    await world.connect(hub, "U_A")
    tools = {tool.name: tool for tool in await hub.tools("U_A")}
    assert {name: tool.access for name, tool in tools.items()} == {"whoami": "read", "send_note": "write"}
    assert tools["send_note"].input_schema["properties"]["body"]["type"] == "string"
    sent = await hub.call("U_A", "fake", "send_note", {"body": "hi"})
    assert isinstance(sent, McpResult) and sent.text == "sent hi" and not sent.is_error
    with pytest.raises(ValueError):
        await hub.call("U_A", "fake", "secret_admin", {})
    world.oauth.access.clear()
    assert {tool.name for tool in await hub.tools("U_A")} == {"whoami", "send_note"}


def tool(name: str, read_only: bool | None) -> mcp_types.Tool:
    annotations = None if read_only is None else mcp_types.ToolAnnotations(read_only_hint=read_only)
    return mcp_types.Tool(name=name, input_schema={"type": "object"}, annotations=annotations)


@pytest.mark.parametrize(
    ("read", "write", "hint", "expected"),
    [
        (["get_*"], [], None, "read"),
        (["get_*"], ["get_*"], False, "read"),
        ([], [], True, "read"),
        ([], ["get_*"], True, "write"),
        ([], [], False, "write"),
        ([], [], None, "write"),
        (["list_*"], [], None, "write"),
    ],
)
async def test_classification_table(world: World, read: list[str], write: list[str], hint: bool | None, expected: str) -> None:
    hub = McpHub(
        world.repo, WORKSPACE, parse_servers({"server": [world.entry(read=read, write=write)]}),
        public_url="http://127.0.0.1:1", secret_key="k",
    )
    assert hub.classify("fake", tool("get_ticket", hint)) == expected


async def test_static_group_shares_one_consent(world: World) -> None:
    world.oauth.dcr = False
    group = {"auth": "oauth_static", "auth_group": "suite", "client_id_env": "SUITE_ID", "client_secret_env": "SUITE_SECRET"}
    entries = (world.entry("mail", **group), world.entry("cal", **group))
    unavailable = await world.hub(*entries)
    assert await unavailable.status("U_A") == {"mail": "unavailable", "cal": "unavailable"}
    assert await unavailable.connect_url("U_A", "mail") is None
    hub = await world.hub(*entries, env={"SUITE_ID": STATIC_ID, "SUITE_SECRET": STATIC_SECRET})
    response = await world.connect(hub, "U_A", "cal")
    assert response.status_code == 200
    params = world.oauth.authorize_params[-1]
    assert (params["access_type"], params["prompt"], params["client_id"]) == ("offline", "consent", STATIC_ID)
    assert world.oauth.token_forms[-1]["client_secret"] == STATIC_SECRET
    assert world.oauth.registrations == 0
    assert await hub.status("U_A") == {"mail": "connected", "cal": "connected"}
    assert world.connected == [("U_A", "suite")]
    result = await hub.call("U_A", "mail", "whoami", {})
    assert isinstance(result, McpResult) and result.text.startswith("Bearer at-U_A-")


async def test_service_and_api_key_modes(world: World) -> None:
    world.extra_tokens.update({"svc-token", "key-A"})
    hub = await world.hub(
        world.entry("svc", auth="service", token_env="SVC_TOKEN"),
        world.entry("keyed", auth="api_key"),
        env={"SVC_TOKEN": "svc-token"},
    )
    assert await hub.status("U_A") == {"svc": "connected", "keyed": "not_connected"}
    service = await hub.call("U_B", "svc", "whoami", {})
    assert isinstance(service, McpResult) and service.text == "Bearer svc-token"
    missing = await hub.call("U_A", "keyed", "whoami", {})
    assert isinstance(missing, NotConnected) and missing.connect_url is None
    await hub.save_api_key("U_A", "keyed", "key-A")
    keyed = await hub.call("U_A", "keyed", "whoami", {})
    assert isinstance(keyed, McpResult) and keyed.text == "Bearer key-A"
    assert isinstance(await hub.call("U_B", "keyed", "whoami", {}), NotConnected)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"name": "Bad Name", "title": "x", "url": "https://x", "auth": "oauth_dcr"}, "'Bad Name'"),
        ({"name": "a__b", "title": "x", "url": "https://x", "auth": "oauth_dcr"}, "'a__b'"),
        ({"name": "google", "title": "x", "url": "https://x", "auth": "oauth_static"}, "'google': entry: Value error, oauth_static"),
        ({"name": "svc", "title": "x", "url": "https://x", "auth": "service"}, "'svc'"),
        ({"name": "odd", "title": "x", "url": "https://x", "auth": "magic"}, "'odd': auth"),
        ({"name": "extra", "title": "x", "url": "https://x", "auth": "oauth_dcr", "typo": 1}, "'extra': typo"),
    ],
)
def test_invalid_entries_name_themselves(entry: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message.replace("(", r"\(")):
        parse_servers({"server": [entry]})


def test_registry_rules() -> None:
    base = {"title": "x", "url": "https://x", "auth": "oauth_dcr"}
    with pytest.raises(ConfigError, match="'one': duplicate"):
        parse_servers({"server": [{"name": "one", **base}, {"name": "one", **base}]})
    with pytest.raises(ConfigError, match="'two': auth_group 'g'"):
        parse_servers({"server": [{"name": "one", "auth_group": "g", **base}, {"name": "two", "auth_group": "g", **base, "auth": "api_key"}]})
    (server,) = parse_servers({"server": [{"name": "solo", **base, "body_field": {"send_*": "body"}}]})
    assert isinstance(server, ServerConfig) and server.auth_group == "solo"
    assert server.body_field_for("send_note") == "body" and server.body_field_for("whoami") is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_mcp_starts_only_when_configured(monkeypatch: pytest.MonkeyPatch, tmp_path, enabled: bool) -> None:
    import knappy.main as main_module
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient

    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test", "SLACK_SIGNING_SECRET": "secret",
        "GEMINI_API_KEY": "test-key", "KNAPPY_DATABASE_URL": f"sqlite:///{tmp_path / 'knappy.db'}",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("KNAPPY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("KNAPPY_SECRET_KEY", raising=False)
    if enabled:
        monkeypatch.setenv("KNAPPY_PUBLIC_URL", "https://knappy.example")
        monkeypatch.setenv("KNAPPY_SECRET_KEY", "test-secret")
        monkeypatch.setenv("KNAPPY_CALLBACK_PORT", "9123")
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)

    async def start_async(self):
        await self.client.close()

    async def auth_test(self, *args, **kwargs):
        return {"team_id": "T_SERVE"}

    async def idle(engine):
        return None

    started: list[tuple[Any, int]] = []

    class Runner:
        async def cleanup(self) -> None:
            started.append(("cleaned", 0))

    async def fake_start(hub, on_connected, port):
        started.append((hub, port))
        return Runner()

    built: list = []
    real_runtime = main_module.KnappyRuntime

    def capture(*args, **kwargs):
        built.append(real_runtime(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(AsyncSocketModeHandler, "start_async", start_async)
    monkeypatch.setattr(AsyncWebClient, "auth_test", auth_test)
    monkeypatch.setattr(main_module, "_memory_loop", idle)
    monkeypatch.setattr(main_module, "start_callback", fake_start)
    monkeypatch.setattr(main_module, "KnappyRuntime", capture)
    await main_module._serve()
    runtime = built[0]
    if enabled:
        assert isinstance(runtime.mcp, McpHub)
        assert runtime.mcp.redirect_uri == "https://knappy.example/oauth/callback"
        assert started == [(runtime.mcp, 9123), ("cleaned", 0)]
    else:
        assert runtime.mcp is None
        assert started == []
