"""Shared fixtures for the Knappy test suite."""

from __future__ import annotations

import os

import pytest

# Spec 13 §5: the hashed embedder is for tests only. Tests that need real embeddings opt in.
os.environ["KNAPPY_EMBEDDER"] = "hash"

from knappy.db.repository import SqliteRepository
from mcp_fakes import world  # noqa: F401  (Spec 19 and 20's fake OAuth and MCP servers)


@pytest.fixture
async def repo() -> SqliteRepository:
    database = SqliteRepository(":memory:")
    await database.connect()
    await database.init_schema()
    await database.upsert_workspace("T_TEST", "Test Workspace", "xoxb-test")
    yield database
    await database.close()
