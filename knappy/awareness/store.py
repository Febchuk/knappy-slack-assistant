"""What awareness keeps (Spec 18 §4-§7): attention items, excluded conversations, and per-conversation read cursors.

Raw message text never reaches these tables. An attention item is a one-line summary with a permalink.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from knappy.db.repository import SqliteRepository, format_ts

AttentionKind = Literal["asks_user", "assigns_user", "waiting_on_user"]
AttentionStatus = Literal["OPEN", "ANSWERED", "DONE", "DISMISSED", "SNOOZED"]
Urgency = Literal["low", "today", "now"]
ATTENTION_KINDS: tuple[AttentionKind, ...] = ("asks_user", "assigns_user", "waiting_on_user")
UPDATE_KINDS = ("workstream_update", "fyi")
URGENCY_RANK = {"now": 0, "today": 1, "low": 2}


@dataclass(frozen=True)
class AttentionItem:
    id: str
    kind: AttentionKind
    summary: str
    who: str | None
    who_slack_id: str | None
    channel_id: str
    channel_name: str | None
    thread_ts: str | None
    source_ts: str
    permalink: str | None
    due_at: str | None
    urgency: Urgency
    status: AttentionStatus
    created_at: str

    @property
    def where(self) -> str:
        if self.channel_name:
            return self.channel_name
        return "a DM" if self.channel_id.startswith("D") else "a conversation"

    def for_model(self) -> dict[str, Any]:
        view = {
            "id": self.id, "kind": self.kind, "summary": self.summary, "who": self.who, "where": self.where,
            "due_utc": self.due_at, "urgency": self.urgency, "status": self.status, "link": self.permalink,
            "since_utc": self.created_at,
        }
        return {key: value for key, value in view.items() if value is not None}


@dataclass(frozen=True)
class Update:
    """A workstream_update or fyi observation, for "what did I miss?" and the brief's Worth knowing section."""

    event_id: str
    kind: str
    summary: str
    permalink: str | None
    where: str | None
    created_at: str

    def for_model(self) -> dict[str, Any]:
        view = {"kind": self.kind, "summary": self.summary, "where": self.where, "link": self.permalink, "when_utc": self.created_at}
        return {key: value for key, value in view.items() if value is not None}


_COLUMNS = (
    "id, kind, summary, who, who_slack_id, channel_id, channel_name, thread_ts, source_ts, permalink, due_at, urgency, "
    "status, created_at"
)


