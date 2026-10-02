"""Open the repository selected by KNAPPY_DATABASE_URL."""

from __future__ import annotations

from knappy.db.postgres import PostgresRepository, is_postgres_url
from knappy.db.repository import SqliteRepository


def sqlite_path(database_url: str) -> str:
    if database_url in {"sqlite:///:memory:", ":memory:"}:
        return ":memory:"
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        return database_url[len(prefix) :]
    return database_url


def repository_class(database_url: str) -> type[SqliteRepository]:
    if is_postgres_url(database_url):
        return PostgresRepository
    return SqliteRepository


async def open_repository(database_url: str) -> SqliteRepository:
    if is_postgres_url(database_url):
        repo: SqliteRepository = PostgresRepository(database_url)
    else:
        repo = SqliteRepository(sqlite_path(database_url))
    await repo.connect()
    return repo
