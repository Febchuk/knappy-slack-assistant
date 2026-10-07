"""Spec 22: "Add to Slack". GET /slack/install sends the browser to Slack, and /slack/oauth_redirect saves the result."""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from cryptography.fernet import InvalidToken
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from knappy.db.repository import utc_now
from knappy.mcp.store import derive_fernet
from knappy.slack.installations import Installation

logger = logging.getLogger(__name__)

# Must match oauth_config.scopes in slack/manifest.yml (tests/test_22_install.py checks).
BOT_SCOPES = (
    "chat:write", "im:history", "im:read", "im:write", "app_mentions:read", "channels:history", "files:read",
    "files:write", "groups:history", "reactions:write", "users:read", "users:read.email",
)
USER_SCOPES = (
    "channels:history", "groups:history", "im:history", "mpim:history", "channels:read", "groups:read", "im:read",
    "mpim:read", "users:read", "chat:write", "search:read",
)
STATE_TTL = timedelta(minutes=10)
AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"

OnInstalled = Callable[[Installation], Awaitable[None]]


class InstallError(Exception):
    """The install did not finish. The message is safe to show the person who tried."""


class InstallFlow:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        public_url: str,
        secret_key: str,
        clock: Callable[[], datetime] = utc_now,
        client: Any | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = public_url.rstrip("/") + "/slack/oauth_redirect"
        self.clock = clock
        self.client = client or AsyncWebClient()
        self._state = derive_fernet(secret_key, "slack-install")

    def authorize_url(self) -> str:
        # The state only proves the install started here in the last 10 minutes. The code exchange needs the secret.
        state = self._state.encrypt_at_time(b"install", int(self.clock().timestamp())).decode()
        query = {
            "client_id": self.client_id,
            "scope": ",".join(BOT_SCOPES),
            "user_scope": ",".join(USER_SCOPES),
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def complete(self, code: str, state: str) -> tuple[Installation, str | None]:
        """Exchange the code for tokens. Returns the installation and the app id (for the "open Slack" link)."""
        try:
            self._state.decrypt_at_time(state.encode(), int(STATE_TTL.total_seconds()), int(self.clock().timestamp()))
        except InvalidToken:
            raise InstallError("This install link is invalid or older than 10 minutes. Start the install again.") from None
        try:
            response = await self.client.oauth_v2_access(
                client_id=self.client_id, client_secret=self.client_secret, code=code, redirect_uri=self.redirect_uri
            )
        except SlackApiError as exc:
            logger.warning("slack install exchange failed error=%s", exc.response.get("error"))
            raise InstallError("Slack did not accept the install. Start the install again.") from None
        if response.get("is_enterprise_install"):
            raise InstallError("Knappy installs into a single workspace, not an Enterprise Grid organization.")
        team = response.get("team") or {}
        user = response.get("authed_user") or {}
        installation = Installation(
            team_id=team["id"],
            team_name=team.get("name") or team["id"],
            bot_token=response["access_token"],
            bot_user_id=response.get("bot_user_id"),
            installer_user_id=user.get("id"),
            user_token=user.get("access_token"),
        )
        return installation, response.get("app_id")


def _page(status: int, message: str, link: tuple[str, str] | None = None) -> web.Response:
    body = f"<!doctype html><title>Knappy</title><p>{html.escape(message)}</p>"
    if link is not None:
        body += f'<p><a href="{html.escape(link[0])}">{html.escape(link[1])}</a></p>'
    return web.Response(status=status, text=body, content_type="text/html")


def add_install_routes(app: web.Application, flow: InstallFlow, on_installed: OnInstalled) -> None:
    async def install(request: web.Request) -> web.Response:
        raise web.HTTPFound(flow.authorize_url())

    async def redirect(request: web.Request) -> web.Response:
        if error := request.query.get("error"):
            logger.info("slack install refused error=%s", error)
            return _page(400, f"Knappy was not installed ({error}).")
        code, state = request.query.get("code"), request.query.get("state")
        if not code or not state:
            return _page(400, "This link is missing its code or state.")
        try:
            installation, app_id = await flow.complete(code, state)
        except InstallError as exc:
            return _page(400, str(exc))
        await on_installed(installation)
        logger.info("slack installed team=%s installer=%s", installation.team_id, installation.installer_user_id)
        link = None
        if app_id:
            query = urlencode({"app": app_id, "team": installation.team_id})
            link = (f"https://slack.com/app_redirect?{query}", "Open Knappy in Slack")
        return _page(200, f"Knappy is installed in {installation.team_name}. Send it a DM to get started.", link)

    app.router.add_get("/slack/install", install)
    app.router.add_get("/slack/oauth_redirect", redirect)
