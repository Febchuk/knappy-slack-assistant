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

    dialect = "sqlite"

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
        await self._migrate_owner_user_id()
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
        owner_user_id: str = "",
        commit: bool = True,
    ) -> str:
        contact_id = str(uuid.uuid4())
        touched = last_interaction_ts or format_ts()
        cursor = await self.connection.execute(
            """
            INSERT INTO contacts (
                id, workspace_id, name, slack_user_id, email, company, role,
                reminder_cadence_days, last_interaction_ts, owner_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, owner_user_id, name) DO UPDATE SET
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
                owner_user_id,
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
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["workspace_id = ?"]
        params: list[Any] = [workspace_id]
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
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
        owner_user_id: str = "",
        commit: bool = True,
    ) -> str:
        interaction_id = str(uuid.uuid4())
        blob = self._store_embedding(embedding)
        placeholder = "CAST(? AS vector)" if self.dialect == "postgres" else "?"
        await self.connection.execute(
            f"""
            INSERT INTO interactions (
                id, workspace_id, contact_id, source_type, channel_id, thread_ts,
                raw_text, summary, commitment, due_date, status, embedding, owner_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {placeholder}, ?)
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
                owner_user_id,
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
        owner_user_id: str = "",
    ) -> tuple[str, str]:
        try:
            contact_id = await self.upsert_contact(
                workspace_id,
                contact_name,
                email=contact_email,
                company=company,
                last_interaction_ts=last_interaction_ts,
                owner_user_id=owner_user_id,
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
                owner_user_id=owner_user_id,
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
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["embedding IS NOT NULL"]
        params: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            params.append(workspace_id)
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        cursor = await self.connection.execute(
            f"SELECT * FROM interactions WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        rows = await cursor.fetchall()
        scored: list[tuple[float, aiosqlite.Row]] = []
        for row in rows:
            blob = row["embedding"]
            vector = coerce_embedding(blob)
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
        owner_user_id: str | None = None,
        match_text: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["i.workspace_id = ?", "i.commitment IS NOT NULL"]
        params: list[Any] = [workspace_id]
        if owner_user_id is not None:
            clauses.append("c.owner_user_id = ?")
            params.append(owner_user_id)
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
                distance = cosine_distance(query_embedding, coerce_embedding(row["embedding"]))
            if match_text and overlap == 0 and distance > 0.25:
                continue
            due = item.get("due_date") or "9999-99-99 99:99:99"
            sort_key = (due, distance) if not match_text else (-overlap, distance)
            ranked.append((sort_key, item))
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
        if self.dialect == "postgres":
            due_sql = "i.due_date <= NOW() + (? * INTERVAL '1 hour')"
            alert_sql = "i.last_alerted_at < NOW() - INTERVAL '24 hours'"
            due_param: Any = within_hours
        else:
            due_sql = "i.due_date <= datetime('now', ?)"
            alert_sql = "i.last_alerted_at < datetime('now', '-24 hours')"
            due_param = f"+{within_hours} hours"
        return await self._all(
            f"""
            SELECT
                c.id AS contact_id,
                c.name AS contact_name,
                c.company AS company,
                c.slack_user_id,
                c.owner_user_id,
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
              AND {due_sql}
              AND (i.last_alerted_at IS NULL OR {alert_sql})
            """,
            (workspace_id, due_param),
        )

    async def scan_dormant_contacts(self, workspace_id: str) -> list[dict[str, Any]]:
        if self.dialect == "postgres":
            stale = "last_interaction_ts <= NOW() - (reminder_cadence_days * INTERVAL '1 day')"
        else:
            stale = "last_interaction_ts <= datetime('now', '-' || reminder_cadence_days || ' days')"
        return await self._all(
            f"""
            SELECT
                id AS contact_id,
                name AS contact_name,
                company,
                slack_user_id,
                owner_user_id,
                reminder_cadence_days,
                last_interaction_ts
            FROM contacts
            WHERE workspace_id = ?
              AND reminder_cadence_days IS NOT NULL
              AND {stale}
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
        stored_payload: Any = payload if self.dialect == "postgres" else json.dumps(payload)
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
                stored_payload,
                expiry,
            ),
        )
        await self.connection.commit()
        return draft_id

    async def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM action_drafts WHERE id = ?", (draft_id,))
        if row is None:
            return None
        if isinstance(row["payload"], str):
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
        stored_payload: Any = payload if self.dialect == "postgres" else json.dumps(payload)
        await self.connection.execute(
            "UPDATE action_drafts SET payload = ? WHERE id = ? AND status = 'PENDING'",
            (stored_payload, draft_id),
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
        owner_user_id: str | None = None,
    ) -> str:
        item_id = str(uuid.uuid4())
        owner = user_id if owner_user_id is None else owner_user_id
        await self.connection.execute(
            """
            INSERT INTO briefing_items (
                id, workspace_id, user_id, kind, interaction_id, contact_id, summary, owner_user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, workspace_id, user_id, kind, interaction_id, contact_id, summary, owner),
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

    async def add_model_usage(
        self,
        workspace_id: str,
        owner_user_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        await self.connection.execute(
            """
            INSERT INTO model_usage (workspace_id, owner_user_id, day, calls, input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT (workspace_id, owner_user_id, day) DO UPDATE SET
                calls = model_usage.calls + 1,
                input_tokens = model_usage.input_tokens + excluded.input_tokens,
                output_tokens = model_usage.output_tokens + excluded.output_tokens,
                cost_usd = model_usage.cost_usd + excluded.cost_usd
            """,
            (workspace_id, owner_user_id, utc_now().date().isoformat(), input_tokens, output_tokens, cost_usd),
        )
        await self.connection.commit()

    async def spend_today(self, workspace_id: str, owner_user_id: str) -> float:
        row = await self._one(
            "SELECT cost_usd FROM model_usage WHERE workspace_id = ? AND owner_user_id = ? AND day = ?",
            (workspace_id, owner_user_id, utc_now().date().isoformat()),
        )
        return float(row["cost_usd"]) if row else 0.0

    def _store_embedding(self, embedding: list[float] | None) -> Any:
        if embedding is None:
            return None
        if self.dialect == "postgres":
            return "[" + ",".join(format(float(value), ".8g") for value in embedding) + "]"
        return pack_embedding(embedding)

    async def _migrate_owner_user_id(self) -> None:
        if self.dialect != "sqlite":
            return
        cursor = await self.connection.execute("PRAGMA table_info(contacts)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "owner_user_id" in columns:
            return
        await self.connection.execute("PRAGMA foreign_keys = OFF")
        await self.connection.executescript(
            """
            CREATE TABLE contacts_owner (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                slack_user_id TEXT,
                email TEXT,
                company TEXT,
                role TEXT,
                reminder_cadence_days INTEGER DEFAULT 30,
                last_interaction_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                owner_user_id TEXT NOT NULL DEFAULT '',
                UNIQUE(workspace_id, owner_user_id, name)
            );
            INSERT INTO contacts_owner (
                id, workspace_id, name, slack_user_id, email, company, role,
                reminder_cadence_days, last_interaction_ts, created_at, owner_user_id
            )
            SELECT
                id, workspace_id, name, slack_user_id, email, company, role,
                reminder_cadence_days, last_interaction_ts, created_at, ''
            FROM contacts;
            DROP TABLE contacts;
            ALTER TABLE contacts_owner RENAME TO contacts;

            CREATE TABLE interactions_owner (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
                source_type TEXT NOT NULL CHECK(source_type IN ('DIRECT_DM', 'APP_MENTION', 'NOTE_INGEST')),
                channel_id TEXT NOT NULL,
                thread_ts TEXT,
                raw_text TEXT NOT NULL,
                summary TEXT NOT NULL,
                commitment TEXT,
                due_date DATETIME,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'EXPIRED')),
                embedding BLOB,
                last_alerted_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                owner_user_id TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO interactions_owner (
                id, workspace_id, contact_id, source_type, channel_id, thread_ts,
                raw_text, summary, commitment, due_date, status, embedding, last_alerted_at, created_at, owner_user_id
            )
            SELECT
                id, workspace_id, contact_id, source_type, channel_id, thread_ts,
                raw_text, summary, commitment, due_date, status, embedding, last_alerted_at, created_at, ''
            FROM interactions;
            DROP TABLE interactions;
            ALTER TABLE interactions_owner RENAME TO interactions;

            CREATE TABLE briefing_items_owner (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('COMMITMENT', 'CADENCE')),
                interaction_id TEXT REFERENCES interactions(id) ON DELETE CASCADE,
                contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED', 'DELIVERED', 'DISMISSED')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                owner_user_id TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO briefing_items_owner (
                id, workspace_id, user_id, kind, interaction_id, contact_id, summary, status, created_at, owner_user_id
            )
            SELECT
                id, workspace_id, user_id, kind, interaction_id, contact_id, summary, status, created_at, user_id
            FROM briefing_items;
            DROP TABLE briefing_items;
            ALTER TABLE briefing_items_owner RENAME TO briefing_items;

            CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
            CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
            CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
            CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
            CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
            """
        )
        await self.connection.execute("PRAGMA foreign_keys = ON")

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


def coerce_embedding(value: Any) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    if isinstance(value, str):
        text = value.strip().strip("[]")
        if not text:
            return []
        return [float(item) for item in text.split(",")]
    return unpack_public(bytes(value))


def _public_row(row: aiosqlite.Row) -> dict[str, Any]:
    data = dict(row)
    data.pop("embedding", None)
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = format_ts(value)
    return data


def assert_schema_complete(names: set[str]) -> None:
    missing = EXPECTED_TABLES - names
    if missing:
        raise AssertionError(f"Missing tables: {sorted(missing)}")
