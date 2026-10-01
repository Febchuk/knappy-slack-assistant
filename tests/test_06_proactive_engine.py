"""Spec 06: deterministic heartbeat, triage, and proactive actions."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from knappy.agent.memory import ThreadMemory
from knappy.agent.react import ReActAgent
from knappy.agent.tools import ToolRegistry
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.scheduler import main as scheduler_main


def _decision(strategy: str, interrupt: float, confidence: float = 0.9, consequence: float = 1.0):
    async def classify(candidate):
        return {
            "interrupt_probability": interrupt,
            "strategy": strategy,
            "strategy_confidence": confidence,
            "consequence_score": consequence,
        }

    return classify


async def _due(repo: SqliteRepository, hours: int, commitment: str = "send the revised budget") -> str:
    contact_id = await repo.upsert_contact("T_TEST", "Alex", company="Acme", slack_user_id="U_ALEX")
    due = format_ts(utc_now() + timedelta(hours=hours))
    return await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=contact_id,
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text=commitment,
        summary=commitment,
        commitment=commitment,
        due_date=due,
        embedding=[1.0] + [0.0] * 383,
    )


@pytest.mark.asyncio
async def test_proact_01_zero_trigger(repo: SqliteRepository) -> None:
    calls = {"n": 0}

    async def classify(candidate):
        calls["n"] += 1
        return {}

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(classify),
        workspace_id="T_TEST",
        user_id="U1",
    )
    started = time.perf_counter()
    counts = await engine.run_tick()
    assert time.perf_counter() - started < 0.005
    assert counts["scanned"] == 0
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_proact_02_immediate_dm(repo: SqliteRepository) -> None:
    await _due(repo, hours=2)
    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs)

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(_decision("immediate_dm", 0.91)),
        workspace_id="T_TEST",
        user_id="U1",
        sender=sender,
    )
    counts = await engine.run_tick()
    assert counts["immediate"] == 1
    assert sent
    action_ids = [item["action_id"] for item in sent[0]["blocks"][1]["elements"]]
    assert "btn_approve_proactive_action" in action_ids


@pytest.mark.asyncio
async def test_proact_03_batches_low_urgency(repo: SqliteRepository) -> None:
    await _due(repo, hours=11)
    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs)

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(_decision("batch_into_morning_digest", 0.5)),
        workspace_id="T_TEST",
        user_id="U1",
        sender=sender,
    )
    counts = await engine.run_tick()
    queued = await repo.list_queued_briefings("T_TEST")
    assert counts["queued"] == 1
    assert len(queued) == 1
    assert queued[0]["status"] == "QUEUED"
    assert sent == []


@pytest.mark.asyncio
async def test_proact_04_snooze(repo: SqliteRepository) -> None:
    interaction_id = await _due(repo, hours=2)
    before = await repo.get_interaction(interaction_id)
    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(_decision("suppress_low_value", 0.1, 0.9, 0.1)),
        workspace_id="T_TEST",
        user_id="U1",
    )
    updated = await engine.snooze(interaction_id)
    after = await repo.get_interaction(interaction_id)
    assert updated is not None
    assert after["due_date"] > before["due_date"]


@pytest.mark.asyncio
async def test_proact_05_mark_done(repo: SqliteRepository) -> None:
    interaction_id = await _due(repo, hours=2)
    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(_decision("suppress_low_value", 0.1, 0.9, 0.1)),
        workspace_id="T_TEST",
        user_id="U1",
    )
    await engine.mark_done(interaction_id)
    row = await repo.get_interaction(interaction_id)
    assert row["status"] == "FULFILLED"


@pytest.mark.asyncio
async def test_proact_06_thread_handoff(repo: SqliteRepository) -> None:
    memory = ThreadMemory()
    memory.append("thread-1", "assistant", "You promised Alex: send the revised budget")
    seen: list[str] = []

    async def complete(messages):
        seen.append(messages[-1]["content"])
        return type("Turn", (), {"text": "I updated the draft.", "tool_name": None, "tool_args": {}})()

    agent = ReActAgent(ToolRegistry(repo, "T_TEST"), complete, memory)
    reply = await agent.run(
        "Actually, tell her I will send it Monday morning.",
        {"thread_ts": "thread-1", "user_id": "U1", "channel_id": "D1"},
    )
    assert "send the revised budget" in seen[0]
    assert reply.text == "I updated the draft."


def test_scheduler_run_now(tmp_path) -> None:
    database = tmp_path / "knappy.db"
    code = scheduler_main(
        ["--run-now", "--database", f"sqlite:///{database}", "--workspace", "T_TEST", "--user", "U1"]
    )
    assert code == 0
