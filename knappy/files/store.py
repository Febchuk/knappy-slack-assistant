"""The documents table and chunk search (Spec 15 §2.2-2.3). Every query is scoped to one workspace and one owner."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from knappy.db.repository import SqliteRepository, format_ts
from knappy.db.vectors import cosine_distance
from knappy.ingestion.embed import generate_embedding, semantic
from knappy.memory.store import search_terms

CHUNK_CHARS = 1_500
CHUNK_OVERLAP = 200
DOCUMENT_COLUMNS = "id, slack_file_id, name, mimetype, size_bytes, text, summary, conversation_key, created_at"


@dataclass(frozen=True)
class Document:
    id: str
    slack_file_id: str
    name: str
    mimetype: str
    size_bytes: int
    text: str
    summary: str
    conversation_key: str
    created_at: str

    def listing(self) -> dict[str, Any]:
        return {"document_id": self.id, "name": self.name, "date": str(self.created_at)[:10], "summary": self.summary}


@dataclass(frozen=True)
class NewDocument:
    slack_file_id: str
    name: str
    mimetype: str
    size_bytes: int
    text: str
    summary: str
    conversation_key: str


def chunks(text: str) -> list[str]:
    """About 1,500 characters each, overlapping by 200, cut at a paragraph or line break when one is near."""
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            window = text[start + CHUNK_CHARS // 2 : end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"))
            if cut >= 0:
                end = start + CHUNK_CHARS // 2 + cut + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return pieces


def embed_chunks(pieces: list[str]) -> list[bytes | None]:
    """Synchronous; callers run it in a thread because the real embedder is CPU-bound."""
    blobs: list[bytes | None] = []
    for piece in pieces:
        vector = generate_embedding(piece)
        blobs.append(None if vector is None else np.asarray(vector, dtype=np.float32).tobytes())
    return blobs


class DocumentStore:
    def __init__(self, repo: SqliteRepository, workspace_id: str) -> None:
        self.repo = repo
        self.workspace_id = workspace_id

    async def _all(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = await self.repo.connection.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]

    async def insert(
        self, owner: str, new: NewDocument, pieces: list[str], embeddings: list[bytes | None], now: datetime
    ) -> str:
        """Expects to run inside repo.transaction(), with the document's memory record."""
        document_id = f"doc_{uuid.uuid4().hex[:12]}"
        await self.repo.connection.execute(
            """
            INSERT INTO documents (
                id, workspace_id, owner_user_id, slack_file_id, name, mimetype, size_bytes, text, summary,
                conversation_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, self.workspace_id, owner, new.slack_file_id, new.name, new.mimetype, new.size_bytes,
                new.text, new.summary, new.conversation_key, format_ts(now),
            ),
        )
        for seq, (piece, embedding) in enumerate(zip(pieces, embeddings)):
            await self.repo.connection.execute(
                "INSERT INTO document_chunks (document_id, seq, text, embedding) VALUES (?, ?, ?, ?)",
                (document_id, seq, piece, embedding),
            )
        return document_id

    async def get(self, owner: str, document_id: str) -> Document | None:
        rows = await self._all(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE workspace_id = ? AND owner_user_id = ? AND id = ?",
            (self.workspace_id, owner, document_id),
        )
        return _document(rows[0]) if rows else None

    async def by_slack_file(self, owner: str, slack_file_id: str) -> Document | None:
        rows = await self._all(
            f"SELECT {DOCUMENT_COLUMNS} FROM documents WHERE workspace_id = ? AND owner_user_id = ? AND slack_file_id = ?",
            (self.workspace_id, owner, slack_file_id),
        )
        return _document(rows[0]) if rows else None

    async def recent(self, owner: str, query: str | None, limit: int) -> list[Document]:
        rows = await self._all(
            f"""
            SELECT {DOCUMENT_COLUMNS} FROM documents WHERE workspace_id = ? AND owner_user_id = ?
            ORDER BY created_at DESC, id LIMIT 200
            """,
            (self.workspace_id, owner),
        )
        documents = [_document(row) for row in rows]
        terms = search_terms(query or "")
        if not terms:
            return documents[:limit]

        def hits(document: Document) -> int:
            haystack = f"{document.name} {document.summary}".lower()
            return sum(term in haystack for term in terms)

        matching = [document for document in documents if hits(document)]
        return sorted(matching, key=hits, reverse=True)[:limit]

    async def best_chunks(self, owner: str, document_id: str, query: str, max_chars: int) -> list[tuple[int, str]]:
        """The chunks that best match `query`, in document order, up to `max_chars`. Owner-scoped by the join."""
        rows = await self._all(
            """
            SELECT c.seq, c.text, c.embedding FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.workspace_id = ? AND d.owner_user_id = ? AND d.id = ?
            ORDER BY c.seq
            """,
            (self.workspace_id, owner, document_id),
        )
        terms = search_terms(query)
        vector = generate_embedding(query, query=True) if semantic() else None
        scored = []
        for row in rows:
            lower = row["text"].lower()
            score = float(sum(lower.count(term) > 0 for term in terms))
            if vector is not None and row["embedding"] is not None:
                score += 1.0 - cosine_distance(vector, np.frombuffer(bytes(row["embedding"]), dtype=np.float32))
            if score > 0:
                scored.append((score, row["seq"], row["text"]))
        kept: list[tuple[int, str]] = []
        size = 0
        for _score, seq, text in sorted(scored, key=lambda item: (-item[0], item[1])):
            if size + len(text) > max_chars:
                continue
            kept.append((seq, text))
            size += len(text)
        return sorted(kept)


def _document(row: dict[str, Any]) -> Document:
    return Document(
        id=row["id"],
        slack_file_id=row["slack_file_id"],
        name=row["name"],
        mimetype=row["mimetype"],
        size_bytes=int(row["size_bytes"]),
        text=row["text"] or "",
        summary=row["summary"] or "",
        conversation_key=row["conversation_key"],
        created_at=str(row["created_at"]),
    )
