"""Spec 10: seeded file database, Slack posts, and the agent path log."""

from __future__ import annotations

import logging

import pytest

from knappy.db.repository import SqliteRepository
from knappy.runtime import KnappyRuntime
from knappy.slack.egress import build_say
from knappy.slack.events import on_app_mention, on_message
from fakes import FakeSlack, HeuristicModel, dm, mention


class DownSlack(FakeSlack):
    async def chat_postMessage(self, **kwargs):
        raise RuntimeError("slack down")


async def _ack() -> None:
    return None


@pytest.fixture
async def seeded(tmp_path):
    repo = SqliteRepository(str(tmp_path / "knappy.db"))
    await repo.connect()
    await repo.init_schema()
    await repo.upsert_workspace("T_TEST", "Test Workspace", "xoxb-test")
    yield repo
    await repo.close()


async def _add_alex(repo: SqliteRepository) -> None:
    await repo.record_interaction(
        workspace_id="T_TEST",
        contact_name="Alex",
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text="send the revised budget by Thursday",
        summary="send the revised budget by Thursday",
        commitment="send the revised budget by Thursday",
        owner_user_id="U1",
    )


def _runtime(repo: SqliteRepository, client: FakeSlack) -> KnappyRuntime:
    return KnappyRuntime(repo, workspace_id="T_TEST", model=HeuristicModel(), say=build_say(client), slack=client)


@pytest.mark.asyncio
async def test_mention_is_answered_ephemerally_without_logging_text(seeded: SqliteRepository, caplog: pytest.LogCaptureFixture) -> None:
    repo = seeded
    await _add_alex(repo)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    caplog.set_level(logging.INFO, logger="knappy")
    await on_app_mention(mention("who are you", "1.0"), _ack, processor=runtime.handle_event)
    assert len(client.ephemerals) == 1
    assert client.ephemerals[0]["text"] == "Model answer to: who are you"
    assert "who are you" not in caplog.text
    assert "agent steps=1 stop=answer" in caplog.text
    assert "deliver ephemeral channel=C1" in caplog.text


@pytest.mark.asyncio
async def test_open_commitments_are_private_to_the_owner(seeded: SqliteRepository) -> None:
    repo = seeded
    await _add_alex(repo)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    await on_app_mention(mention("what do I have to do", "2.0"), _ack, processor=runtime.handle_event)
    await on_app_mention(mention("what do I have to do", "2.1", user="U2"), _ack, processor=runtime.handle_event)
    assert "revised budget" in client.ephemerals[0]["text"]
    assert "Alex" in client.ephemerals[0]["text"]
    assert client.ephemerals[1]["user"] == "U2"
    assert "none" in client.ephemerals[1]["text"]
    assert "revised budget" not in client.ephemerals[1]["text"]


@pytest.mark.asyncio
async def test_note_then_recall_answers_both_turns(seeded: SqliteRepository) -> None:
    repo = seeded
    client = FakeSlack()
    runtime = _runtime(repo, client)
    await on_message(
        dm("note: Met with Alex from Acme Corp, promised to send the revised budget by Thursday.", "3.0"),
        _ack,
        processor=runtime.handle_event,
    )
    await on_message(dm("What did I promise to send Alex?", "3.1"), _ack, processor=runtime.handle_event)
    assert len(client.posts) == 2
    assert "Alex" in client.shown("100.1")["text"]
    assert client.shown("100.2")["text"] == "Alex: send the revised budget by Thursday"


@pytest.mark.asyncio
async def test_ephemeral_failure_falls_back_to_channel_post(seeded: SqliteRepository) -> None:
    repo = seeded
    client = FakeSlack(ephemeral_error=RuntimeError("channel_not_found"))
    runtime = _runtime(repo, client)
    await on_app_mention(mention("who are you", "4.0"), _ack, processor=runtime.handle_event)
    assert client.ephemerals == []
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == "Model answer to: who are you"
    assert client.posts[0]["thread_ts"] == "4.0"


@pytest.mark.asyncio
async def test_slack_outage_is_logged_not_raised(seeded: SqliteRepository, caplog: pytest.LogCaptureFixture) -> None:
    repo = seeded
    runtime = _runtime(repo, DownSlack())
    caplog.set_level(logging.INFO, logger="knappy")
    reply = await runtime.handle_event(dm("hello", "5.0"))
    assert "Reference:" in reply.text
    assert "handle_event failed ref=" in caplog.text
    assert "slack down" in caplog.text
