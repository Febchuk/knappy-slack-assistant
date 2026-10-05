"""The OAuth redirect target (Spec 19 §5): GET /oauth/callback, served next to the Socket Mode app.

Spec 22: each workspace has its own hub, so `resolve` picks the hub the state was sealed for.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable

import httpx
from aiohttp import web

from knappy.mcp.auth import DiscoveryError, InvalidState, TokenError
from knappy.mcp.hub import McpHub

logger = logging.getLogger(__name__)

OnConnected = Callable[[str, str], Awaitable[None]]
Resolve = Callable[[str], Awaitable[tuple[McpHub, OnConnected] | None]]


def _page(status: int, message: str) -> web.Response:
    body = f"<!doctype html><title>Knappy</title><p>{html.escape(message)}</p>"
    return web.Response(status=status, text=body, content_type="text/html")


def add_callback(app: web.Application, resolve: Resolve) -> None:
    async def callback(request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        if error := request.query.get("error"):
            logger.info("mcp callback refused error=%s", error)
            return _page(400, f"The app did not grant access ({error}). Ask Knappy for a new link to try again.")
        code = request.query.get("code")
        if not state or not code:
            return _page(400, "This link is missing its code or state.")
        target = await resolve(state)
        if target is None:
            return _page(400, "This link is invalid or older than 10 minutes. Ask Knappy for a new one.")
        hub, on_connected = target
        try:
            user_id, auth_group = await hub.complete(state, code)
        except InvalidState:
            return _page(400, "This link is invalid or older than 10 minutes. Ask Knappy for a new one.")
        except (TokenError, DiscoveryError, httpx.HTTPError) as exc:
            logger.warning("mcp callback exchange failed error=%s", exc)
            return _page(502, "The app did not accept the sign-in. Ask Knappy for a new link to try again.")
        logger.info("mcp connected owner=%s auth_group=%s", user_id, auth_group)
        try:
            await on_connected(user_id, auth_group)
        except Exception:
            logger.exception("mcp on_connected failed")
        return _page(200, "Connected. You can close this tab and go back to Slack.")

    app.router.add_get("/oauth/callback", callback)


async def serve(app: web.Application, port: int, host: str = "0.0.0.0") -> web.AppRunner:
    # No access log: the query string carries the authorization code.
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("web listening port=%d", port)
    return runner
