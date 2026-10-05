"""In-process fake OAuth and MCP servers shared by the Spec 19 and Spec 20 tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import types as mcp_types
from mcp.server.mcpserver import Context, MCPServer

from fakes import FakeClock
from knappy.db.repository import SqliteRepository
from knappy.mcp.callback import start_callback
from knappy.mcp.hub import McpHub
from knappy.mcp.servers import parse_servers

WORKSPACE = "T_TEST"
STATIC_ID, STATIC_SECRET = "static-client", "static-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(frozen=True)
class ToolCallRecord:
    tool: str
    authorization: str
    args: dict[str, Any]


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


def fake_mcp(oauth: FakeOAuth, extra_tokens: set[str], calls: list[ToolCallRecord]) -> Any:
    server = MCPServer("fake")

    def record(ctx: Context, tool: str, **args: Any) -> str:
        token = (ctx.headers or {}).get("authorization", "")
        calls.append(ToolCallRecord(tool, token, args))
        return token

    @server.tool(annotations=mcp_types.ToolAnnotations(read_only_hint=True))
    def whoami(ctx: Context) -> str:
        """Echo the bearer token the request carried."""
        return record(ctx, "whoami")

    @server.tool()
    def send_note(body: str, ctx: Context) -> str:
        record(ctx, "send_note", body=body)
        return f"sent {body}"

    @server.tool(title="Create ticket", annotations=mcp_types.ToolAnnotations(read_only_hint=False))
    def create_ticket(subject: str, body: str, ctx: Context, priority: Literal["low", "high"] = "low") -> str:
        """Open a support ticket."""
        record(ctx, "create_ticket", subject=subject, body=body, priority=priority)
        return f"ticket {subject}"

    @server.tool(annotations=mcp_types.ToolAnnotations(read_only_hint=False))
    def close_ticket(ticket_id: str, ctx: Context) -> str:
        record(ctx, "close_ticket", ticket_id=ticket_id)
        raise ValueError(f"ticket {ticket_id} is locked")

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
    calls: list[ToolCallRecord] = field(default_factory=list)
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
    calls: list[ToolCallRecord] = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    mcp_server = uvicorn.Server(uvicorn.Config(fake_mcp(oauth, extra, calls), log_level="warning", lifespan="on"))
    task = asyncio.create_task(mcp_server.serve(sockets=[sock]))
    while not mcp_server.started:
        await asyncio.sleep(0.01)
    clock = FakeClock(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    built = World(repo, oauth, f"http://127.0.0.1:{port}/mcp", clock, extra, calls)
    yield built
    await built.close()
    mcp_server.should_exit = True
    await task
    await oauth_server.close()
