"""Per-user Slack facts, cached: timezones for prompts, and turning a person's name into a Slack user id."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from knappy.db.repository import SqliteRepository

logger = logging.getLogger("knappy")

DEFAULT_TIMEZONE = "UTC"
CACHE_TTL_S = 24 * 3600
MEMBERS_TTL_S = 3600

SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
MENTION = re.compile(r"<@([UW][A-Z0-9]{2,})(?:\|[^>]*)?>")


class UserDirectory:
    def __init__(self, client: Any | None, ttl_s: float = CACHE_TTL_S) -> None:
        self.client = client
        self.ttl_s = ttl_s
        self._timezones: dict[str, tuple[str, float]] = {}
        self._members: tuple[list[dict[str, Any]], float] | None = None

    async def timezone(self, user_id: str) -> str:
        cached = self._timezones.get(user_id)
        if cached is not None and time.monotonic() - cached[1] < self.ttl_s:
            return cached[0]
        zone = await self._fetch_timezone(user_id)
        self._timezones[user_id] = (zone, time.monotonic())
        return zone

    async def _fetch_timezone(self, user_id: str) -> str:
        if self.client is None or not user_id:
            return DEFAULT_TIMEZONE
        try:
            response = await self.client.users_info(user=user_id)
        except Exception as exc:
            logger.info("users.info failed user=%s error=%s", user_id, type(exc).__name__)
            return DEFAULT_TIMEZONE
        return (response.get("user") or {}).get("tz") or DEFAULT_TIMEZONE

    async def by_email(self, email: str) -> str | None:
        """users.lookupByEmail. Needs the users:read.email scope."""
        if self.client is None or not email:
            return None
        try:
            response = await self.client.users_lookupByEmail(email=email)
        except Exception as exc:
            logger.info("users.lookupByEmail failed error=%s", type(exc).__name__)
            return None
        return (response.get("user") or {}).get("id")

    async def by_name(self, name: str) -> list[str]:
        """Ids of active people whose display or real name is `name`; failing that, whose first name is."""
        wanted = _normal(name)
        if not wanted:
            return []
        members = await self._list_members()
        exact = [m["id"] for m in members if wanted in {_normal(n) for n in _names(m)}]
        if exact:
            return exact
        return [m["id"] for m in members if wanted in {_normal(n).split(" ")[0] for n in _names(m) if n}]

    async def _list_members(self) -> list[dict[str, Any]]:
        if self._members is not None and time.monotonic() - self._members[1] < MEMBERS_TTL_S:
            return self._members[0]
        if self.client is None:
            return []
        members: list[dict[str, Any]] = []
        cursor = None
        try:
            while True:
                response = await self.client.users_list(limit=200, **({"cursor": cursor} if cursor else {}))
                members += [
                    m for m in response.get("members") or []
                    if not m.get("deleted") and not m.get("is_bot") and m.get("id") != "USLACKBOT"
                ]
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as exc:
            logger.info("users.list failed error=%s", type(exc).__name__)
            return []
        self._members = (members, time.monotonic())
        return members


@dataclass(frozen=True)
class Recipient:
    """Who a draft is for. Without a user_id there is nobody to send to, and `problem` says why."""

    name: str
    user_id: str | None = None
    problem: str | None = None


class RecipientResolver:
    """Name to Slack user id: a mention, the contact's stored id, their email, then a unique directory match."""

    def __init__(self, repo: SqliteRepository, workspace_id: str, directory: UserDirectory) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.directory = directory

    async def resolve(self, owner: str, name: str, *, slack_id: str | None = None) -> Recipient:
        mentioned = MENTION.search(name)
        display = MENTION.sub("", name).strip() or name.strip()
        if slack_id and SLACK_USER_ID.match(slack_id):
            return Recipient(display, slack_id)
        if mentioned:
            return Recipient(display, mentioned.group(1))
        contact = await self._contact(owner, display)
        if slack_id and "@" in slack_id:
            found = await self.directory.by_email(slack_id)
            if found:
                return await self._learned(owner, contact, display, found)
        if contact is not None and contact.get("slack_user_id"):
            return Recipient(display, contact["slack_user_id"])
        if contact is not None and contact.get("email"):
            found = await self.directory.by_email(contact["email"])
            if found:
                return await self._learned(owner, contact, display, found)
        matches = await self.directory.by_name(display)
        if len(matches) == 1:
            return await self._learned(owner, contact, display, matches[0])
        if matches:
            return Recipient(display, problem=f"{len(matches)} people in this Slack workspace match {display!r}")
        return Recipient(display, problem=f"I couldn't find {display} in this Slack workspace")

    async def _contact(self, owner: str, name: str) -> dict[str, Any] | None:
        contacts = await self.repo.find_contacts(self.workspace_id, name=name, owner_user_id=owner)
        exact = [contact for contact in contacts if contact["name"].strip().lower() == name.lower()]
        return exact[0] if exact else None

    async def _learned(self, owner: str, contact: dict[str, Any] | None, name: str, user_id: str) -> Recipient:
        """Store a looked-up id on the contact so the next draft needs no lookup."""
        if contact is not None:
            await self.repo.set_contact_slack_id(contact["id"], user_id)
        return Recipient(name, user_id)


def _names(member: dict[str, Any]) -> list[str]:
    profile = member.get("profile") or {}
    return [profile.get("display_name") or "", profile.get("real_name") or member.get("real_name") or "", member.get("name") or ""]


def _normal(name: str) -> str:
    return " ".join(name.strip().lstrip("@").lower().split())