class AwarenessStore:
    def __init__(self, repo: SqliteRepository, workspace_id: str) -> None:
        self.repo = repo
        self.workspace_id = workspace_id

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = await self.repo.connection.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    async def _run(self, sql: str, params: tuple[Any, ...]) -> int:
        cursor = await self.repo.connection.execute(sql, params)
        return cursor.rowcount

    async def upsert_item(
        self,
        owner: str,
        *,
        kind: AttentionKind,
        summary: str,
        who: str | None,
        who_slack_id: str | None,
        channel_id: str,
        channel_name: str | None,
        thread_ts: str | None,
        source_ts: str,
        permalink: str | None,
        due_at: datetime | None,
        urgency: Urgency,
        now: datetime,
    ) -> str:
        """One item per source message. Seeing it again (an edit, a re-read) refreshes it while it is still open."""
        rows = await self._all(
            """
            INSERT INTO attention_items (
                id, workspace_id, owner_user_id, kind, summary, who, who_slack_id, channel_id, channel_name, thread_ts,
                source_ts, permalink, due_at, urgency, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (owner_user_id, channel_id, source_ts) DO UPDATE SET
                kind = excluded.kind, summary = excluded.summary, who = excluded.who, due_at = excluded.due_at,
                urgency = excluded.urgency
            WHERE attention_items.status = 'OPEN'
            RETURNING id
            """,
            (
                uuid.uuid4().hex[:12], self.workspace_id, owner, kind, summary, who, who_slack_id, channel_id, channel_name,
                thread_ts, source_ts, permalink, format_ts(due_at) if due_at else None, urgency, format_ts(now),
            ),
        )
        if rows:
            return rows[0]["id"]
        existing = await self._all(
            "SELECT id FROM attention_items WHERE owner_user_id = ? AND channel_id = ? AND source_ts = ?",
            (owner, channel_id, source_ts),
        )
        return existing[0]["id"]

    async def get(self, owner: str, item_id: str) -> AttentionItem | None:
        rows = await self._all(
            f"SELECT {_COLUMNS} FROM attention_items WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
            (self.workspace_id, owner, item_id),
        )
        return AttentionItem(**rows[0]) if rows else None

    async def items(self, owner: str, now: datetime, status: AttentionStatus = "OPEN", limit: int = 25) -> list[AttentionItem]:
        """Most urgent first, then oldest first. A snooze that has run out counts as open again."""
        await self._run(
            """
            UPDATE attention_items SET status = 'OPEN', snoozed_until = NULL
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'SNOOZED' AND snoozed_until <= ?
            """,
            (self.workspace_id, owner, format_ts(now)),
        )
        await self.repo.connection.commit()
        rows = await self._all(
            f"""
            SELECT {_COLUMNS} FROM attention_items
            WHERE workspace_id = ? AND owner_user_id = ? AND status = ?
            ORDER BY CASE urgency WHEN 'now' THEN 0 WHEN 'today' THEN 1 ELSE 2 END, created_at, source_ts
            LIMIT ?
            """,
            (self.workspace_id, owner, status, limit),
        )
        return [AttentionItem(**row) for row in rows]

    async def resolve(self, owner: str, item_id: str, status: AttentionStatus, now: datetime) -> bool:
        snoozed = format_ts(now + timedelta(hours=24)) if status == "SNOOZED" else None
        resolved = None if status in ("OPEN", "SNOOZED") else format_ts(now)
        changed = await self._run(
            """
            UPDATE attention_items SET status = ?, snoozed_until = ?, resolved_at = ?
            WHERE workspace_id = ? AND owner_user_id = ? AND id = ?
            """,
            (status, snoozed, resolved, self.workspace_id, owner, item_id),
        )
        await self.repo.connection.commit()
        return changed == 1

    async def answered(self, owner: str, channel_id: str, *, thread_ts: str | None, ts: str, direct: bool, now: datetime) -> int:
        """The owner posted: items they replied to are ANSWERED. Their thread, or in a DM anything they posted after."""
        if direct:
            sql = "source_ts < ?"
            params: tuple[Any, ...] = (ts,)
        elif thread_ts:
            sql = "(thread_ts = ? OR source_ts = ?) AND source_ts < ?"
            params = (thread_ts, thread_ts, ts)
        else:
            return 0
        changed = await self._run(
            f"""
            UPDATE attention_items SET status = 'ANSWERED', resolved_at = ?
            WHERE workspace_id = ? AND owner_user_id = ? AND channel_id = ? AND status IN ('OPEN', 'SNOOZED') AND {sql}
            """,
            (format_ts(now), self.workspace_id, owner, channel_id, *params),
        )
        return changed

    async def urgent_undecided(self, owner: str) -> list[AttentionItem]:
        """Open urgency-now items no interrupt decision has been made on yet (Spec 18 §6)."""
        rows = await self._all(
            f"""
            SELECT {_COLUMNS} FROM attention_items
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'OPEN' AND urgency = 'now' AND last_surfaced_at IS NULL
            ORDER BY created_at
            """,
            (self.workspace_id, owner),
        )
        return [AttentionItem(**row) for row in rows]

    async def mark_surfaced(self, item_ids: list[str], now: datetime) -> None:
        for item_id in item_ids:
            await self._run("UPDATE attention_items SET last_surfaced_at = ? WHERE id = ?", (format_ts(now), item_id))
        await self.repo.connection.commit()

    async def updates(self, owner: str, since: datetime, *, unbriefed: bool = False, limit: int = 10) -> list[Update]:
        rows = await self._all(
            """
            SELECT id, summary, metadata, created_at, admission_score FROM memory_events
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'ACTIVE' AND metadata IS NOT NULL AND created_at >= ?
            ORDER BY admission_score DESC, created_at DESC
            """,
            (self.workspace_id, owner, format_ts(since)),
        )
        found = []
        for row in rows:
            meta = json.loads(row["metadata"])
            if meta.get("observation") not in UPDATE_KINDS or (unbriefed and meta.get("briefed")):
                continue
            found.append(Update(row["id"], meta["observation"], row["summary"], meta.get("permalink"), meta.get("where"), str(row["created_at"])))
        return found[:limit]

    async def mark_briefed(self, event_ids: list[str]) -> None:
        for event_id in event_ids:
            rows = await self._all("SELECT metadata FROM memory_events WHERE id = ?", (event_id,))
            if rows:
                meta = {**json.loads(rows[0]["metadata"] or "{}"), "briefed": True}
                await self._run("UPDATE memory_events SET metadata = ? WHERE id = ?", (json.dumps(meta), event_id))
        await self.repo.connection.commit()

    async def exclude(self, owner: str, channel_id: str, channel_name: str | None, now: datetime) -> None:
        await self._run(
            """
            INSERT INTO awareness_excluded (workspace_id, owner_user_id, channel_id, channel_name, created_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (self.workspace_id, owner, channel_id, channel_name, format_ts(now)),
        )
        await self._run(
            "DELETE FROM attention_items WHERE workspace_id = ? AND owner_user_id = ? AND channel_id = ?",
            (self.workspace_id, owner, channel_id),
        )
        await self._run(
            "DELETE FROM awareness_cursors WHERE workspace_id = ? AND owner_user_id = ? AND channel_id = ?",
            (self.workspace_id, owner, channel_id),
        )

    async def excluded(self, owner: str) -> set[str]:
        rows = await self._all(
            "SELECT channel_id FROM awareness_excluded WHERE workspace_id = ? AND owner_user_id = ?",
            (self.workspace_id, owner),
        )
        return {row["channel_id"] for row in rows}

    async def channel_events(self, owner: str, channel_id: str) -> list[str]:
        """Every active ledger event learned from this conversation: observations, and what the reconciler made of them."""
        rows = await self._all(
            """
            SELECT p.target_id AS id FROM memory_provenance p
            WHERE p.owner_user_id = ? AND p.target_type = 'event' AND p.source_type = 'slack_message' AND p.source_id LIKE ?
            UNION
            SELECT p.target_id AS id FROM memory_provenance p
            JOIN conversation_turns t ON t.id = p.source_id
            WHERE p.owner_user_id = ? AND p.target_type = 'event' AND p.source_type = 'turn' AND t.conversation_key = ?
            """,
            (owner, f"{channel_id}:%", owner, f"awareness:{channel_id}"),
        )
        return [row["id"] for row in rows]

    async def drop_turns(self, owner: str, channel_id: str) -> None:
        await self._run(
            "DELETE FROM conversation_turns WHERE workspace_id = ? AND owner_user_id = ? AND conversation_key = ?",
            (self.workspace_id, owner, f"awareness:{channel_id}"),
        )

    async def cursors(self, owner: str) -> dict[str, str]:
        rows = await self._all(
            "SELECT channel_id, last_ts FROM awareness_cursors WHERE workspace_id = ? AND owner_user_id = ?",
            (self.workspace_id, owner),
        )
        return {row["channel_id"]: row["last_ts"] for row in rows}

    async def advance_cursor(self, owner: str, channel_id: str, ts: str) -> None:
        # Slack ts strings ("1696000000.000100") sort as numbers only after padding; compare as floats.
        current = (await self.cursors(owner)).get(channel_id)
        if current is not None and float(current) >= float(ts):
            return
        await self._run(
            """
            INSERT INTO awareness_cursors (workspace_id, owner_user_id, channel_id, last_ts) VALUES (?, ?, ?, ?)
            ON CONFLICT (workspace_id, owner_user_id, channel_id) DO UPDATE SET last_ts = excluded.last_ts
            """,
            (self.workspace_id, owner, channel_id, ts),
        )
