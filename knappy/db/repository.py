"""SQLite repository for contacts, interactions, drafts, and briefings."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from knappy.db.schema import EXPECTED_TABLES, SQLITE_SCHEMA
from knappy.db.vectors import cosine_distance, pack_embedding


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_ts(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    return current.strftime("%Y-%m-%d %H:%M:%S")


class SqliteRepository:
    """Async repository. One connection owns an in-memory or file database."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Repository is not connected")
        return self._conn

    async def init_schema(self) -> None:
        await self.connection.executescript(SQLITE_SCHEMA)
        await self.connection.execute("PRAGMA foreign_keys = ON")
        await self.connection.commit()

    async def table_names(self) -> set[str]:
        cursor = await self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        rows = await cursor.fetchall()
        return {row["name"] for row in rows}

    async def upsert_workspace(self, workspace_id: str, team_name: str, bot_token: str) -> None:
        await self.connection.execute(
            """
            INSERT INTO workspaces (id, team_name, bot_token)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET team_name = excluded.team_name, bot_token = excluded.bot_token
            """,
            (workspace_id, team_name, bot_token),
        )
        await self.connection.commit()

    async def upsert_contact(
        self,
        workspace_id: str,
        name: str,
        *,
        slack_user_id: str | None = None,
        email: str | None = None,
        company: str | None = None,
        role: str | None = None,
        reminder_cadence_days: int = 30,
        last_interaction_ts: str | None = None,
        commit: bool = True,
    ) -> str:
        contact_id = str(uuid.uuid4())
        touched = last_interaction_ts or format_ts()
        cursor = await self.connection.execute(
            """
            INSERT INTO contacts (
                id, workspace_id, name, slack_user_id, email, company, role,
                reminder_cadence_days, last_interaction_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, name) DO UPDATE SET
                slack_user_id = COALESCE(excluded.slack_user_id, contacts.slack_user_id),
                email = COALESCE(excluded.email, contacts.email),
                company = COALESCE(excluded.company, contacts.company),
                role = COALESCE(excluded.role, contacts.role),
                last_interaction_ts = excluded.last_interaction_ts
            RETURNING id
            """,
            (
                contact_id,
                workspace_id,
                name,
                slack_user_id,
                email,
                company,
                role,
                reminder_cadence_days,
                touched,
            ),
        )
        row = await cursor.fetchone()
        if commit:
            await self.connection.commit()
        return str(row["id"])

    async def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM contacts WHERE id = ?", (contact_id,))

    async def find_contacts(
        self,
        workspace_id: str,
        *,
        name: str | None = None,
        company: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["workspace_id = ?"]
        params: list[Any] = [workspace_id]
        if name:
            clauses.append("lower(name) LIKE '%' || lower(?) || '%'")
            params.append(name)
        if company:
            clauses.append("lower(COALESCE(company, '')) LIKE '%' || lower(?) || '%'")
            params.append(company)
        sql = f"SELECT * FROM contacts WHERE {' AND '.join(clauses)} ORDER BY name"
        return await self._all(sql, tuple(params))

    async def delete_contact(self, contact_id: str) -> None:
        await self.connection.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        await self.connection.commit()

    async def insert_interaction(
        self,
        *,
        workspace_id: str,
        contact_id: str,
        source_type: str,
        channel_id: str,
        raw_text: str,
        summary: str,
        thread_ts: str | None = None,
        commitment: str | None = None,
        due_date: str | None = None,
        status: str = "PENDING",
        embedding: list[float] | None = None,
        commit: bool = True,
    ) -> str:
        interaction_id = str(uuid.uuid4())
        blob = pack_embedding(embedding) if embedding is not None else None
        await self.connection.execute(
            """
            INSERT INTO interactions (
                id, workspace_id, contact_id, source_type, channel_id, thread_ts,
                raw_text, summary, commitment, due_date, status, embedding
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id,
                workspace_id,
                contact_id,
                source_type,
                channel_id,
                thread_ts,
                raw_text,
                summary,
                commitment,
                due_date,
                status,
                blob,
            ),
        )
        if commit:
            await self.connection.commit()
        return interaction_id

    async def record_interaction(
        self,
        *,
        workspace_id: str,
        contact_name: str,
        source_type: str,
        channel_id: str,
        raw_text: str,
        summary: str,
        thread_ts: str | None = None,
        contact_email: str | None = None,
        company: str | None = None,
        commitment: str | None = None,
        due_date: str | None = None,
        embedding: list[float] | None = None,
        last_interaction_ts: str | None = None,
    ) -> tuple[str, str]:
        try:
            contact_id = await self.upsert_contact(
                workspace_id,
                contact_name,
                email=contact_email,
                company=company,
                last_interaction_ts=last_interaction_ts,
                commit=False,
            )
            interaction_id = await self.insert_interaction(
                workspace_id=workspace_id,
                contact_id=contact_id,
                source_type=source_type,
                channel_id=channel_id,
                thread_ts=thread_ts,
                raw_text=raw_text,
                summary=summary,
                commitment=commitment,
                due_date=due_date,
                embedding=embedding,
                commit=False,
            )
            await self.connection.commit()
        except Exception:
            await self.connection.rollback()
            raise
        return contact_id, interaction_id

    async def list_interactions_for_contact(self, contact_id: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM interactions WHERE contact_id = ? ORDER BY created_at",
            (contact_id,),
        )

    async def recent_interactions(self, contact_id: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT * FROM interactions
            WHERE contact_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (contact_id, limit),
        )

    async def get_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM interactions WHERE id = ?", (interaction_id,))

    async def nearest_interactions(
        self,
        embedding: list[float],
        *,
        limit: int = 5,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["embedding IS NOT NULL"]
        params: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        cursor = await self.connection.execute(
            f"SELECT * FROM interactions WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        rows = await cursor.fetchall()
        scored: list[tuple[float, aiosqlite.Row]] = []
        for row in rows:
            blob = row["embedding"]
            vector = unpack_public(blob)
            scored.append((cosine_distance(embedding, vector), row))
        scored.sort(key=lambda item: item[0])
        results = []
        for distance, row in scored[:limit]:
            item = _public_row(row)
            item["cosine_distance"] = distance
            results.append(item)
        return results

    async def search_commitments(
        self,
        workspace_id: str,
        *,
        query: str,
        query_embedding: list[float] | None = None,
        status: str | None = "PENDING",
        due_before: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        clauses = ["i.workspace_id = ?", "i.commitment IS NOT NULL"]
        params: list[Any] = [workspace_id]
        if status:
            clauses.append("i.status = ?")
            params.append(status)
        if due_before:
            clauses.append("i.due_date IS NOT NULL AND i.due_date <= ?")
            params.append(due_before)
        cursor = await self.connection.execute(
            f"""
            SELECT i.*, c.name AS contact_name, c.company AS company
            FROM interactions i
            JOIN contacts c ON c.id = i.contact_id
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )
        rows = await cursor.fetchall()
        tokens = {token for token in query.lower().split() if len(token) > 2}
        ranked: list[tuple[tuple[int, float], dict[str, Any]]] = []
        for row in rows:
            item = _public_row(row)
            haystack = f"{item.get('commitment') or ''} {item.get('summary') or ''}".lower()
            overlap = sum(1 for token in tokens if token in haystack)
            distance = 1.0
            if query_embedding is not None and row["embedding"] is not None:
                distance = cosine_distance(query_embedding, unpack_public(row["embedding"]))
            if overlap == 0 and distance > 0.25:
                continue
            ranked.append(((-overlap, distance), item))
        ranked.sort(key=lambda pair: pair[0])
        return [item for _, item in ranked[:limit]]

    async def update_interaction_status(self, interaction_id: str, status: str) -> None:
        await self.connection.execute(
            "UPDATE interactions SET status = ? WHERE id = ?",
            (status, interaction_id),
        )
        await self.connection.commit()

    async def snooze_interaction(self, interaction_id: str, hours: int = 24) -> str | None:
        row = await self.get_interaction(interaction_id)
        if row is None or not row.get("due_date"):
            return None
        current = datetime.strptime(row["due_date"], "%Y-%m-%d %H:%M:%S")
        updated = format_ts(current + timedelta(hours=hours))
        await self.connection.execute(
            "UPDATE interactions SET due_date = ?, last_alerted_at = ? WHERE id = ?",
            (updated, format_ts(), interaction_id),
        )
        await self.connection.commit()
        return updated

    async def mark_alerted(self, interaction_id: str) -> None:
        await self.connection.execute(
            "UPDATE interactions SET last_alerted_at = ? WHERE id = ?",
            (format_ts(), interaction_id),
        )
        await self.connection.commit()

    async def scan_due_commitments(self, workspace_id: str, within_hours: int = 12) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT
                c.id AS contact_id,
                c.name AS contact_name,
                c.company AS company,
                c.slack_user_id,
                i.id AS interaction_id,
                i.commitment,
                i.due_date,
                i.summary
            FROM interactions i
            JOIN contacts c ON i.contact_id = c.id
            WHERE i.workspace_id = ?
              AND i.status = 'PENDING'
              AND i.commitment IS NOT NULL
              AND i.due_date IS NOT NULL
              AND i.due_date <= datetime('now', ?)
              AND (i.last_alerted_at IS NULL OR i.last_alerted_at < datetime('now', '-24 hours'))
            """,
            (workspace_id, f"+{within_hours} hours"),
        )

    async def scan_dormant_contacts(self, workspace_id: str) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT
                id AS contact_id,
                name AS contact_name,
                company,
                slack_user_id,
                reminder_cadence_days,
                last_interaction_ts
            FROM contacts
            WHERE workspace_id = ?
              AND reminder_cadence_days IS NOT NULL
              AND last_interaction_ts <= datetime('now', '-' || reminder_cadence_days || ' days')
            """,
            (workspace_id,),
        )

    async def create_draft(
        self,
        *,
        workspace_id: str,
        user_id: str,
        channel_id: str,
        action_type: str,
        payload: dict[str, Any],
        thread_ts: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        draft_id = str(uuid.uuid4())
        expiry = expires_at or format_ts(utc_now() + timedelta(hours=24))
        await self.connection.execute(
            """
            INSERT INTO action_drafts (
                id, workspace_id, user_id, channel_id, thread_ts, action_type, payload, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                workspace_id,
                user_id,
                channel_id,
                thread_ts,
                action_type,
                json.dumps(payload),
                expiry,
            ),
        )
        await self.connection.commit()
        return draft_id

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM action_drafts WHERE id = ?", (draft_id,))
        if row is None:
            return None
        row["payload"] = json.loads(row["payload"])
        return row

    async def cas_approve(self, draft_id: str) -> bool:
        cursor = await self.connection.execute(
            """
            UPDATE action_drafts
            SET status = 'APPROVED'
            WHERE id = ? AND status = 'PENDING'
            """,
            (draft_id,),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def mark_executed(self, draft_id: str) -> None:
        await self.connection.execute(
            """
            UPDATE action_drafts
            SET executed_at = ?
            WHERE id = ? AND status = 'APPROVED' AND executed_at IS NULL
            """,
            (format_ts(), draft_id),
        )
        await self.connection.commit()

    async def mark_failed(self, draft_id: str) -> None:
        await self.connection.execute(
            """
            UPDATE action_drafts
            SET status = 'FAILED'
            WHERE id = ? AND status = 'APPROVED' AND executed_at IS NULL
            """,
            (draft_id,),
        )
        await self.connection.commit()

    async def cancel_draft(self, draft_id: str) -> bool:
        cursor = await self.connection.execute(
            """
            UPDATE action_drafts
            SET status = 'CANCELLED'
            WHERE id = ? AND status = 'PENDING'
            """,
            (draft_id,),
        )
        await self.connection.commit()
        return cursor.rowcount == 1

    async def update_draft_content(self, draft_id: str, staged_content: str) -> bool:
        draft = await self.get_draft(draft_id)
        if draft is None or draft["status"] != "PENDING":
            return False
        payload = draft["payload"]
        payload["staged_content"] = staged_content
        await self.connection.execute(
            "UPDATE action_drafts SET payload = ? WHERE id = ? AND status = 'PENDING'",
            (json.dumps(payload), draft_id),
        )
        await self.connection.commit()
        return True

    async def enqueue_briefing(
        self,
        *,
        workspace_id: str,
        user_id: str,
        kind: str,
        summary: str,
        interaction_id: str | None = None,
        contact_id: str | None = None,
    ) -> str:
        item_id = str(uuid.uuid4())
        await self.connection.execute(
            """
            INSERT INTO briefing_items (
                id, workspace_id, user_id, kind, interaction_id, contact_id, summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, workspace_id, user_id, kind, interaction_id, contact_id, summary),
        )
        await self.connection.commit()
        return item_id

    async def list_queued_briefings(self, workspace_id: str) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT * FROM briefing_items
            WHERE workspace_id = ? AND status = 'QUEUED'
            ORDER BY created_at
            """,
            (workspace_id,),
        )

    async def mark_briefing_delivered(self, item_id: str) -> None:
        await self.connection.execute(
            "UPDATE briefing_items SET status = 'DELIVERED' WHERE id = ?",
            (item_id,),
        )
        await self.connection.commit()

    async def _one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        cursor = await self.connection.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return _public_row(row)

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = await self.connection.execute(sql, params)
        rows = await cursor.fetchall()
        return [_public_row(row) for row in rows]


def unpack_public(blob: bytes) -> list[float]:
    from knappy.db.vectors import unpack_embedding

    return unpack_embedding(blob)


def _public_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data.pop("embedding", None)
    return data


def assert_schema_complete(names: set[str]) -> None:
    missing = EXPECTED_TABLES - names
    if missing:
        raise AssertionError(f"Missing tables: {sorted(missing)}")
