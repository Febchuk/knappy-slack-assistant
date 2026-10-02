"""Spec 02: Slack transport and dual-memory storage."""

from __future__ import annotations

import os
import time

import pytest

from knappy.config import ConfigError, Settings, load_dotenv
from knappy.db.repository import SqliteRepository
from knappy.db.schema import EXPECTED_TABLES, POSTGRES_SCHEMA
from knappy.db.vectors import cosine_distance
from knappy.slack.app import create_app
from knappy.slack.events import on_message


def test_infra_01_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    settings = Settings.from_env()
    assert settings.slack_bot_token == "xoxb-test"
    assert settings.slack_app_token == "xapp-test"
    app = create_app(settings)
    assert app is not None


def test_dotenv_strips_quotes_and_inline_comments(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KNAPPY_DATABASE_URL", raising=False)
    monkeypatch.delenv("KNAPPY_WORKSPACE_ID", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        'KNAPPY_DATABASE_URL="sqlite:///knappy.db"\n'
        "KNAPPY_WORKSPACE_ID=T123456789 # your Slack team id\n"
    )
    load_dotenv(env_file)
    assert os.environ["KNAPPY_DATABASE_URL"] == "sqlite:///knappy.db"
    assert os.environ["KNAPPY_WORKSPACE_ID"] == "T123456789"


def test_infra_01_missing_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


@pytest.mark.asyncio
async def test_infra_02_message_ack_is_fast() -> None:
    acked: list[float] = []

    async def ack() -> None:
        acked.append(time.perf_counter())

    started = time.perf_counter()
    elapsed = await on_message(
        {"text": "hello there from me", "channel_type": "im", "ts": "1.0"},
        ack,
    )
    assert acked
    assert elapsed < 0.5
    assert acked[0] - started < 0.5


@pytest.mark.asyncio
async def test_infra_03_sqlite_schema(repo: SqliteRepository) -> None:
    names = await repo.table_names()
    assert EXPECTED_TABLES <= names
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in POSTGRES_SCHEMA


@pytest.mark.asyncio
async def test_infra_04_vector_roundtrip(repo: SqliteRepository) -> None:
    contact_id = await repo.upsert_contact("T_TEST", "Alex", company="Acme")
    vector = [0.0] * 384
    vector[0] = 1.0
    await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=contact_id,
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text="budget",
        summary="Send the budget",
        embedding=vector,
    )
    matches = await repo.nearest_interactions(vector, workspace_id="T_TEST")
    assert len(matches) == 1
    assert matches[0]["cosine_distance"] == pytest.approx(0.0, abs=1e-6)
    assert cosine_distance(vector, vector) == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_infra_05_cascade_delete(repo: SqliteRepository) -> None:
    contact_id = await repo.upsert_contact("T_TEST", "Alex")
    await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=contact_id,
        source_type="DIRECT_DM",
        channel_id="D1",
        raw_text="hello there friend today",
        summary="Said hello",
    )
    await repo.delete_contact(contact_id)
    assert await repo.list_interactions_for_contact(contact_id) == []
    assert await repo.get_contact(contact_id) is None


@pytest.mark.asyncio
async def test_socket_mode_handler_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    settings = Settings.from_env()
    handler = AsyncSocketModeHandler(create_app(settings), settings.slack_app_token)
    assert handler.client is not None
    await handler.client.close()
