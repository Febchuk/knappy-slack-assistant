"""Spec 06: the zero-cost sweep, triage routing, and the card buttons. Spec 16 behavior lives in test_16_proactive.py."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeClock, FakeSlack, dm, knappy_runtime, memory_structured, seed_commitment
from knappy.agent.session import Turn
from knappy.config import ConfigError
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, UserMessage
from knappy.scheduler import main as scheduler_main

# Tuesday 15:00 UTC: daytime for an owner in UTC, outside the brief window.
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def decide(strategy: str, interrupt: float, confidence: float = 0.9, consequence: float = 1.0):
    calls: list[dict] = []

    async def classify(candidate):
        calls.append(candidate)
        return {
            "interrupt_probability": interrupt,
            "strategy": strategy,
            "strategy_confidence": confidence,
            "consequence_score": consequence,
        }

    return ProactiveAlertTriager(classify), calls


async def utc_runtime(repo: SqliteRepository, slack: FakeSlack, triager: ProactiveAlertTriager | None = None):
    runtime = knappy_runtime(repo, slack, FakeClock(NOW))
    await runtime.store.set_timezone("U1", "UTC")
    if triager is not None:
        runtime.heartbeat.triager = triager
    return runtime


async def test_proact_01_zero_trigger(repo: SqliteRepository) -> None:
    slack = FakeSlack()
    triager, calls = decide("immediate_dm", 0.9)
    runtime = await utc_runtime(repo, slack, triager)
    started = time.perf_counter()
    ticks = await runtime.heartbeat.run_tick()
    elapsed = time.perf_counter() - started

    assert [(tick.owner, tick.candidates, tick.outcome) for tick in ticks] == [("U1", 0, "silent")]
    assert elapsed < 0.05
    assert calls == [] and runtime.loop.model.structured_requests == [] and slack.posts == []


async def test_proact_02_immediate_dm(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send the revised budget", due=NOW + timedelta(hours=2))
    slack = FakeSlack()
    runtime = await utc_runtime(repo, slack, decide("immediate_dm", 0.91)[0])
    [tick] = await runtime.heartbeat.run_tick()

    assert tick.outcome == "sent"
    assert [post["channel"] for post in slack.posts] == ["DU1"]
    action_ids = [e["action_id"] for b in slack.posts[0]["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert action_ids == ["btn_approve_proactive_action", "btn_edit_draft", "btn_resolve_commitment", "btn_snooze_commitment"]


async def test_proact_03_batches_low_urgency(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send the revised budget", due=NOW + timedelta(hours=11))
    slack = FakeSlack()
    runtime = await utc_runtime(repo, slack, decide("batch_into_morning_digest", 0.5)[0])
    [tick] = await runtime.heartbeat.run_tick()
    queued = await repo.list_queued_briefings("T_TEST")

    assert tick.outcome == "queued"
    assert [(item["status"], item["owner_user_id"]) for item in queued] == [("QUEUED", "U1")]
    assert slack.posts == []


async def test_proact_04_snooze(repo: SqliteRepository) -> None:
    interaction_id = await seed_commitment(repo, "send the revised budget", due=NOW + timedelta(hours=2))
    runtime = await utc_runtime(repo, FakeSlack())
    before = await repo.get_interaction(interaction_id)
    updated = await runtime.heartbeat.snooze(interaction_id)
    after = await repo.get_interaction(interaction_id)

    assert updated == after["due_date"] == "2026-10-07 17:00:00"
    assert before["due_date"] == "2026-10-06 17:00:00"


async def test_proact_05_mark_done(repo: SqliteRepository) -> None:
    interaction_id = await seed_commitment(repo, "send the revised budget", due=NOW + timedelta(hours=2))
    runtime = await utc_runtime(repo, FakeSlack())
    await runtime.heartbeat.mark_done(interaction_id)

    assert (await repo.get_interaction(interaction_id))["status"] == "FULFILLED"


async def test_contactless_commitment_reminds_owner_without_a_draft(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "renew passport", due=NOW + timedelta(hours=2), contact=None)
    slack = FakeSlack()
    runtime = await utc_runtime(repo, slack, decide("immediate_dm", 0.91)[0])
    await runtime.heartbeat.run_tick()
    drafts = await (await repo.connection.execute("SELECT COUNT(*) AS n FROM action_drafts")).fetchone()

    assert [post["channel"] for post in slack.posts] == ["DU1"]
    action_ids = [e["action_id"] for b in slack.posts[0]["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert action_ids == ["btn_resolve_commitment", "btn_snooze_commitment"]
    assert drafts["n"] == 0


@pytest.mark.asyncio
async def test_proact_06_thread_handoff(repo: SqliteRepository) -> None:
    model = FakeModel([ModelTurn(text="I updated the draft.")], structured=memory_structured())
    runtime = knappy_runtime(repo, FakeSlack(), FakeClock(NOW), model=model)
    await runtime.conversations.append(
        "U1", "thread:D1:thread-1", Turn("assistant", "You promised Alex: send the revised budget")
    )
    reply = await runtime.handle_event(
        dm("Actually, tell her I will send it Monday morning.", "2.0", thread_ts="thread-1")
    )
    assert model.requests[0].contents == [
        ModelTurn(text="You promised Alex: send the revised budget"),
        UserMessage("Actually, tell her I will send it Monday morning."),
    ]
    assert reply.text == "I updated the draft."


def test_scheduler_run_now_requires_gemini_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("knappy.scheduler.load_dotenv", lambda: None)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        scheduler_main(["--run-now", "--database", f"sqlite:///{tmp_path / 'knappy.db'}"])


def test_scheduler_run_now(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr("knappy.scheduler.load_dotenv", lambda: None)
    database = tmp_path / "knappy.db"
    code = scheduler_main(["--run-now", "--database", f"sqlite:///{database}", "--workspace", "T_TEST"])

    assert code == 0
    assert "no owners" in capsys.readouterr().out
