"""Specs 08 and 09: Slack replies, one-shot sends, per-user memory, and history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from knappy.agent.tools import ToolRegistry, current_owner
from knappy.db.factory import repository_class
from knappy.db.postgres import PostgresRepository, _placeholders
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.db.schema import POSTGRES_SCHEMA
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.schedule import cadence_due
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.runtime import KnappyRuntime, heuristic_triage
from knappy.slack.egress import build_say
from knappy.slack.executor import SlackActionExecutor


class FakeSlack:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.posts: list[dict] = []
        self.ephemerals: list[dict] = []
        self.messages = messages or []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)

    async def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)

    async def conversations_history(self, *, channel, limit=20):
        return {"messages": self.messages}

    async def users_conversations(self, **kwargs):
        return {"channels": []}


def _runtime(repo: SqliteRepository, client: FakeSlack) -> KnappyRuntime:
    return KnappyRuntime(
        repo,
        workspace_id="T_TEST",
        say=build_say(client),
        sender=build_say(client),
        executor=SlackActionExecutor(client),
        history=client,
    )


@pytest.mark.asyncio
async def test_egress_note_answer_card_and_single_send(repo: SqliteRepository) -> None:
    client = FakeSlack()
    runtime = _runtime(repo, client)
    note = await runtime.handle_event(
        {
            "text": "note: Met with Alex from Acme Corp, promised to send the revised budget by Thursday.",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "1.0",
        }
    )
    assert note is not None
    assert len(client.posts) == 1
    assert "Alex" in client.posts[0]["text"]
    assert "thread_ts" not in client.posts[0]

    answer = await runtime.handle_event(
        {
            "text": "What did I promise to send Alex?",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "2.0",
        }
    )
    assert answer is not None
    assert len(client.posts) == 2
    assert answer.text == "You promised to send Alex the revised budget by Thursday."

    staged = await runtime.handle_event(
        {
            "text": "Follow up with Alex",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "3.0",
        }
    )
    assert staged is not None and staged.blocks is not None
    assert len(client.posts) == 3
    action_ids = [element["action_id"] for element in client.posts[-1]["blocks"][2]["elements"]]
    assert "btn_approve_action" in action_ids

    approved = await runtime.gateway.approve(staged.draft_id, "U1")
    again = await runtime.gateway.approve(staged.draft_id, "U1")
    sends = [post for post in client.posts if post.get("channel") == "Alex"]
    assert approved.executed is True
    assert again.status == "ignored"
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_mention_questions_are_not_small_talk(repo: SqliteRepository) -> None:
    client = FakeSlack()
    runtime = _runtime(repo, client)
    await repo.record_interaction(
        workspace_id="T_TEST",
        contact_name="Alex",
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text="send the revised budget",
        summary="send the revised budget",
        commitment="send the revised budget by Thursday",
        owner_user_id="U1",
    )
    who = await runtime.handle_event(
        {
            "type": "app_mention",
            "text": "<@UBOT> who are you",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "ts": "8.0",
        }
    )
    tasks = await runtime.handle_event(
        {
            "type": "app_mention",
            "text": "<@UBOT> what do I have to do",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "ts": "8.1",
        }
    )
    assert who is not None and "I'm Knappy" in who.text
    assert "How can I help" not in who.text
    assert tasks is not None and "revised budget" in tasks.text
    assert "How can I help" not in tasks.text


@pytest.mark.asyncio
async def test_channel_answer_is_ephemeral(repo: SqliteRepository) -> None:
    client = FakeSlack()
    runtime = _runtime(repo, client)
    await runtime.handle_event(
        {
            "type": "app_mention",
            "text": "What did I promise to send Alex?",
            "channel": "C1",
            "channel_type": "channel",
            "user": "U1",
            "ts": "4.0",
        }
    )
    assert client.posts == []
    assert len(client.ephemerals) == 1
    assert client.ephemerals[0]["user"] == "U1"


@pytest.mark.asyncio
async def test_second_user_alex_stays_private(repo: SqliteRepository) -> None:
    await repo.record_interaction(
        workspace_id="T_TEST",
        contact_name="Alex",
        source_type="NOTE_INGEST",
        channel_id="D2",
        raw_text="send the secret plan",
        summary="send the secret plan",
        commitment="send the secret plan",
        owner_user_id="U2",
    )
    tools = ToolRegistry(repo, "T_TEST")
    token = current_owner.set("U1")
    try:
        hidden = await tools.search_commitments("secret plan")
    finally:
        current_owner.reset(token)
    token = current_owner.set("U2")
    try:
        visible = await tools.search_commitments("secret plan")
    finally:
        current_owner.reset(token)
    assert hidden == []
    assert visible
    assert visible[0]["commitment"] == "send the secret plan"


@pytest.mark.asyncio
async def test_history_answers_without_ingestion(repo: SqliteRepository) -> None:
    client = FakeSlack(messages=[{"text": "ship the budget Friday", "user": "U2", "ts": "9.0"}])
    runtime = _runtime(repo, client)
    before = await repo.find_contacts("T_TEST")
    reply = await runtime.handle_event(
        {
            "text": "What did we say about the budget?",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "5.0",
        }
    )
    after = await repo.find_contacts("T_TEST")
    assert reply is not None
    assert "ship the budget Friday" in reply.text
    assert after == before


@pytest.mark.asyncio
async def test_reminder_dm_goes_to_owner(repo: SqliteRepository) -> None:
    contact_id = await repo.upsert_contact("T_TEST", "Alex", owner_user_id="U_FRIEND", slack_user_id="U_ALEX")
    due = format_ts(utc_now() + timedelta(hours=2))
    await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=contact_id,
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text="send the revised budget",
        summary="send the revised budget",
        commitment="send the revised budget",
        due_date=due,
        owner_user_id="U_FRIEND",
    )
    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs)

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(heuristic_triage),
        workspace_id="T_TEST",
        user_id="U_OTHER",
        sender=sender,
    )
    counts = await engine.run_tick()
    assert counts["immediate"] == 1
    assert sent[0]["channel"] == "U_FRIEND"


@pytest.mark.asyncio
async def test_unconnected_action_fails(repo: SqliteRepository) -> None:
    client = FakeSlack()
    runtime = _runtime(repo, client)
    draft_id = await repo.create_draft(
        workspace_id="T_TEST",
        user_id="U1",
        channel_id="D1",
        action_type="GMAIL_DRAFT",
        payload={
            "recipient_name": "Alex",
            "recipient_identifier": "alex@acme.test",
            "staged_content": "mail",
            "preview_summary": "mail",
            "action_type": "GMAIL_DRAFT",
            "metadata": {},
        },
    )
    result = await runtime.gateway.approve(draft_id, "U1")
    draft = await repo.get_draft(draft_id)
    assert result.status == "FAILED"
    assert draft is not None and draft["status"] == "FAILED"
    assert client.posts == []


def test_postgres_url_selects_postgres_repository() -> None:
    assert repository_class("postgresql://localhost/knappy") is PostgresRepository
    assert repository_class("sqlite:///knappy.db") is SqliteRepository
    sql, params = _placeholders("SELECT * FROM contacts WHERE id = ? AND name = ?", ("1", "Alex"))
    assert sql == "SELECT * FROM contacts WHERE id = $1 AND name = $2"
    assert params == ("1", "Alex")
    assert "owner_user_id" in POSTGRES_SCHEMA
    assert "UNIQUE(workspace_id, owner_user_id, name)" in POSTGRES_SCHEMA


def test_cadence_runs_once_at_eight() -> None:
    morning = datetime(2026, 10, 2, 8, 5)
    assert cadence_due(morning, None) is True
    assert cadence_due(morning, date(2026, 10, 2)) is False
    assert cadence_due(datetime(2026, 10, 2, 9, 0), None) is False


def test_dockerfile_starts_one_worker() -> None:
    text = Path("Dockerfile").read_text()
    assert 'CMD ["python", "-m", "knappy.main"]' in text
    manifest = Path("slack/manifest.yml").read_text()
    assert "channels:history" in manifest
    assert "groups:history" in manifest
