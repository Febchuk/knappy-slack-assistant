"""Auth modes behind one interface (Spec 19 §3): OAuth with DCR or a static client, per-user API keys, and service tokens."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken

from knappy.mcp.servers import ServerConfig
from knappy.mcp.store import McpStore, OAuthClient, Tokens

logger = logging.getLogger(__name__)

Status = Literal["connected", "not_connected", "needs_reauth", "unavailable"]
STATE_TTL = timedelta(minutes=10)
REFRESH_MARGIN = timedelta(minutes=5)
HTTP_TIMEOUT_S = 15.0


class DiscoveryError(RuntimeError):
    pass


class InvalidState(ValueError):
    pass


class TokenError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthServer:
    """What discovery learns: the protected resource's metadata joined to its authorization server's."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    grant_types: tuple[str, ...]
    scopes: tuple[str, ...]
    resource: str
    resource_name: str | None

    @property
    def refreshes(self) -> bool:
        return "refresh_token" in self.grant_types


def _well_known(url: str, name: str) -> list[str]:
    """RFC 9728 / RFC 8414 locations: the path-inserted form first, then the root form."""
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    candidates = [f"{origin}/.well-known/{name}{path}"] if path else []
    return [*candidates, f"{origin}/.well-known/{name}"]


async def _first_json(http: httpx.AsyncClient, urls: list[str]) -> dict | None:
    for url in urls:
        try:
            response = await http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            continue
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                continue
            if isinstance(body, dict):
                return body
    return None


async def discover(url: str, http: httpx.AsyncClient) -> AuthServer:
    resource = await _first_json(http, _well_known(url, "oauth-protected-resource"))
    if not resource or not resource.get("authorization_servers"):
        raise DiscoveryError(f"{url} publishes no OAuth protected-resource metadata")
    issuer = str(resource["authorization_servers"][0]).rstrip("/")
    server = await _first_json(
        http, [*_well_known(issuer, "oauth-authorization-server"), *_well_known(issuer, "openid-configuration")]
    )
    if not server or not server.get("authorization_endpoint") or not server.get("token_endpoint"):
        raise DiscoveryError(f"{issuer} publishes no authorization-server metadata")
    return AuthServer(
        issuer=issuer,
        authorization_endpoint=server["authorization_endpoint"],
        token_endpoint=server["token_endpoint"],
        registration_endpoint=server.get("registration_endpoint"),
        # RFC 8414 leaves grant_types_supported optional; most servers that omit it refresh.
        grant_types=tuple(server.get("grant_types_supported") or ("authorization_code", "refresh_token")),
        scopes=tuple(resource.get("scopes_supported") or server.get("scopes_supported") or ()),
        resource=resource.get("resource") or url,
        resource_name=resource.get("resource_name"),
    )


@dataclass(frozen=True)
class PendingAuth:
    workspace_id: str
    user_id: str
    auth_group: str
    verifier: str


def seal_state(fernet: Fernet, pending: PendingAuth, now: datetime) -> str:
    """Authenticated and encrypted, so the PKCE verifier can ride along without appearing in the URL."""
    payload = {
        "w": pending.workspace_id, "u": pending.user_id, "g": pending.auth_group, "v": pending.verifier,
        "n": secrets.token_urlsafe(8),
    }
    return fernet.encrypt_at_time(json.dumps(payload).encode(), int(now.timestamp())).decode()


def open_state(fernet: Fernet, state: str, now: datetime) -> PendingAuth:
    try:
        raw = fernet.decrypt_at_time(state.encode(), int(STATE_TTL.total_seconds()), int(now.timestamp()))
    except (InvalidToken, ValueError):
        raise InvalidState("state is forged or expired") from None
    payload = json.loads(raw)
    return PendingAuth(payload["w"], payload["u"], payload["g"], payload["v"])


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class AuthStrategy(Protocol):
    async def headers(self, owner: str) -> dict[str, str] | None:
        """Request headers carrying the owner's credential, or None when there is no usable one."""

    async def status(self, owner: str) -> Status: ...

    async def connect_url(self, owner: str) -> str | None: ...


class UnavailableAuth:
    """A server whose required env vars are unset. Nobody can connect it."""

    async def headers(self, owner: str) -> dict[str, str] | None:
        return None

    async def status(self, owner: str) -> Status:
        return "unavailable"

    async def connect_url(self, owner: str) -> str | None:
        return None


class ServiceAuth:
    def __init__(self, token: str) -> None:
        self._token = token

    async def headers(self, owner: str) -> dict[str, str] | None:
        return _bearer(self._token)

    async def status(self, owner: str) -> Status:
        return "connected"

    async def connect_url(self, owner: str) -> str | None:
        return None


class ApiKeyAuth:
    def __init__(self, store: McpStore, auth_group: str) -> None:
        self.store = store
        self.auth_group = auth_group

    async def headers(self, owner: str) -> dict[str, str] | None:
        connection = await self.store.connection(owner, self.auth_group)
        if connection is None or connection.status != "connected":
            return None
        return _bearer(connection.tokens.access_token)

    async def status(self, owner: str) -> Status:
        connection = await self.store.connection(owner, self.auth_group)
        return connection.status if connection else "not_connected"

    async def connect_url(self, owner: str) -> str | None:
        return None

    async def save(self, owner: str, key: str, now: datetime) -> None:
        await self.store.save_connection(owner, self.auth_group, Tokens(key, None, None), now)


