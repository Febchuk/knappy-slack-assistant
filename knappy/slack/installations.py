"""Spec 22: one Slack installation per workspace, with its tokens encrypted at rest."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import InvalidToken

from knappy.db.repository import SqliteRepository
from knappy.mcp.store import derive_fernet


@dataclass(frozen=True)
class Installation:
    team_id: str
    team_name: str
    bot_token: str
    bot_user_id: str | None = None
    installer_user_id: str | None = None
    # The installer's User OAuth Token. It turns on workspace awareness for them (Spec 18).
    user_token: str | None = None


class Installations:
    def __init__(self, repo: SqliteRepository, secret_key: str) -> None:
        self.repo = repo
        self._fernet = derive_fernet(secret_key, "slack")

    async def save(self, installation: Installation) -> None:
        await self.repo.save_installation(
            installation.team_id,
            installation.team_name,
            bot_token=self._seal(installation.bot_token),
            bot_user_id=installation.bot_user_id,
            installer_user_id=installation.installer_user_id,
            user_token=self._seal(installation.user_token) if installation.user_token else None,
        )

    async def get(self, team_id: str) -> Installation | None:
        row = await self.repo.get_workspace(team_id)
        return self._open(row) if row is not None else None

    async def all(self) -> list[Installation]:
        return [installation for row in await self.repo.list_workspaces() if (installation := self._open(row))]

    async def revoke(self, team_id: str) -> None:
        """Forget the tokens. The workspace's memory stays, so a reinstall picks up where it left off."""
        await self.repo.revoke_installation(team_id)

    def _seal(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def _unseal(self, sealed: str | None) -> str | None:
        if not sealed:
            return None
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken:
            return None

    def _open(self, row: dict) -> Installation | None:
        # A revoked row, or one written before Spec 22 with a plaintext token, is not an installation.
        bot_token = self._unseal(row["bot_token"])
        if bot_token is None:
            return None
        return Installation(
            team_id=row["id"],
            team_name=row["team_name"],
            bot_token=bot_token,
            bot_user_id=row.get("bot_user_id"),
            installer_user_id=row.get("installer_user_id"),
            user_token=self._unseal(row.get("user_token")),
        )
