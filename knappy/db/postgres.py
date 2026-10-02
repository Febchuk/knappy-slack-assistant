"""PostgreSQL repository selected by a postgres:// database URL."""

from __future__ import annotations

import json
from typing import Any

from knappy.db.repository import SqliteRepository
from knappy.db.schema import POSTGRES_SCHEMA


class _PgResult:
    def __init__(self, rows: list[Any], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchone(self) -> Any:
        if not self._rows:
            return None
        return self._rows[0]

    async def fetchall(self) -> list[Any]:
        return list(self._rows)


class _PgConnection:
    """Small adapter so repository methods can keep using `?` placeholders."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self._tx: Any = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _PgResult:
        converted, values = _placeholders(sql, params)
        await self._begin()
        if _returns_rows(converted):
            rows = await self.raw.fetch(converted, *values)
            return _PgResult(rows, len(rows))
        status = await self.raw.execute(converted, *values)
        return _PgResult([], _rowcount(status))

    async def executescript(self, script: str) -> None:
        for statement in _statements(script):
            await self._begin()
            await self.raw.execute(statement)

    async def commit(self) -> None:
        if self._tx is not None:
            await self._tx.commit()
            self._tx = None

    async def rollback(self) -> None:
        if self._tx is not None:
            await self._tx.rollback()
            self._tx = None

    async def close(self) -> None:
        await self.rollback()
        await self.raw.close()

    async def _begin(self) -> None:
        if self._tx is None:
            self._tx = self.raw.transaction()
            await self._tx.start()


class PostgresRepository(SqliteRepository):
    dialect = "postgres"

    async def connect(self) -> None:
        import asyncpg

        raw = await asyncpg.connect(self.path)
        await raw.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        self._conn = _PgConnection(raw)

    async def init_schema(self) -> None:
        await self.connection.executescript(POSTGRES_SCHEMA)
        await self.connection.commit()


def is_postgres_url(database_url: str) -> bool:
    return database_url.startswith("postgres://") or database_url.startswith("postgresql://")


def _placeholders(sql: str, params: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
    parts: list[str] = []
    index = 0
    for char in sql:
        if char == "?":
            index += 1
            parts.append(f"${index}")
        else:
            parts.append(char)
    if index != len(params):
        raise ValueError(f"Expected {index} SQL parameters, got {len(params)}")
    return "".join(parts), params


def _returns_rows(sql: str) -> bool:
    leading = sql.lstrip().upper()
    return leading.startswith("SELECT") or " RETURNING " in f" {leading} "


def _rowcount(status: str) -> int:
    pieces = status.split()
    if len(pieces) >= 2 and pieces[-1].isdigit():
        return int(pieces[-1])
    return 0


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]