class OAuthAuth:
    """One auth group's OAuth flow. `static_client` set means oauth_static; None means register dynamically."""

    def __init__(
        self,
        servers: list[ServerConfig],
        store: McpStore,
        *,
        redirect_uri: str,
        state_key: Fernet,
        static_client: OAuthClient | None,
        clock: Callable[[], datetime],
    ) -> None:
        self.servers = servers
        self.auth_group = servers[0].auth_group
        self.store = store
        self.redirect_uri = redirect_uri
        self.state_key = state_key
        self.static_client = static_client
        self.clock = clock
        self.scopes = list(dict.fromkeys(scope for server in servers for scope in server.scopes))
        self._metadata: AuthServer | None = None
        self._refreshing: dict[str, asyncio.Lock] = {}

    async def metadata(self) -> AuthServer:
        if self._metadata is None:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
                self._metadata = await discover(self.servers[0].url, http)
        return self._metadata

    async def client(self) -> OAuthClient:
        if self.static_client is not None:
            return self.static_client
        meta = await self.metadata()
        cached = await self.store.client(self.auth_group, meta.issuer, self.redirect_uri)
        if cached is not None:
            return cached
        if not meta.registration_endpoint:
            raise DiscoveryError(f"{meta.issuer} has no registration endpoint; use auth = 'oauth_static'")
        body: dict[str, object] = {
            "client_name": "Knappy",
            "redirect_uris": [self.redirect_uri],
            "grant_types": [grant for grant in ("authorization_code", "refresh_token") if grant in meta.grant_types],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        if self.scopes:
            body["scope"] = " ".join(self.scopes)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
            response = await http.post(meta.registration_endpoint, json=body)
        if response.status_code not in (200, 201):
            raise DiscoveryError(f"client registration at {meta.issuer} failed: HTTP {response.status_code} {response.text[:300]}")
        registered = response.json()
        client = OAuthClient(registered["client_id"], registered.get("client_secret"))
        await self.store.save_client(self.auth_group, meta.issuer, self.redirect_uri, client, self.clock())
        logger.info("mcp registered client auth_group=%s issuer=%s", self.auth_group, meta.issuer)
        return client

    async def connect_url(self, owner: str) -> str | None:
        meta = await self.metadata()
        client = await self.client()
        verifier = secrets.token_urlsafe(48)
        state = seal_state(self.state_key, PendingAuth(self.store.workspace_id, owner, self.auth_group, verifier), self.clock())
        params = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        if self.static_client is None:
            params["resource"] = meta.resource
        else:
            # Google returns a refresh token only for offline access, and only on a fresh consent.
            params.update(access_type="offline", prompt="consent")
        return f"{meta.authorization_endpoint}?{urlencode(params)}"

    async def _token_request(self, form: dict[str, str]) -> Tokens:
        meta = await self.metadata()
        client = await self.client()
        form = {**form, "client_id": client.client_id}
        if client.client_secret:
            form["client_secret"] = client.client_secret
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as http:
            response = await http.post(meta.token_endpoint, data=form, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise TokenError(f"token endpoint returned HTTP {response.status_code} {response.text[:300]}")
        body = response.json()
        if not body.get("access_token"):
            raise TokenError("token response has no access_token")
        expires_in = body.get("expires_in")
        return Tokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=self.clock() + timedelta(seconds=int(expires_in)) if expires_in else None,
            scopes=body.get("scope") or " ".join(self.scopes),
        )

    async def complete(self, owner: str, verifier: str, code: str) -> None:
        tokens = await self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri, "code_verifier": verifier}
        )
        await self.store.save_connection(owner, self.auth_group, tokens, self.clock())

    def _expiring(self, tokens: Tokens) -> bool:
        return tokens.expires_at is not None and tokens.expires_at - self.clock() < REFRESH_MARGIN

    async def headers(self, owner: str) -> dict[str, str] | None:
        connection = await self.store.connection(owner, self.auth_group)
        if connection is None or connection.status != "connected":
            return None
        if not self._expiring(connection.tokens):
            return _bearer(connection.tokens.access_token)
        # Serialized per owner: a refresh token may be single-use, and a second concurrent refresh would burn it.
        async with self._refreshing.setdefault(owner, asyncio.Lock()):
            connection = await self.store.connection(owner, self.auth_group)
            if connection is None or connection.status != "connected":
                return None
            if not self._expiring(connection.tokens):
                return _bearer(connection.tokens.access_token)
            refreshed = await self._refresh(connection.tokens)
            if refreshed is None:
                await self.store.mark_needs_reauth(owner, self.auth_group, self.clock())
                logger.info("mcp needs reauth owner=%s auth_group=%s", owner, self.auth_group)
                return None
            await self.store.save_connection(owner, self.auth_group, refreshed, self.clock())
            return _bearer(refreshed.access_token)

    async def _refresh(self, tokens: Tokens) -> Tokens | None:
        if not tokens.refresh_token:
            return None
        try:
            fresh = await self._token_request({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})
        except (TokenError, httpx.HTTPError, DiscoveryError) as exc:
            logger.warning("mcp refresh failed auth_group=%s error=%s", self.auth_group, exc)
            return None
        if fresh.refresh_token is None:
            return Tokens(fresh.access_token, tokens.refresh_token, fresh.expires_at, fresh.scopes)
        return fresh

    async def status(self, owner: str) -> Status:
        connection = await self.store.connection(owner, self.auth_group)
        if connection is None:
            return "not_connected"
        if connection.status == "connected" and self._expiring(connection.tokens) and not connection.tokens.refresh_token:
            return "needs_reauth"
        return connection.status
