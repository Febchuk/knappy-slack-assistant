"""A relational cache of Slack people and channels the owner can see.

Names resolve here before a live history read. This is not memory: rows are not embedded, and a
person record points at a Slack id only when that name matches one directory row.
"""

from __future__ import annotations

from knappy.db.repository import SqliteRepository


def normal_label(value: str) -> str:
    return " ".join(value.strip().lstrip("#@").lower().split())


def name_matches(query: str, display_name: str, real_name: str, handle: str) -> bool:
    wanted = normal_label(query)
    if not wanted:
        return False
    names = [normal_label(name) for name in (display_name, real_name, handle) if name]
    if wanted in names:
        return True
    return wanted in {name.split(" ", 1)[0] for name in names if name}


def matching_user_ids(users: list[dict[str, str]], query: str) -> list[str]:
    return [
        user["slack_user_id"]
        for user in users
        if name_matches(query, user.get("display_name") or "", user.get("real_name") or "", user.get("handle") or "")
    ]


def matching_channel_id(channels: list[dict[str, str]], query: str) -> str | None:
    wanted = normal_label(query)
    if not wanted:
        return None
    hits = [channel["channel_id"] for channel in channels if normal_label(channel.get("name") or "") == wanted]
    unique = list(dict.fromkeys(hits))
    return unique[0] if len(unique) == 1 else None


async def slack_id_for_name(repo: SqliteRepository, workspace_id: str, owner: str, name: str) -> str | None:
    """The Slack user id when `name` matches exactly one directory row. Several matches stay unset."""
    hits = list(dict.fromkeys(matching_user_ids(await repo.directory_users(workspace_id, owner), name)))
    return hits[0] if len(hits) == 1 else None
