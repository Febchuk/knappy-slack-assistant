"""Spec 06: deterministic heartbeat, triage, and proactive actions."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from knappy.config import ConfigError

from knappy.agent.session import Turn
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, UserMessage
from knappy.runtime import KnappyRuntime
from knappy.scheduler import main as scheduler_main
from fakes import dm


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
async def test_contactless_commitment_reminds_owner_without_a_draft(repo: SqliteRepository) -> None:
    await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=None,
        source_type="DIRECT_DM",
        channel_id="D1",
        raw_text="renew passport",
        summary="renew passport",
        commitment="renew passport",
        due_date=format_ts(utc_now() + timedelta(hours=2)),
        owner_user_id="U1",
    )
    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs)

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(_decision("immediate_dm", 0.91)),
        workspace_id="T_TEST",
        user_id="U_FALLBACK",
        sender=sender,
    )
    counts = await engine.run_tick()
    drafts = await (await repo.connection.execute("SELECT COUNT(*) AS n FROM action_drafts")).fetchone()
    assert counts["immediate"] == 1
    assert sent[0]["channel"] == "U1"
    assert [element["action_id"] for element in sent[0]["blocks"][1]["elements"]] == [
        "btn_resolve_commitment",
        "btn_snooze_commitment",
    ]
    assert drafts["n"] == 0


@pytest.mark.asyncio
async def test_proact_06_thread_handoff(repo: SqliteRepository) -> None:
    model = FakeModel([ModelTurn(text="I updated the draft.")])
    runtime = KnappyRuntime(repo, workspace_id="T_TEST", model=model)
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


def test_scheduler_run_now(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    database = tmp_path / "knappy.db"
    code = scheduler_main(
        ["--run-now", "--database", f"sqlite:///{database}", "--workspace", "T_TEST", "--user", "U1"]
    )
    assert code == 0
