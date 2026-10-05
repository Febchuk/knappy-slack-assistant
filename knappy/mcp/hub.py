"""McpHub (Spec 19 §6): list and call a connected server's tools with the owner's own credential."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from mcp import Client
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from knappy.db.repository import SqliteRepository, utc_now
from knappy.mcp.auth import (
    ApiKeyAuth,
    AuthStrategy,
    DiscoveryError,
    InvalidState,
    OAuthAuth,
    ServiceAuth,
    Status,
    UnavailableAuth,
    open_state,
)
from knappy.mcp.servers import ServerConfig, classify
from knappy.mcp.store import McpStore, OAuthClient, derive_fernet

logger = logging.getLogger(__name__)

Access = Literal["read", "write"]
TOOLS_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class NotConnected:
    server: str
    title: str
    status: Status
    connect_url: str | None

    def for_model(self) -> dict[str, Any]:
        view = {"error": "not_connected", "server": self.server, "app": self.title, "status": self.status}
        return view | ({"connect_url": self.connect_url} if self.connect_url else {})


@dataclass(frozen=True)
class McpTool:
    server: str
    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    access: Access


@dataclass(frozen=True)
class McpResult:
    text: str
    is_error: bool
    structured: dict[str, Any] | None


class _Unauthorized(Exception):
    pass


def _text(content: list[mcp_types.ContentBlock]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.EmbeddedResource) and hasattr(block.resource, "text"):
            parts.append(block.resource.text)
        else:
            parts.append(f"[{block.type}]")
    return "\n".join(parts)


class McpHub:
    def __init__(
        self,
        repo: SqliteRepository,
        workspace_id: str,
        servers: tuple[ServerConfig, ...],
        *,
        public_url: str,
        secret_key: str,
        env: Mapping[str, str] = os.environ,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.workspace_id = workspace_id
        self.servers = {server.name: server for server in servers}
        self.clock = clock
        self.store = McpStore(repo, workspace_id, secret_key)
        self.redirect_uri = f"{public_url.rstrip('/')}/oauth/callback"
        self._state_key = derive_fernet(secret_key, "state")
        self._cache: dict[tuple[str, str], tuple[datetime, list[McpTool]]] = {}
        groups: dict[str, list[ServerConfig]] = {}
        for server in servers:
            groups.setdefault(server.auth_group, []).append(server)
        self.auth: dict[str, AuthStrategy] = {group: self._strategy(members, env) for group, members in groups.items()}

    def _strategy(self, members: list[ServerConfig], env: Mapping[str, str]) -> AuthStrategy:
        first = members[0]
        if first.missing_env(env):
            return UnavailableAuth()
        if first.auth == "service":
            return ServiceAuth(env[first.token_env or ""])
        if first.auth == "api_key":
            return ApiKeyAuth(self.store, first.auth_group)
        static = (
            OAuthClient(env[first.client_id_env or ""], env[first.client_secret_env or ""])
            if first.auth == "oauth_static"
            else None
        )
        return OAuthAuth(
            members, self.store, redirect_uri=self.redirect_uri, state_key=self._state_key, static_client=static,
            clock=self.clock,
        )

    def _server(self, name: str) -> ServerConfig:
        if name not in self.servers:
            raise ValueError(f"unknown MCP server {name!r}")
        return self.servers[name]

    @asynccontextmanager
    async def _session(self, server: ServerConfig, headers: dict[str, str]) -> AsyncIterator[Client]:
        # The SDK folds an HTTP 401 into a generic MCPError, so the status is caught on the way in.
        statuses: list[int] = []

        async def record(response: Any) -> None:
            statuses.append(response.status_code)

        http = create_mcp_http_client(headers=headers)
        http.event_hooks["response"].append(record)
        try:
            async with http, Client(streamable_http_client(server.url, http_client=http), cache=None) as client:
                yield client
        except Exception as exc:
            if 401 in statuses:
                raise _Unauthorized from exc
            raise

    def classify(self, server: str, tool: mcp_types.Tool) -> Access:
        hint = tool.annotations.read_only_hint if tool.annotations else None
        return classify(self._server(server), tool.name, hint)

    async def connect_url(self, owner: str, server: str) -> str | None:
        try:
            return await self.auth[self._server(server).auth_group].connect_url(owner)
        except (DiscoveryError, httpx.HTTPError) as exc:
            logger.warning("mcp connect url failed server=%s error=%s", server, exc)
            return None

    async def status(self, owner: str) -> dict[str, Status]:
        return {name: await self.auth[server.auth_group].status(owner) for name, server in self.servers.items()}

    async def _not_connected(self, owner: str, server: ServerConfig) -> NotConnected:
        strategy = self.auth[server.auth_group]
        status = await strategy.status(owner)
        if status == "connected":
            status = "needs_reauth"
        return NotConnected(server.name, server.title, status, await self.connect_url(owner, server.name))

    async def _rejected(self, owner: str, server: ServerConfig) -> NotConnected:
        """The server answered 401: the stored credential is dead, whatever its expiry said."""
        await self.store.mark_needs_reauth(owner, server.auth_group, self.clock())
        self.forget(owner)
        return await self._not_connected(owner, server)

    def forget(self, owner: str) -> None:
        for key in [key for key in self._cache if key[0] == owner]:
            del self._cache[key]

    async def tools(self, owner: str) -> list[McpTool]:
        found: list[McpTool] = []
        for server in self.servers.values():
            cached = self._cache.get((owner, server.name))
            if cached and cached[0] > self.clock():
                found.extend(cached[1])
                continue
            headers = await self.auth[server.auth_group].headers(owner)
            if headers is None:
                continue
            try:
                listed = await self._list(server, headers)
            except _Unauthorized:
                await self._rejected(owner, server)
                continue
            except Exception as exc:
                logger.warning("mcp list_tools failed server=%s error=%s", server.name, type(exc).__name__)
                continue
            tools = [
                McpTool(
                    server=server.name,
                    name=tool.name,
                    title=tool.title or (tool.annotations.title if tool.annotations else None),
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                    access=self.classify(server.name, tool),
                )
                for tool in listed
                if server.exposes(tool.name)
            ]
            self._cache[(owner, server.name)] = (self.clock() + TOOLS_TTL, tools)
            found.extend(tools)
        return found

    async def _list(self, server: ServerConfig, headers: dict[str, str]) -> list[mcp_types.Tool]:
        listed: list[mcp_types.Tool] = []
        async with self._session(server, headers) as client:
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor)
                listed.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return listed

    async def call(self, owner: str, server: str, tool: str, args: dict[str, Any]) -> McpResult | NotConnected:
        config = self._server(server)
        if not config.exposes(tool):
            raise ValueError(f"{server} does not expose tool {tool!r}")
        headers = await self.auth[config.auth_group].headers(owner)
        if headers is None:
            return await self._not_connected(owner, config)
        try:
            async with self._session(config, headers) as client:
                result = await client.call_tool(tool, args)
        except _Unauthorized:
            return await self._rejected(owner, config)
        return McpResult(_text(result.content), bool(result.is_error), result.structured_content)

    async def complete(self, state: str, code: str) -> tuple[str, str]:
        """Finish an OAuth connect from the callback. Raises InvalidState for a forged, expired, or foreign state."""
        pending = open_state(self._state_key, state, self.clock())
        strategy = self.auth.get(pending.auth_group)
        if pending.workspace_id != self.workspace_id or not isinstance(strategy, OAuthAuth):
            raise InvalidState("state names another workspace or a group without OAuth")
        await strategy.complete(pending.user_id, pending.verifier, code)
        self.forget(pending.user_id)
        return pending.user_id, pending.auth_group

    async def save_api_key(self, owner: str, server: str, key: str) -> None:
        strategy = self.auth[self._server(server).auth_group]
        if not isinstance(strategy, ApiKeyAuth):
            raise ValueError(f"{server} does not take an API key")
        await strategy.save(owner, key, self.clock())
        self.forget(owner)
