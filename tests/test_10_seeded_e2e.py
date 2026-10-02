"""Spec 10: seeded file database, Slack posts, and the agent path log."""

from __future__ import annotations

import logging

import pytest

from knappy.db.repository import SqliteRepository
from knappy.main import FALLBACK_TEXT, dispatch_event
from knappy.runtime import KnappyRuntime
from knappy.slack.egress import build_say
from knappy.slack.events import on_app_mention, on_message


class FakeSlack:
    def __init__(self, *, ephemeral_error: BaseException | None = None) -> None:
        self.posts: list[dict] = []
        self.ephemerals: list[dict] = []
        self.ephemeral_error = ephemeral_error

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)

    async def chat_postEphemeral(self, **kwargs):
        if self.ephemeral_error is not None:
            raise self.ephemeral_error
        self.ephemerals.append(kwargs)


class BoomSay:
    async def __call__(self, **kwargs):
        raise RuntimeError("slack down")


async def _ack() -> None:
    return None


async def _seed(tmp_path, *, with_alex: bool) -> SqliteRepository:
    repo = SqliteRepository(str(tmp_path / "knappy.db"))
    await repo.connect()
    await repo.init_schema()
    await repo.upsert_workspace("T_TEST", "Test Workspace", "xoxb-test")
    if with_alex:
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
    return repo


def _runtime(repo: SqliteRepository, client: FakeSlack) -> KnappyRuntime:
    return KnappyRuntime(repo, workspace_id="T_TEST", say=build_say(client), history=client)


@pytest.mark.asyncio
async def test_who_are_you_posts_and_logs_identity(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    repo = await _seed(tmp_path, with_alex=True)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    caplog.set_level(logging.INFO, logger="knappy")
    await on_app_mention(
        {
            "type": "app_mention",
            "text": "<@UBOT> who are you",
            "channel": "C1",
            "user": "U1",
            "ts": "1.0",
        },
        _ack,
        processor=runtime.handle_event,
    )
    assert len(client.ephemerals) == 1
    assert "I'm Knappy" in client.ephemerals[0]["text"]
    assert "who are you" in caplog.text
    assert "path=identity" in caplog.text
    assert "deliver ephemeral channel=C1" in caplog.text
    await repo.close()


@pytest.mark.asyncio
async def test_open_commitments_are_private_to_the_owner(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    repo = await _seed(tmp_path, with_alex=True)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    caplog.set_level(logging.INFO, logger="knappy")
    await on_app_mention(
        {
            "type": "app_mention",
            "text": "<@UBOT> what do I have to do",
            "channel": "C1",
            "user": "U1",
            "ts": "2.0",
        },
        _ack,
        processor=runtime.handle_event,
    )
    await on_app_mention(
        {
            "type": "app_mention",
            "text": "<@UBOT> what do I have to do",
            "channel": "C1",
            "user": "U2",
            "ts": "2.1",
        },
        _ack,
        processor=runtime.handle_event,
    )
    assert "revised budget" in client.ephemerals[0]["text"]
    assert "Alex" in client.ephemerals[0]["text"]
    assert "open commitments" in client.ephemerals[1]["text"].lower() or "don't have" in client.ephemerals[1]["text"]
    assert "revised budget" not in client.ephemerals[1]["text"]
    assert "path=list_commitments" in caplog.text
    await repo.close()


@pytest.mark.asyncio
async def test_note_then_recall_posts_both_turns(tmp_path) -> None:
    repo = await _seed(tmp_path, with_alex=False)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    await on_message(
        {
            "text": "note: Met with Alex from Acme Corp, promised to send the revised budget by Thursday.",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "3.0",
        },
        _ack,
        processor=runtime.handle_event,
    )
    await on_message(
        {
            "text": "What did I promise to send Alex?",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "3.1",
        },
        _ack,
        processor=runtime.handle_event,
    )
    assert len(client.posts) == 2
    assert "Alex" in client.posts[0]["text"]
    assert client.posts[1]["text"] == "You promised to send Alex the revised budget by Thursday."
    await repo.close()


@pytest.mark.asyncio
async def test_ephemeral_failure_falls_back_to_channel_post(tmp_path) -> None:
    repo = await _seed(tmp_path, with_alex=False)
    client = FakeSlack(ephemeral_error=RuntimeError("channel_not_found"))
    runtime = _runtime(repo, client)
    await on_app_mention(
        {
            "type": "app_mention",
            "text": "<@UBOT> who are you",
            "channel": "C1",
            "user": "U1",
            "ts": "4.0",
        },
        _ack,
        processor=runtime.handle_event,
    )
    assert client.ephemerals == []
    assert len(client.posts) == 1
    assert "I'm Knappy" in client.posts[0]["text"]
    assert client.posts[0]["thread_ts"] == "4.0"
    await repo.close()


@pytest.mark.asyncio
async def test_draft_show_and_general_question(tmp_path) -> None:
    repo = await _seed(tmp_path, with_alex=True)
    client = FakeSlack()
    runtime = _runtime(repo, client)
    drafted = await runtime.handle_event(
        {
            "text": "give me a draft budget template for Alex",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "6.0",
        }
    )
    shown = await runtime.handle_event(
        {
            "text": "show me",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "6.1",
        }
    )
    general = await runtime.handle_event(
        {
            "text": "how many r in strawberry",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "6.2",
        }
    )
    assert drafted is not None and "Draft budget for Alex" in drafted.text
    assert "Done." not in drafted.text
    assert shown is not None and "Draft budget for Alex" in shown.text
    assert general is not None and "How can I help" not in general.text
    await repo.close()


@pytest.mark.asyncio
async def test_handler_error_posts_fallback(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    repo = await _seed(tmp_path, with_alex=False)
    client = FakeSlack()
    runtime = KnappyRuntime(repo, workspace_id="T_TEST", say=BoomSay())
    caplog.set_level(logging.INFO, logger="knappy")
    await dispatch_event(
        runtime,
        client,
        {
            "text": "<@UBOT> who are you",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "ts": "5.0",
        },
    )
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == FALLBACK_TEXT
    assert "RuntimeError" in caplog.text
    assert "slack down" in caplog.text
    await repo.close()
