"""Shared fixtures for the Knappy test suite."""

from __future__ import annotations

import pytest

from knappy.db.repository import SqliteRepository


@pytest.fixture
async def repo() -> SqliteRepository:
    database = SqliteRepository(":memory:")
    await database.connect()
    await database.init_schema()
    await database.upsert_workspace("T_TEST", "Test Workspace", "xoxb-test")
    yield database
    await database.close()
