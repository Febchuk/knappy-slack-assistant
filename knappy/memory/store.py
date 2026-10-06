"""Persistent memory: the turn log, versioned records, the ledger, provenance, and the profile (Spec 13 §2).

Everything here is deterministic SQL. Model calls live in engine.py. Every query is scoped to one
workspace and one owner. Methods that write several rows expect to run inside repo.transaction().
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import dropwhile
from typing import Any

import numpy as np

from knappy.agent.session import WINDOW_TURNS, Turn
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.db.vectors import cosine_distance
from knappy.ingestion.embed import generate_embedding, semantic

DM_SEGMENT_GAP = timedelta(hours=6)
RECAP_SLIDE = 10
RECENT = timedelta(days=14)
SEMANTIC_MIN = 0.6
PROFILE_CHAR_BUDGET = 6000  # about 1,500 tokens
TOOL_TURN_LIMIT = 4096

RECORD_COLUMNS = (
    "id, type, title, aliases, body, links, source, contact_id, status, supersedes, valid_from, expires_at, updated_at"
)
PROFILE_TYPES = ("person", "org", "fact", "preference", "decision")
DERIVED_SOURCES = ("reconciler", "remember")

_STOP = frozenset(
    "a an and are as at be but by do for from has have i in is it me my of on or our so that the this to was we what"
    " when where who why will with you your about did does know think".split()
)
LINK = re.compile(r"\[\[([^\]]+)\]\]")
_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(password|passcode|passwd|pin|api[ _-]?key|secret|token)(\s*(?:is|:|=)\s*)\S+"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b"),
]
REDACTED = "[redacted secret]"


def redact(text: str) -> str:
    """Strip anything that looks like a credential. Defense in depth behind the reconciler prompt."""
    for pattern in _SECRETS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48].strip("-") or "note"


def search_terms(query: str) -> list[str]:
    terms = [term for term in re.findall(r"[a-z0-9]+", query.lower()) if len(term) > 1 and term not in _STOP]
    return list(dict.fromkeys(terms))


def summarize_body(body: str, limit: int = 200) -> str:
    lines = [line.strip().lstrip("-*•").strip() for line in body.splitlines()]
    text = "; ".join(line for line in lines if line)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class LoggedTurn:
    id: str
    seq: int
    key: str
    role: str
    text: str
    at: datetime


@dataclass(frozen=True)
class ConversationView:
    """One conversation as the prompt sees it: the window, and older turns no recap covers yet."""

    window: list[LoggedTurn]
    uncovered: list[LoggedTurn]
    recap: str | None
    new_segment: bool

    @property
    def needs_recap(self) -> bool:
        return len(self.uncovered) >= RECAP_SLIDE or (bool(self.uncovered) and self.new_segment)


@dataclass
class Cascade:
    """What a forget touched. Records in `repass` keep surviving sources but cite forgotten content."""

    forgotten: list[str] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    # Open commitments the reconciler made from retracted events, with nothing else standing behind them.
    cancelled: list[str] = field(default_factory=list)
    repass: dict[str, list[str]] = field(default_factory=dict)
    surviving: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


class MemoryStore:
    def __init__(
        self, repo: SqliteRepository, workspace_id: str, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.clock = clock

    @property
    def _db(self) -> Any:
        return self.repo.connection

    async def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = await self._db.execute(sql, tuple(params))
        return [dict(row) for row in await cursor.fetchall()]

    async def _one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = await self._all(sql, params)
        return rows[0] if rows else None

    async def _run(self, sql: str, params: Iterable[Any] = ()) -> int:
        cursor = await self._db.execute(sql, tuple(params))
        return cursor.rowcount

    async def append(self, owner: str, key: str, turn: Turn, slack_ts: str | None = None) -> str:
        turn_id = uuid.uuid4().hex
        content = turn.text if turn.role != "tool" else turn.text[:TOOL_TURN_LIMIT]
        async with self.repo.transaction():
            await self._run(
                """
                INSERT INTO conversation_turns
                    (id, workspace_id, owner_user_id, conversation_key, role, content, slack_ts, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (turn_id, self.workspace_id, owner, key, turn.role, content, slack_ts, format_ts(self.clock())),
            )
        return turn_id

    async def window(self, owner: str, key: str, limit: int = WINDOW_TURNS) -> list[Turn]:
        view = await self.conversation_view(owner, key, limit=limit)
        return [Turn(turn.role, turn.text) for turn in view.window]  # type: ignore[arg-type]

    async def conversation_view(self, owner: str, key: str, limit: int = WINDOW_TURNS) -> ConversationView:
        recap = await self._one(
            "SELECT body, through_turn_id, updated_at FROM conversation_recaps WHERE owner_user_id = ? AND conversation_key = ?",
            (owner, key),
        )
        after = 0
        if recap is not None:
            through = await self._one("SELECT seq FROM conversation_turns WHERE id = ?", (recap["through_turn_id"],))
            if through is not None:
                after = int(through["seq"])
            else:
                row = await self._one(
                    "SELECT MAX(seq) AS seq FROM conversation_turns WHERE owner_user_id = ? AND conversation_key = ? AND created_at <= ?",
                    (owner, key, recap["updated_at"]),
                )
                after = int(row["seq"] or 0) if row else 0
        # A forget clears short-term context: earlier answers, and its confirmation, may restate what was forgotten.
        forgot_at = await self._last_forget_seq(owner)
        rows = await self._all(
            """
            SELECT id, seq, conversation_key, role, content, created_at FROM conversation_turns
            WHERE workspace_id = ? AND owner_user_id = ? AND conversation_key = ? AND seq > ?
              AND role IN ('user', 'assistant')
            ORDER BY seq
            """,
            (self.workspace_id, owner, key, max(after, forgot_at)),
        )
        logged = _logged(rows)
        if forgot_at > after:
            logged = list(dropwhile(lambda turn: turn.role == "assistant", logged))
        turns = _without_retracted(logged, await self.retracted_turn_ids(owner))
        start = 0
        if key.startswith("dm:") and turns:
            for index in range(1, len(turns)):
                if turns[index].at - turns[index - 1].at > DM_SEGMENT_GAP:
                    start = index
            if self.clock() - turns[-1].at > DM_SEGMENT_GAP:
                start = len(turns)
        window_start = max(start, len(turns) - limit)
        return ConversationView(
            window=turns[window_start:],
            uncovered=turns[:window_start],
            recap=recap["body"] if recap else None,
            new_segment=window_start == start and start > 0,
        )

    async def _prompt_of(self, owner: str, reply_id: str) -> str | None:
        """The user turn an assistant reply answered: the turn before it in the same conversation."""
        row = await self._one(
            """
            SELECT prev.id FROM conversation_turns reply
            JOIN conversation_turns prev ON prev.owner_user_id = reply.owner_user_id
             AND prev.conversation_key = reply.conversation_key AND prev.role IN ('user', 'assistant')
            WHERE reply.owner_user_id = ? AND reply.id = ? AND prev.seq < reply.seq
            ORDER BY prev.seq DESC LIMIT 1
            """,
            (owner, reply_id),
        )
        return row["id"] if row else None

    async def _last_forget_seq(self, owner: str) -> int:
        row = await self._one(
            """
            SELECT MAX(t.seq) AS seq FROM conversation_turns t
            JOIN memory_provenance p ON p.owner_user_id = t.owner_user_id AND p.source_type = 'turn' AND p.source_id = t.id
            JOIN memory_events e ON e.id = p.target_id
            WHERE t.workspace_id = ? AND t.owner_user_id = ? AND p.target_type = 'event' AND e.kind = 'forgotten'
            """,
            (self.workspace_id, owner),
        )
        return int(row["seq"] or 0) if row else 0

    async def save_recap(self, owner: str, key: str, body: str, through_turn_id: str) -> None:
        async with self.repo.transaction():
            await self._run(
                """
                INSERT INTO conversation_recaps (owner_user_id, conversation_key, body, through_turn_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (owner_user_id, conversation_key) DO UPDATE SET
                    body = excluded.body, through_turn_id = excluded.through_turn_id, updated_at = excluded.updated_at
                """,
                (owner, key, redact(body), through_turn_id, format_ts(self.clock())),
            )

    async def retracted_turn_ids(self, owner: str, *, with_forget_requests: bool = False) -> set[str]:
        """Turns that are sources of retracted events. The reconciler also skips the forget requests themselves."""
        kinds = "e.status = 'RETRACTED' OR e.kind = 'forgotten'" if with_forget_requests else "e.status = 'RETRACTED'"
        rows = await self._all(
            f"""
            SELECT p.source_id FROM memory_provenance p
            JOIN memory_events e ON e.id = p.target_id
            WHERE p.owner_user_id = ? AND p.target_type = 'event' AND p.source_type = 'turn' AND ({kinds})
            """,
            (owner,),
        )
        return {row["source_id"] for row in rows}

    async def remembered(self, owner: str, turn_ids: list[str]) -> dict[str, list[str]]:
        """Records each turn already produced through the remember tool, so the reconciler does not duplicate them."""
        if not turn_ids:
            return {}
        rows = await self._all(
            f"""
            SELECT turn.source_id AS turn_id, record.target_id AS record_id
            FROM memory_provenance turn
            JOIN memory_provenance record
              ON record.owner_user_id = turn.owner_user_id AND record.target_type = 'record'
             AND record.source_type = 'event' AND record.source_id = turn.target_id
            JOIN memory_records r ON r.owner_user_id = record.owner_user_id AND r.id = record.target_id
            WHERE turn.owner_user_id = ? AND turn.target_type = 'event' AND turn.source_type = 'turn'
              AND r.source = 'remember' AND turn.source_id IN ({', '.join('?' for _ in turn_ids)})
            """,
            (owner, *turn_ids),
        )
        saved: dict[str, list[str]] = {}
        for row in rows:
            saved.setdefault(row["turn_id"], []).append(row["record_id"])
        return saved

    async def turns(
        self, owner: str, *, since: datetime | None = None, upto: datetime | None = None, unreconciled: bool = False
    ) -> list[LoggedTurn]:
        clauses = ["workspace_id = ?", "owner_user_id = ?"]
        params: list[Any] = [self.workspace_id, owner]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(format_ts(since))
        if upto is not None:
            clauses.append("created_at <= ?")
            params.append(format_ts(upto))
        if unreconciled:
            clauses.append("reconciled_at IS NULL")
        rows = await self._all(
            f"SELECT id, seq, conversation_key, role, content, created_at FROM conversation_turns WHERE {' AND '.join(clauses)} ORDER BY seq",
            params,
        )
        return _logged(rows)

    async def idle_keys(self, owner: str, now: datetime, idle: timedelta) -> list[str]:
        rows = await self._all(
            """
            SELECT conversation_key, MAX(created_at) AS last_at,
                   SUM(CASE WHEN reconciled_at IS NULL THEN 1 ELSE 0 END) AS pending
            FROM conversation_turns
            WHERE workspace_id = ? AND owner_user_id = ? AND created_at <= ?
            GROUP BY conversation_key
            """,
            (self.workspace_id, owner, format_ts(now)),
        )
        cutoff = format_ts(now - idle)
        return [row["conversation_key"] for row in rows if row["pending"] and row["last_at"] <= cutoff]

    async def mark_reconciled(self, turn_ids: list[str], now: datetime) -> None:
        for turn_id in turn_ids:
            await self._run("UPDATE conversation_turns SET reconciled_at = ? WHERE id = ?", (format_ts(now), turn_id))

    async def search_conversations(
        self, owner: str, query: str, since: datetime | None = None, limit: int = 10, exclude: str | None = None
    ) -> list[dict[str, Any]]:
        terms = search_terms(query)
        if not terms:
            return []
        clauses = "t.workspace_id = ? AND t.owner_user_id = ? AND t.role IN ('user', 'assistant')"
        params: list[Any] = [self.workspace_id, owner]
        if since is not None:
            clauses += " AND t.created_at >= ?"
            params.append(format_ts(since))
        if self.repo.dialect == "postgres":
            sql = f"""
                SELECT t.id, t.conversation_key, t.role, t.content, t.created_at FROM conversation_turns t
                WHERE t.search @@ to_tsquery('english', ?) AND {clauses}
                ORDER BY ts_rank(t.search, to_tsquery('english', ?)) DESC LIMIT 50
            """
            params = [" | ".join(terms), *params, " | ".join(terms)]
        else:
            sql = f"""
                SELECT t.id, t.conversation_key, t.role, t.content, t.created_at
                FROM turns_fts JOIN conversation_turns t ON t.seq = turns_fts.rowid
                WHERE turns_fts MATCH ? AND {clauses}
                ORDER BY bm25(turns_fts) LIMIT 50
            """
            params = [" OR ".join(f'"{term}"' for term in terms), *params]
        hidden = await self.retracted_turn_ids(owner, with_forget_requests=True)
        hits = [row for row in await self._all(sql, params) if row["id"] not in hidden and row["id"] != exclude]
        if hidden:
            hits = [row for row in hits if row["role"] != "assistant" or await self._prompt_of(owner, row["id"]) not in hidden]
        return [
            {
                "conversation": row["conversation_key"],
                "role": row["role"],
                "date": str(row["created_at"])[:16] + " UTC",
                "text": row["content"][:500],
            }
            for row in hits[:limit]
        ]

    async def delete_reconciled_turns(self, owner: str, before: datetime) -> int:
        return await self._run(
            "DELETE FROM conversation_turns WHERE workspace_id = ? AND owner_user_id = ? AND reconciled_at IS NOT NULL AND created_at < ?",
            (self.workspace_id, owner, format_ts(before)),
        )

    async def owners(self) -> list[str]:
        rows = await self._all(
            """
            SELECT owner_user_id FROM conversation_turns WHERE workspace_id = ?
            UNION SELECT owner_user_id FROM user_profile WHERE workspace_id = ?
            """,
            (self.workspace_id, self.workspace_id),
        )
        return sorted(row["owner_user_id"] for row in rows if row["owner_user_id"])

    async def get_record(self, owner: str, record_id: str) -> dict[str, Any] | None:
        return await self._one(
            f"SELECT {RECORD_COLUMNS} FROM memory_records WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
            (self.workspace_id, owner, record_id),
        )

    async def active_records(self, owner: str, types: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {RECORD_COLUMNS} FROM memory_records WHERE workspace_id = ? AND owner_user_id = ? AND status = 'ACTIVE'"
        params: list[Any] = [self.workspace_id, owner]
        kinds = list(types or [])
        if kinds:
            sql += f" AND type IN ({', '.join('?' for _ in kinds)})"
            params.extend(kinds)
        return await self._all(sql + " ORDER BY updated_at DESC, seq DESC", params)

    async def free_id(self, owner: str, record_type: str, title: str) -> str:
        base = f"{record_type}:{slugify(title)}"
        candidate, suffix = base, 2
        while await self.get_record(owner, candidate) is not None:
            candidate, suffix = f"{base}-{suffix}", suffix + 1
        return candidate

    async def create_record(
        self,
        owner: str,
        *,
        record_id: str,
        type: str,
        title: str,
        body: str,
        source: str,
        now: datetime,
        sources: list[tuple[str, str]],
        aliases: Iterable[str] = (),
        expires_at: datetime | None = None,
        contact_id: str | None = None,
    ) -> str:
        title, body, alias_text = redact(title), redact(body), redact(", ".join(aliases))
        await self._run(
            """
            INSERT INTO memory_records (
                id, workspace_id, owner_user_id, type, title, aliases, body, links, source, contact_id,
                valid_from, expires_at, updated_at, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id, self.workspace_id, owner, type, title, alias_text, body, _links(body), source, contact_id,
                format_ts(now), format_ts(expires_at) if expires_at else None, format_ts(now),
                _embedding(title, alias_text, body),
            ),
        )
        await self.add_provenance(owner, "record", record_id, sources)
        return record_id

    async def revise_record(
        self,
        owner: str,
        record_id: str,
        *,
        now: datetime,
        sources: list[tuple[str, str]],
        keep_sources: bool,
        title: str | None = None,
        aliases: Iterable[str] | None = None,
        body: str | None = None,
        expires_at: datetime | None = None,
        old_status: str = "SUPERSEDED",
    ) -> str:
        """New version at the stable id. The old row is copied to {id}@v{n} (Spec 13 §2)."""
        current = await self.get_record(owner, record_id)
        if current is None:
            raise KeyError(record_id)
        version = f"{record_id}@v{_version_number(current['supersedes']) + 1}"
        await self._run(
            """
            INSERT INTO memory_records (
                id, workspace_id, owner_user_id, type, title, aliases, body, links, source, contact_id,
                status, supersedes, valid_from, expires_at, updated_at, embedding
            )
            SELECT ?, workspace_id, owner_user_id, type, title, aliases, body, links, source, contact_id,
                   ?, supersedes, valid_from, expires_at, ?, embedding
            FROM memory_records WHERE workspace_id = ? AND owner_user_id = ? AND id = ?
            """,
            (version, old_status, format_ts(now), self.workspace_id, owner, record_id),
        )
        await self._run(
            """
            INSERT INTO memory_provenance (target_type, target_id, source_type, source_id, owner_user_id)
            SELECT target_type, ?, source_type, source_id, owner_user_id FROM memory_provenance
            WHERE owner_user_id = ? AND target_type = 'record' AND target_id = ?
            """,
            (version, owner, record_id),
        )
        if not keep_sources:
            await self._run(
                "DELETE FROM memory_provenance WHERE owner_user_id = ? AND target_type = 'record' AND target_id = ?",
                (owner, record_id),
            )
        new_title = redact(title) if title else current["title"]
        new_aliases = redact(", ".join(aliases)) if aliases else current["aliases"]
        new_body = redact(body) if body is not None else current["body"]
        new_expiry = format_ts(expires_at) if expires_at else current["expires_at"]
        await self._run(
            """
            UPDATE memory_records SET title = ?, aliases = ?, body = ?, links = ?, status = 'ACTIVE', supersedes = ?,
                valid_from = ?, expires_at = ?, updated_at = ?, embedding = ?
            WHERE workspace_id = ? AND owner_user_id = ? AND id = ?
            """,
            (
                new_title, new_aliases, new_body, _links(new_body), version, format_ts(now), new_expiry,
                format_ts(now), _embedding(new_title, new_aliases, new_body), self.workspace_id, owner, record_id,
            ),
        )
        await self.add_provenance(owner, "record", record_id, sources)
        return record_id

    async def set_status(self, owner: str, record_ids: Iterable[str], status: str, now: datetime) -> None:
        for record_id in record_ids:
            await self._run(
                "UPDATE memory_records SET status = ?, updated_at = ? WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
                (status, format_ts(now), self.workspace_id, owner, record_id),
            )

    async def versions(self, owner: str, record_id: str) -> list[dict[str, Any]]:
        """The record and its prior versions, newest first, following supersedes."""
        chain: list[dict[str, Any]] = []
        current = await self.get_record(owner, record_id)
        while current is not None and len(chain) < 100:
            chain.append(current)
            current = await self.get_record(owner, current["supersedes"]) if current["supersedes"] else None
        return chain

    async def expire_records(self, owner: str, now: datetime) -> list[str]:
        rows = await self._all(
            """
            SELECT id FROM memory_records WHERE workspace_id = ? AND owner_user_id = ? AND status = 'ACTIVE'
              AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (self.workspace_id, owner, format_ts(now)),
        )
        ids = [row["id"] for row in rows]
        await self.set_status(owner, ids, "EXPIRED", now)
        return ids

    async def search(
        self, owner: str, query: str, *, types: Iterable[str] = (), limit: int = 8, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Ranked per Spec 13 §5: text rank (title and aliases 3x body) + recency + semantic similarity."""
        now = now or self.clock()
        kinds = list(types)
        type_sql = f" AND r.type IN ({', '.join('?' for _ in kinds)})" if kinds else ""
        scope = [self.workspace_id, owner, *kinds]
        terms = search_terms(query)
        if not terms:
            rows = await self.active_records(owner, kinds)
            return [_hit(row) for row in rows[:limit]]
        text_scores: dict[str, float] = {}
        if terms:
            text_scores = await self._text_scores(terms, type_sql, scope)
        top = max(text_scores.values(), default=0.0)
        scores = {record_id: score / top for record_id, score in text_scores.items()} if top > 0 else {}
        if semantic():
            vector = generate_embedding(query, query=True)
            for record_id, similarity in await self._similar(vector, type_sql, scope):
                scores[record_id] = scores.get(record_id, 0.0) + similarity
        if not scores:
            return []
        rows = {
            row["id"]: row
            for row in await self._all(
                f"""
                SELECT {RECORD_COLUMNS} FROM memory_records r
                WHERE r.workspace_id = ? AND r.owner_user_id = ? AND r.status = 'ACTIVE'
                  AND r.id IN ({', '.join('?' for _ in scores)})
                """,
                (self.workspace_id, owner, *scores),
            )
        }
        recent = format_ts(now - RECENT)
        ranked = sorted(
            ((score + (0.25 if rows[rid]["updated_at"] >= recent else 0.0), rid) for rid, score in scores.items() if rid in rows),
            reverse=True,
        )
        return [{**_hit(rows[rid]), "score": round(score, 3)} for score, rid in ranked[:limit]]

    async def superseded_matches(self, owner: str, query: str, *, types: Iterable[str], limit: int = 10) -> list[str]:
        """Earlier versions of records whose text mentions any query term, newest first."""
        terms = search_terms(query)
        kinds = list(types)
        if not terms or not kinds:
            return []
        matches = " OR ".join("LOWER(title || ' ' || aliases || ' ' || body) LIKE ?" for _ in terms)
        rows = await self._all(
            f"""
            SELECT id FROM memory_records
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'SUPERSEDED'
              AND type IN ({', '.join('?' for _ in kinds)}) AND ({matches})
            ORDER BY updated_at DESC LIMIT ?
            """,
            (self.workspace_id, owner, *kinds, *[f"%{term}%" for term in terms], limit),
        )
        return [row["id"] for row in rows]

    async def _text_scores(self, terms: list[str], type_sql: str, scope: list[Any]) -> dict[str, float]:
        if self.repo.dialect == "postgres":
            query = " | ".join(terms)
            rows = await self._all(
                f"""
                SELECT r.id, ts_rank('{{0.33, 0.33, 0.33, 1.0}}'::float4[], r.search, to_tsquery('english', ?)) AS score
                FROM memory_records r
                WHERE r.search @@ to_tsquery('english', ?)
                  AND r.workspace_id = ? AND r.owner_user_id = ? AND r.status = 'ACTIVE'{type_sql}
                ORDER BY score DESC LIMIT 50
                """,
                (query, query, *scope),
            )
        else:
            rows = await self._all(
                f"""
                SELECT r.id, -bm25(memory_fts, 3.0, 3.0, 1.0) AS score
                FROM memory_fts JOIN memory_records r ON r.seq = memory_fts.rowid
                WHERE memory_fts MATCH ?
                  AND r.workspace_id = ? AND r.owner_user_id = ? AND r.status = 'ACTIVE'{type_sql}
                ORDER BY score DESC LIMIT 50
                """,
                (" OR ".join(f'"{term}"' for term in terms), *scope),
            )
        return {row["id"]: float(row["score"]) for row in rows}

    async def _similar(self, vector: list[float] | None, type_sql: str, scope: list[Any]) -> list[tuple[str, float]]:
        if vector is None:
            return []
        rows = await self._all(
            f"""
            SELECT r.id, r.embedding FROM memory_records r
            WHERE r.workspace_id = ? AND r.owner_user_id = ? AND r.status = 'ACTIVE' AND r.embedding IS NOT NULL{type_sql}
            """,
            scope,
        )
        scored = []
        for row in rows:
            similarity = 1.0 - cosine_distance(vector, np.frombuffer(bytes(row["embedding"]), dtype=np.float32))
            if similarity >= SEMANTIC_MIN:
                scored.append((row["id"], similarity))
        return sorted(scored, key=lambda item: -item[1])[:20]

    async def record_index(self, owner: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._all(
            """
            SELECT id, type, title, aliases FROM memory_records
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'ACTIVE' AND type NOT IN ('episode_daily', 'episode_weekly')
            ORDER BY updated_at DESC, seq DESC LIMIT ?
            """,
            (self.workspace_id, owner, limit),
        )
        return rows

    async def add_event(
        self,
        owner: str,
        *,
        kind: str,
        summary: str,
        occurred_at: datetime,
        score: float,
        now: datetime,
        sources: list[tuple[str, str]],
        commitment_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        await self._run(
            """
            INSERT INTO memory_events
                (id, workspace_id, owner_user_id, kind, summary, occurred_at, admission_score, commitment_id, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, self.workspace_id, owner, kind, redact(summary), format_ts(occurred_at), score, commitment_id,
                format_ts(now), json.dumps(metadata) if metadata else None,
            ),
        )
        await self.add_provenance(owner, "event", event_id, sources)
        return event_id

    async def link_events_to_commitment(self, event_ids: Iterable[str], commitment_id: str) -> None:
        for event_id in event_ids:
            await self._run("UPDATE memory_events SET commitment_id = ? WHERE id = ?", (commitment_id, event_id))

    async def add_provenance(self, owner: str, target_type: str, target_id: str, sources: Iterable[tuple[str, str]]) -> None:
        for source_type, source_id in sources:
            await self._run(
                """
                INSERT INTO memory_provenance (target_type, target_id, source_type, source_id, owner_user_id)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
                """,
                (target_type, target_id, source_type, source_id, owner),
            )

    async def sources(self, owner: str, target_type: str, target_id: str) -> list[tuple[str, str]]:
        rows = await self._all(
            "SELECT source_type, source_id FROM memory_provenance WHERE owner_user_id = ? AND target_type = ? AND target_id = ?",
            (owner, target_type, target_id),
        )
        return [(row["source_type"], row["source_id"]) for row in rows]

    async def source_events(self, owner: str, record_id: str) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT e.id, e.kind, e.summary, e.occurred_at, e.status, e.created_at, e.metadata FROM memory_provenance p
            JOIN memory_events e ON e.id = p.source_id
            WHERE p.owner_user_id = ? AND p.target_type = 'record' AND p.target_id = ? AND p.source_type = 'event'
            ORDER BY e.occurred_at
            """,
            (owner, record_id),
        )

    async def events_created_between(self, owner: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT id, kind, summary, occurred_at FROM memory_events
            WHERE workspace_id = ? AND owner_user_id = ? AND status = 'ACTIVE' AND kind != 'forgotten'
              AND created_at >= ? AND created_at < ?
            ORDER BY occurred_at
            """,
            (self.workspace_id, owner, format_ts(start), format_ts(end)),
        )

    async def cascade_forget(
        self, owner: str, record_ids: list[str], now: datetime, *, events: Iterable[str] = ()
    ) -> Cascade:
        """Steps 1-3 of the forget cascade (Spec 13 §3.2). Re-passing bodies needs the model, so the caller does it.

        `events` are retracted directly, for memory that came from somewhere other than a record (Spec 18 §7).
        """
        cascade = Cascade()
        dead_events: set[str] = set()
        dead_records: set[str] = set()
        summaries: dict[str, str] = {}
        for event_id in events:
            row = await self._one(
                "SELECT summary, status FROM memory_events WHERE owner_user_id = ? AND id = ?", (owner, event_id)
            )
            if row is None:
                continue
            if row["status"] == "ACTIVE":
                await self._run("UPDATE memory_events SET status = 'RETRACTED' WHERE id = ?", (event_id,))
                cascade.retracted.append(event_id)
            dead_events.add(event_id)
            summaries[event_id] = row["summary"]

        async def forget(record_id: str) -> None:
            for version in await self.versions(owner, record_id):
                dead_records.add(version["id"])
                summaries[version["id"]] = f"{version['title']}: {summarize_body(version['body'])}"
                await self.set_status(owner, [version["id"]], "FORGOTTEN", now)
                if version["type"] == "document":
                    await self._drop_documents(owner, version["id"])
            cascade.forgotten.append(record_id)

        for record_id in record_ids:
            await forget(record_id)
        # Forgetting only an earlier version keeps the events its current version still stands on.
        heads = {record_id.split("@", 1)[0] for record_id in record_ids} - dead_records
        protected = {source_id for head in heads for _, source_id in await self.sources(owner, "record", head)}
        for version_id in list(dead_records):
            for event in await self.source_events(owner, version_id):
                if event["id"] in protected:
                    continue
                if event["status"] == "ACTIVE":
                    await self._run("UPDATE memory_events SET status = 'RETRACTED' WHERE id = ?", (event["id"],))
                    cascade.retracted.append(event["id"])
                dead_events.add(event["id"])
                summaries[event["id"]] = event["summary"]

        cascade.cancelled = await self._cancel_unsourced_commitments(owner, cascade.retracted)
        changed: set[str] = set()
        frontier = set(dead_events) | dead_records
        while frontier:
            dependents = await self._dependents(owner, frontier)
            frontier = set()
            for record_id in dependents:
                if record_id in dead_records or record_id in changed:
                    continue
                cited = await self.sources(owner, "record", record_id)
                dead = {source_id for _, source_id in cited if source_id in dead_events or source_id in dead_records}
                tainted = [source_id for _, source_id in cited if source_id in dead or source_id in changed]
                if not tainted:
                    continue
                alive = [(kind, source_id) for kind, source_id in cited if source_id not in dead]
                if alive:
                    changed.add(record_id)
                    removed: list[str] = []
                    for source_id in tainted:
                        removed.extend([summaries[source_id]] if source_id in summaries else cascade.repass.get(source_id, []))
                    cascade.repass[record_id] = list(dict.fromkeys(removed))
                    cascade.surviving[record_id] = alive
                else:
                    await forget(record_id)
                frontier.add(record_id)
        settled = False
        while not settled:
            settled = True
            for record_id in list(cascade.repass):
                alive = [source for source in cascade.surviving[record_id] if source[1] not in dead_records]
                cascade.surviving[record_id] = alive
                if not alive:
                    del cascade.repass[record_id], cascade.surviving[record_id]
                    await forget(record_id)
                    settled = False
        return cascade

    async def _cancel_unsourced_commitments(self, owner: str, retracted: list[str]) -> list[str]:
        """Cancel open commitments whose every commitment_made event was retracted. Returns what they said."""
        if not retracted:
            return []
        marks = ", ".join("?" for _ in retracted)
        rows = await self._all(
            f"""
            SELECT DISTINCT CAST(i.id AS TEXT) AS id, i.commitment FROM interactions i
            JOIN memory_events made ON made.commitment_id = CAST(i.id AS TEXT)
            WHERE i.workspace_id = ? AND i.owner_user_id = ? AND i.status = 'PENDING'
              AND made.kind = 'commitment_made' AND made.id IN ({marks})
              AND NOT EXISTS (
                  SELECT 1 FROM memory_events e
                  WHERE e.commitment_id = CAST(i.id AS TEXT) AND e.kind = 'commitment_made' AND e.status = 'ACTIVE'
              )
            """,
            (self.workspace_id, owner, *retracted),
        )
        for row in rows:
            await self._run("UPDATE interactions SET status = 'CANCELLED' WHERE CAST(id AS TEXT) = ?", (row["id"],))
        return [row["commitment"] for row in rows]

    async def _drop_documents(self, owner: str, record_id: str) -> None:
        """A forgotten document record takes its stored text and chunks with it (Spec 15 §2.2)."""
        for kind, document_id in await self.sources(owner, "record", record_id):
            if kind != "document":
                continue
            scope = (self.workspace_id, owner, document_id)
            await self._run(
                "DELETE FROM document_chunks WHERE document_id IN "
                "(SELECT id FROM documents WHERE workspace_id = ? AND owner_user_id = ? AND id = ?)",
                scope,
            )
            await self._run("DELETE FROM documents WHERE workspace_id = ? AND owner_user_id = ? AND id = ?", scope)

    async def _dependents(self, owner: str, source_ids: set[str]) -> list[str]:
        ids = sorted(source_ids)
        rows = await self._all(
            f"""
            SELECT DISTINCT p.target_id FROM memory_provenance p
            JOIN memory_records r ON r.owner_user_id = p.owner_user_id AND r.id = p.target_id
            WHERE p.owner_user_id = ? AND p.target_type = 'record' AND r.workspace_id = ? AND r.status = 'ACTIVE'
              AND p.source_id IN ({', '.join('?' for _ in ids)})
            """,
            (owner, self.workspace_id, *ids),
        )
        return [row["target_id"] for row in rows]

    async def drop_recaps(self, owner: str) -> None:
        await self._run("DELETE FROM conversation_recaps WHERE owner_user_id = ?", (owner,))

    async def open_commitments(self, owner: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all(
            """
            SELECT CAST(i.id AS TEXT) AS id, i.commitment, i.due_date, i.next_check_at, i.waiting_on, c.name AS person
            FROM interactions i LEFT JOIN contacts c ON c.id = i.contact_id
            WHERE i.workspace_id = ? AND i.owner_user_id = ? AND i.status = 'PENDING' AND i.commitment IS NOT NULL
            ORDER BY i.created_at LIMIT ?
            """,
            (self.workspace_id, owner, limit),
        )

    async def get_commitment(self, owner: str, commitment_id: str) -> dict[str, Any] | None:
        return await self._one(
            """
            SELECT CAST(id AS TEXT) AS id, commitment, status FROM interactions
            WHERE workspace_id = ? AND owner_user_id = ? AND CAST(id AS TEXT) = ? AND commitment IS NOT NULL
            """,
            (self.workspace_id, owner, commitment_id),
        )

    async def set_commitment_plan(
        self,
        commitment_id: str,
        *,
        next_check_at: datetime | None,
        on_no_progress: str | None,
        waiting_on: str | None,
    ) -> None:
        await self._run(
            """
            UPDATE interactions SET
                next_check_at = COALESCE(?, next_check_at),
                on_no_progress = COALESCE(?, on_no_progress),
                waiting_on = COALESCE(?, waiting_on)
            WHERE CAST(id AS TEXT) = ?
            """,
            (format_ts(next_check_at) if next_check_at else None, on_no_progress, waiting_on, commitment_id),
        )

    async def finish_commitment(self, commitment_id: str) -> None:
        await self._run("UPDATE interactions SET status = 'FULFILLED' WHERE CAST(id AS TEXT) = ?", (commitment_id,))

    async def profile(self, owner: str) -> dict[str, Any] | None:
        return await self._one(
            """
            SELECT body, timezone, generated_at, nightly_on, brief_on, nudges_on, nudges_sent
            FROM user_profile WHERE workspace_id = ? AND owner_user_id = ?
            """,
            (self.workspace_id, owner),
        )

    async def _ensure_profile(self, owner: str) -> None:
        await self._run(
            """
            INSERT INTO user_profile (workspace_id, owner_user_id, body, generated_at) VALUES (?, ?, '', ?)
            ON CONFLICT (workspace_id, owner_user_id) DO NOTHING
            """,
            (self.workspace_id, owner, format_ts(self.clock())),
        )

    async def set_timezone(self, owner: str, zone: str) -> None:
        async with self.repo.transaction():
            await self._ensure_profile(owner)
            await self._run(
                "UPDATE user_profile SET timezone = ? WHERE workspace_id = ? AND owner_user_id = ? AND (timezone IS NULL OR timezone != ?)",
                (zone, self.workspace_id, owner, zone),
            )

    async def set_nightly_on(self, owner: str, day: str | None) -> None:
        await self._ensure_profile(owner)
        await self._run(
            "UPDATE user_profile SET nightly_on = ? WHERE workspace_id = ? AND owner_user_id = ?",
            (day, self.workspace_id, owner),
        )

    async def set_brief_on(self, owner: str, day: str) -> None:
        """The owner-local date whose morning brief was decided, sent or deliberately not (Spec 16 §3)."""
        async with self.repo.transaction():
            await self._ensure_profile(owner)
            await self._run(
                "UPDATE user_profile SET brief_on = ? WHERE workspace_id = ? AND owner_user_id = ?",
                (day, self.workspace_id, owner),
            )

    async def count_nudge(self, owner: str, day: str) -> None:
        """One more unprompted immediate DM on the owner-local `day` (Spec 16 §3.1 caps these)."""
        async with self.repo.transaction():
            await self._ensure_profile(owner)
            await self._run(
                """
                UPDATE user_profile
                SET nudges_sent = CASE WHEN nudges_on = ? THEN nudges_sent + 1 ELSE 1 END, nudges_on = ?
                WHERE workspace_id = ? AND owner_user_id = ?
                """,
                (day, day, self.workspace_id, owner),
            )

    async def rebuild_profile(self, owner: str, now: datetime) -> str:
        """Compile the one-pager (Spec 13 §4.1) from active records. Deterministic, so forget is honored at once."""
        records = await self.active_records(owner)
        counts: dict[str, int] = {}
        for record in records:
            counts[record["type"]] = counts.get(record["type"], 0) + 1
        sections: list[tuple[str, list[str], int]] = []

        def lines(predicate: Callable[[dict[str, Any]], bool]) -> list[str]:
            return [_profile_line(record) for record in records if predicate(record)]

        def tagged(record: dict[str, Any], tag: str) -> bool:
            return record["type"] == "preference" and tag in record["aliases"].lower()

        sections.append(("About you", lines(lambda r: r["type"] in ("fact", "org", "decision")), 20))
        sections.append(("Key people", lines(lambda r: r["type"] == "person"), 10))
        sections.append((
            "Preferences",
            lines(lambda r: r["type"] == "preference" and not tagged(r, "communication-style") and not tagged(r, "autonomy")),
            20,
        ))
        sections.append(("Communication style", lines(lambda r: tagged(r, "communication-style")), 8))
        sections.append(("Autonomy calibration", lines(lambda r: tagged(r, "autonomy")), 8))
        weekly = [record for record in records if record["type"] == "episode_weekly"][:1]
        sections.append(("Last week", [f"- {summarize_body(weekly[0]['body'], 600)}"] if weekly else [], 1))
        parts = [f"*{name}*\n" + "\n".join(items[:cap]) for name, items, cap in sections if items]
        tally = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
        footer = f"Memory: {tally or 'nothing yet'}. Profile generated {format_ts(now)} UTC."
        body = "\n\n".join(parts)
        if len(body) > PROFILE_CHAR_BUDGET:
            body = body[:PROFILE_CHAR_BUDGET].rsplit("\n", 1)[0]
        body = f"{body}\n\n{footer}" if body else footer
        await self._ensure_profile(owner)
        await self._run(
            "UPDATE user_profile SET body = ?, generated_at = ? WHERE workspace_id = ? AND owner_user_id = ?",
            (body, format_ts(now), self.workspace_id, owner),
        )
        return body

    async def migrate_contacts(self, now: datetime) -> int:
        """Spec 13 §6: one person record per contact. Idempotent on contact_id."""
        contacts = await self._all(
            """
            SELECT CAST(c.id AS TEXT) AS id, c.name, c.email, c.company, c.role, c.owner_user_id FROM contacts c
            WHERE c.workspace_id = ? AND NOT EXISTS (
                SELECT 1 FROM memory_records r
                WHERE r.workspace_id = c.workspace_id AND r.owner_user_id = c.owner_user_id
                  AND r.contact_id = CAST(c.id AS TEXT) AND r.type = 'person'
            )
            ORDER BY c.created_at
            """,
            (self.workspace_id,),
        )
        owners: set[str] = set()
        async with self.repo.transaction():
            for contact in contacts:
                interactions = await self._all(
                    "SELECT summary, commitment, created_at FROM interactions WHERE CAST(contact_id AS TEXT) = ? ORDER BY created_at",
                    (contact["id"],),
                )
                facts = [f"- {label}: {contact[key]}" for label, key in (("Role", "role"), ("Company", "company"), ("Email", "email")) if contact.get(key)]
                for row in interactions:
                    promise = f" (commitment: {row['commitment']})" if row.get("commitment") else ""
                    facts.append(f"- {str(row['created_at'])[:10]}: {row['summary']}{promise}")
                owner = contact["owner_user_id"]
                await self.create_record(
                    owner,
                    record_id=await self.free_id(owner, "person", contact["name"]),
                    type="person",
                    title=contact["name"],
                    body="\n".join(facts) or f"- Contact: {contact['name']}",
                    aliases=[value for value in (contact["name"], contact.get("email"), contact.get("company")) if value],
                    source="migration",
                    contact_id=contact["id"],
                    now=now,
                    sources=[("migration", contact["id"])],
                )
                owners.add(owner)
            for owner in owners:
                await self.rebuild_profile(owner, now)
        return len(contacts)

    async def wipe_derived(self, owner: str, since: datetime | None) -> None:
        """Remove memory compiled from turns (Spec 13 §3.5). Contacts migration and documents stay."""
        sources = ", ".join("?" for _ in DERIVED_SOURCES)
        cutoff = format_ts(since) if since else "0000"
        doomed = await self._all(
            f"""
            SELECT id, supersedes, valid_from FROM memory_records
            WHERE workspace_id = ? AND owner_user_id = ? AND source IN ({sources}) AND valid_from >= ?
            """,
            (self.workspace_id, owner, *DERIVED_SOURCES, cutoff),
        )
        for row in doomed:
            if "@v" in row["id"]:
                await self._delete_record(owner, row["id"])
        for row in doomed:
            if "@v" in row["id"]:
                continue
            restore = await self._newest_version_before(owner, row["supersedes"], cutoff)
            await self._delete_record(owner, row["id"])
            if restore is not None:
                await self._run(
                    "UPDATE memory_records SET id = ?, status = CASE WHEN status = 'SUPERSEDED' THEN 'ACTIVE' ELSE status END WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
                    (row["id"], self.workspace_id, owner, restore),
                )
                await self._run(
                    "UPDATE memory_provenance SET target_id = ? WHERE owner_user_id = ? AND target_type = 'record' AND target_id = ?",
                    (row["id"], owner, restore),
                )
        events = await self._all(
            "SELECT id FROM memory_events WHERE workspace_id = ? AND owner_user_id = ? AND created_at >= ?",
            (self.workspace_id, owner, cutoff),
        )
        for event in events:
            await self._run("DELETE FROM memory_provenance WHERE owner_user_id = ? AND target_type = 'event' AND target_id = ?", (owner, event["id"]))
            await self._run("DELETE FROM memory_events WHERE id = ?", (event["id"],))
        await self._run(
            "UPDATE conversation_turns SET reconciled_at = NULL WHERE workspace_id = ? AND owner_user_id = ? AND created_at >= ?",
            (self.workspace_id, owner, cutoff),
        )
        await self.drop_recaps(owner)
        await self.set_nightly_on(owner, None)

    async def _newest_version_before(self, owner: str, version_id: str | None, cutoff: str) -> str | None:
        while version_id:
            row = await self.get_record(owner, version_id)
            if row is None:
                return None
            if row["valid_from"] < cutoff:
                return row["id"]
            version_id = row["supersedes"]
        return None

    async def _delete_record(self, owner: str, record_id: str) -> None:
        await self._run(
            "DELETE FROM memory_provenance WHERE owner_user_id = ? AND target_type = 'record' AND target_id = ?",
            (owner, record_id),
        )
        await self._run(
            "DELETE FROM memory_records WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
            (self.workspace_id, owner, record_id),
        )


def _logged(rows: list[dict[str, Any]]) -> list[LoggedTurn]:
    return [
        LoggedTurn(
            id=row["id"], seq=int(row["seq"]), key=row["conversation_key"], role=row["role"], text=row["content"],
            at=parse_ts(row["created_at"]) or utc_now(),
        )
        for row in rows
    ]


def _without_retracted(turns: list[LoggedTurn], retracted: set[str]) -> list[LoggedTurn]:
    """Drop forgotten statements and the replies that answered them, so forget holds on the next read."""
    kept: list[LoggedTurn] = []
    skip_reply = False
    for turn in turns:
        if turn.id in retracted:
            skip_reply = turn.role == "user"
            continue
        if skip_reply and turn.role == "assistant":
            skip_reply = False
            continue
        skip_reply = False
        kept.append(turn)
    return kept


def _links(body: str) -> str:
    return json.dumps(list(dict.fromkeys(LINK.findall(body))))


def _embedding(title: str, aliases: str, body: str) -> bytes | None:
    vector = generate_embedding(f"{title} {aliases} {body[:500]}")
    return None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()


def _version_number(supersedes: str | None) -> int:
    match = re.search(r"@v(\d+)$", supersedes or "")
    return int(match.group(1)) if match else 0


def _hit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "title": row["title"],
        "snippet": summarize_body(row["body"], 240),
        "updated_at": str(row["updated_at"]),
    }


def _profile_line(record: dict[str, Any]) -> str:
    body = summarize_body(record["body"])
    if body.lower().startswith(record["title"].lower()) or not body:
        return f"- {body or record['title']}"
    return f"- {record['title']}: {body}"
