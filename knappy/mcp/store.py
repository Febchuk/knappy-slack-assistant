"""MCP clients and per-user connections (Spec 19 §4). Every secret is Fernet ciphertext at rest."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from cryptography.fernet import Fernet

from knappy.db.repository import SqliteRepository, format_ts

ConnectionStatus = Literal["connected", "needs_reauth"]


def derive_fernet(secret: str, purpose: str) -> Fernet:
    """One key per purpose, so a leaked state token says nothing about the token key."""
    digest = hashlib.sha256(f"knappy:{purpose}:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: str = ""


@dataclass(frozen=True)
class Connection:
    tokens: Tokens
    status: ConnectionStatus


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None


class McpStore:
    def __init__(self, repo: SqliteRepository, workspace_id: str, secret: str) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self._fernet = derive_fernet(secret, "tokens")

    def _seal(self, value: str | None) -> str | None:
        return None if value is None else self._fernet.encrypt(value.encode()).decode()

    def _open(self, value: str | None) -> str | None:
        return None if value is None else self._fernet.decrypt(value.encode()).decode()

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = await self.repo.connection.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    async def client(self, auth_group: str, issuer: str, redirect_uri: str) -> OAuthClient | None:
        """The registered client, unless the issuer or redirect URI changed since registration."""
        rows = await self._all(
            "SELECT client_id, client_secret FROM mcp_clients WHERE auth_group = ? AND issuer = ? AND redirect_uri = ?",
            (auth_group, issuer, redirect_uri),
        )
        if not rows:
            return None
        return OAuthClient(rows[0]["client_id"], self._open(rows[0]["client_secret"]))

    async def save_client(self, auth_group: str, issuer: str, redirect_uri: str, client: OAuthClient, now: datetime) -> None:
        await self.repo.connection.execute(
            """
            INSERT INTO mcp_clients (auth_group, issuer, redirect_uri, client_id, client_secret, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (auth_group) DO UPDATE SET
                issuer = excluded.issuer, redirect_uri = excluded.redirect_uri, client_id = excluded.client_id,
                client_secret = excluded.client_secret, created_at = excluded.created_at
            """,
            (auth_group, issuer, redirect_uri, client.client_id, self._seal(client.client_secret), format_ts(now)),
        )
        await self.repo.connection.commit()

    async def connection(self, owner: str, auth_group: str) -> Connection | None:
        rows = await self._all(
            """
            SELECT access_token, refresh_token, expires_at, scopes, status FROM mcp_connections
            WHERE workspace_id = ? AND owner_user_id = ? AND auth_group = ?
            """,
            (self.workspace_id, owner, auth_group),
        )
        if not rows:
            return None
        row = rows[0]
        expires = row["expires_at"]
        tokens = Tokens(
            access_token=self._open(row["access_token"]) or "",
            refresh_token=self._open(row["refresh_token"]),
            expires_at=datetime.strptime(expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if expires else None,
            scopes=row["scopes"],
        )
        return Connection(tokens, row["status"])

    async def save_connection(self, owner: str, auth_group: str, tokens: Tokens, now: datetime) -> None:
        await self.repo.connection.execute(
            """
            INSERT INTO mcp_connections (
                workspace_id, owner_user_id, auth_group, access_token, refresh_token, expires_at, scopes, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'connected', ?)
            ON CONFLICT (workspace_id, owner_user_id, auth_group) DO UPDATE SET
                access_token = excluded.access_token, refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at, scopes = excluded.scopes, status = 'connected',
                updated_at = excluded.updated_at
            """,
            (
                self.workspace_id, owner, auth_group, self._seal(tokens.access_token), self._seal(tokens.refresh_token),
                format_ts(tokens.expires_at) if tokens.expires_at else None, tokens.scopes, format_ts(now),
            ),
        )
        await self.repo.connection.commit()

    async def mark_needs_reauth(self, owner: str, auth_group: str, now: datetime) -> None:
        await self.repo.connection.execute(
            """
            UPDATE mcp_connections SET status = 'needs_reauth', updated_at = ?
            WHERE workspace_id = ? AND owner_user_id = ? AND auth_group = ?
            """,
            (format_ts(now), self.workspace_id, owner, auth_group),
        )
        await self.repo.connection.commit()
