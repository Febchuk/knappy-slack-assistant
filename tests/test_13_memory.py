"""Spec 13: persistent turns, records with provenance, the admission-gated reconciler, forget, and rebuild."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from knappy.db.repository import SqliteRepository
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, UserMessage
from knappy.memory import MemoryConfig, MemoryEngine, MemoryStore
from knappy.memory.engine import BACKGROUND_TIMEOUT_S
from knappy.runtime import KnappyRuntime
from knappy.slack.egress import build_say
from fakes import FakeSlack, dm, event, memory_structured, op, tool_results, tool_turn, user_turns

START = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)  # a Saturday


class Clock:
    def __init__(self, at: datetime = START) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def agent(script: dict[str, tuple[str, dict]] | None = None):
    """Agent double: calls the scripted tool for a message prefix, then reports the tool results."""

    async def respond(request):
        results = tool_results(request.contents)
        if results:
            return ModelTurn(text=json.dumps([result.result for result in results], default=str))
        text = request.contents[-1].text.lower()
        for prefix, (name, args) in (script or {}).items():
            if text.startswith(prefix):
                return tool_turn(name, args)
        return ModelTurn(text="ok")

    return respond


def runtime_for(repo, *, script=None, reconcile=None, clock=None, client=None, config=None):
    client = client or FakeSlack(tz="UTC")
    model = FakeModel(agent(script), structured=memory_structured(reconcile))
    runtime = KnappyRuntime(
        repo, workspace_id="T_TEST", model=model, say=build_say(client), sender=build_say(client), slack=client,
        clock=clock or Clock(), memory_config=config,
    )
    return runtime, model, client


async def open_repo(path: Path) -> SqliteRepository:
    repo = SqliteRepository(str(path))
    await repo.connect()
    await repo.init_schema()
    await repo.upsert_workspace("T_TEST", "Test", "xoxb")
    return repo


@pytest.fixture
async def open_db():
    """Opens file databases and closes every one at teardown, even when the test fails."""
    opened: list[SqliteRepository] = []

    async def open_(path: Path) -> SqliteRepository:
        opened.append(await open_repo(path))
        return opened[-1]

    yield open_
    for repo in opened:
        await repo.close()


async def rows(repo: SqliteRepository, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cursor = await repo.connection.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


def employer(payload: dict) -> dict:
    """Reconciler double: learns where the user works, superseding when they move."""
    known = {record["id"] for record in payload["records"]}
    events, ops = [], []
    for turn in user_turns(payload):
        text = turn["text"].lower()
        if "work at google" in text:
            events.append(event("learned", "Works at Google", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="fact:employer", type="fact", title="Employer",
                          body="- Works at Google", aliases=["job", "work", "company"]))
        if "moved from google to stripe" in text:
            events.append(event("changed", "Moved from Google to Stripe", [turn["id"]], occurred_at="2026-10-03T12:30:00Z"))
            ops.append(op("supersede" if "fact:employer" in known else "create", events=[len(events) - 1],
                          record_id="fact:employer", type="fact", title="Employer", body="- Works at Stripe",
                          aliases=["job", "work", "company"]))
    return {"events": events, "ops": ops, "discarded": []}


async def test_mem_01_turns_survive_restart(tmp_path: Path, open_db) -> None:
    clock = Clock()
    repo = await open_db(tmp_path / "k.db")
    runtime, _model, _client = runtime_for(repo, clock=clock)
    await runtime.handle_event(dm("my sister is visiting next week", "1.0"))
    clock.advance(minutes=1)
    await runtime.handle_event(dm("she likes jazz", "2.0"))
    await repo.close()

    repo = await open_db(tmp_path / "k.db")
    clock.advance(minutes=1)
    runtime, model, _client = runtime_for(repo, clock=clock)
    await runtime.handle_event(dm("any ideas for her?", "3.0"))

    assert model.requests[0].contents == [
        UserMessage("my sister is visiting next week"), ModelTurn(text="ok"),
        UserMessage("she likes jazz"), ModelTurn(text="ok"),
        UserMessage("any ideas for her?"),
    ]


async def test_mem_02_remember_is_in_the_next_prompt(repo: SqliteRepository) -> None:
    script = {"remember": ("remember", {"text": "I'm vegetarian", "type": "preference", "about": "diet"})}
    seen: list[dict] = []
    clock = Clock()
    runtime, model, _client = runtime_for(repo, script=script, clock=clock, reconcile=lambda payload: seen.append(payload) or {})
    await runtime.handle_event(dm("Remember I'm vegetarian", "1.0"))
    await runtime.handle_event(dm("pick a lunch spot", "2.0", thread_ts="2.0"))

    record = await runtime.store.get_record("U1", "preference:diet")
    assert record["type"] == "preference" and "vegetarian" in record["body"]
    assert "vegetarian" in model.requests[-1].system
    said = (await runtime.memory_engine.read("U1", "preference:diet"))["sources"]
    assert said[0]["said"] == "I'm vegetarian"
    turn = (await rows(repo, "SELECT p.source_id FROM memory_provenance p JOIN memory_events e ON e.id = p.target_id WHERE p.target_type = 'event'"))[0]
    user_turn = await rows(repo, "SELECT content FROM conversation_turns WHERE id = ?", (turn["source_id"],))
    assert user_turn[0]["content"] == "Remember I'm vegetarian"

    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    told = {turn["text"]: turn.get("already_saved") for payload in seen for turn in payload["turns"]}
    assert told["Remember I'm vegetarian"] == ["preference:diet"], "the reconciler is told what remember already saved"
    assert told["pick a lunch spot"] is None


async def test_mem_03_supersede_keeps_history(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=employer, clock=clock)
    await runtime.handle_event(dm("I work at Google", "1.0"))
    clock.advance(minutes=5)
    await runtime.memory_engine.tick()
    assert await runtime.store.get_record("U1", "fact:employer") is None, "an active conversation is not reconciled"
    clock.advance(minutes=16)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("I moved from Google to Stripe", "2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()

    current = await runtime.memory_engine.read("U1", "fact:employer", history=True)
    assert current["status"] == "ACTIVE" and "Stripe" in current["body"] and "Google" not in current["body"]
    assert [(version["id"], version["status"], version["body"]) for version in current["history"]] == [
        ("fact:employer@v1", "SUPERSEDED", "- Works at Google")
    ]
    assert [source["said"] for source in current["sources"]] == ["Moved from Google to Stripe"]


async def test_background_reconcile_gets_the_long_timeout(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(repo, reconcile=employer, clock=clock)
    await runtime.handle_event(dm("I work at Google", "1.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    reconciles = [timeout for (schema, _, _), timeout in zip(model.structured_requests, model.structured_timeouts)
                  if schema.__name__ == "ReconcileResult"]
    assert reconciles == [BACKGROUND_TIMEOUT_S]
    assert BACKGROUND_TIMEOUT_S >= 60


async def test_mem_04_time_bound_fact_expires(repo: SqliteRepository) -> None:
    def exam(payload):
        turn = user_turns(payload)[0]
        return {"events": [event("learned", "Exam on Oct 12", [turn["id"]])], "ops": [
            op("create", events=[0], type="fact", title="Exam", body="- Exam on October 12", expires_at="2026-10-13T00:00:00Z")
        ]}

    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=exam, clock=clock)
    await runtime.handle_event(dm("My exam is on the 12th", "1.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    record = await runtime.store.get_record("U1", "fact:exam")
    assert record["expires_at"] == "2026-10-13 00:00:00"
    assert "Exam" in (await runtime.store.profile("U1"))["body"]

    clock.now = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
    await runtime.memory_engine.tick()

    assert (await runtime.store.get_record("U1", "fact:exam"))["status"] == "EXPIRED"
    assert "Exam" not in (await runtime.store.profile("U1"))["body"]


async def test_mem_05_forget_is_honored_on_the_next_read(repo: SqliteRepository) -> None:
    script = {
        "remember": ("remember", {"text": "I'm vegetarian", "type": "preference", "about": "diet"}),
        "forget": ("forget", {"query_or_id": "vegetarian"}),
    }
    runtime, model, _client = runtime_for(repo, script=script)
    await runtime.handle_event(dm("Remember I'm vegetarian", "1.0"))
    forgot = await runtime.handle_event(dm("Forget that I'm vegetarian", "2.0"))
    await runtime.handle_event(dm("what should I eat?", "3.0"))

    assert (await runtime.store.get_record("U1", "preference:diet"))["status"] == "FORGOTTEN"
    assert "Remember I'm vegetarian" not in [getattr(item, "text", None) for item in model.requests[-1].contents]
    assert "vegetarian" not in model.requests[-1].system
    assert await runtime.store.search("U1", "vegetarian") == []
    assert await runtime.store.search_conversations("U1", "remember vegetarian") == [
        {"conversation": "dm:D1", "role": "user", "date": "2026-10-03 12:00 UTC", "text": "Forget that I'm vegetarian"}
    ]
    assert "Diet" in forgot.text


async def test_mem_06_owners_never_see_each_others_memory(repo: SqliteRepository) -> None:
    runtime, _model, _client = runtime_for(repo, script={
        "remember my manager is sam": ("remember", {"text": "My manager is Sam", "about": "manager"}),
        "remember my manager is priya": ("remember", {"text": "My manager is Priya", "about": "manager"}),
    })
    await runtime.handle_event(dm("remember my manager is Sam", "1.0", user="U1", channel="D1"))
    await runtime.handle_event(dm("remember my manager is Priya", "2.0", user="U2", channel="D2"))

    assert "Sam" in (await runtime.memory_engine.read("U1", "fact:manager"))["body"]
    assert "Priya" in (await runtime.memory_engine.read("U2", "fact:manager"))["body"]
    assert [hit["snippet"] for hit in await runtime.store.search("U1", "manager")] == ["My manager is Sam"]
    assert await runtime.store.search("U1", "Priya") == []
    assert "Sam" not in (await runtime.store.profile("U2"))["body"]


async def test_mem_07_unknown_id_is_rejected_and_the_rest_applies(
    repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    def reconcile(payload):
        turn = user_turns(payload)[0]["id"]
        return {"events": [event("learned", "Likes window seats", [turn])], "ops": [
            op("update", events=[0], record_id="fact:does-not-exist", body="- nope"),
            op("create", events=[0], type="preference", title="Seating", body="- Prefers window seats"),
        ]}

    caplog.set_level(logging.INFO, logger="knappy")
    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=reconcile, clock=clock)
    await runtime.handle_event(dm("I always take the window seat", "1.0"))
    clock.advance(minutes=21)
    report = await runtime.memory_engine.reconcile("U1")

    assert report.rejected == ["update fact:does-not-exist: unknown record id 'fact:does-not-exist'"]
    assert "rejected update fact:does-not-exist" in caplog.text
    assert (await runtime.store.get_record("U1", "preference:seating"))["status"] == "ACTIVE"
    assert await runtime.store.get_record("U1", "fact:does-not-exist") is None


async def test_mem_08_secrets_never_reach_memory(repo: SqliteRepository) -> None:
    key = "sk-proj-4fT9xQ2LmZ8vB1nR7kW3yH6pJ0dS5aE"

    def naive(payload):
        turn = user_turns(payload)[0]
        return {"events": [event("learned", f"OpenAI key is {key}", [turn["id"]])], "ops": [
            op("create", events=[0], type="fact", title=f"OpenAI key {key}", body=f"- key: {key}", aliases=[key])
        ]}

    clock = Clock()
    runtime, _model, _client = runtime_for(
        repo, reconcile=naive, clock=clock,
        script={"save": ("remember", {"text": f"my api key is {key}"})},
    )
    await runtime.handle_event(dm(f"here is my OpenAI key {key}", "1.0"))
    await runtime.handle_event(dm(f"save my api key is {key}", "2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()

    stored = await rows(repo, "SELECT title || aliases || body AS text FROM memory_records")
    stored += await rows(repo, "SELECT summary AS text FROM memory_events")
    stored += await rows(repo, "SELECT body AS text FROM user_profile")
    assert len(stored) >= 4
    assert not [row for row in stored if key in row["text"] or key[3:20] in row["text"]]


OLD_SCHEMA = """
CREATE TABLE workspaces (id TEXT PRIMARY KEY, team_name TEXT NOT NULL, bot_token TEXT NOT NULL, installed_at DATETIME DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE contacts (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE, name TEXT NOT NULL,
    slack_user_id TEXT, email TEXT, company TEXT, role TEXT, reminder_cadence_days INTEGER DEFAULT 30,
    last_interaction_ts DATETIME DEFAULT CURRENT_TIMESTAMP, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL DEFAULT '', UNIQUE(workspace_id, owner_user_id, name));
CREATE TABLE interactions (
    id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('DIRECT_DM', 'APP_MENTION', 'NOTE_INGEST')),
    channel_id TEXT NOT NULL, thread_ts TEXT, raw_text TEXT NOT NULL, summary TEXT NOT NULL, commitment TEXT,
    due_date DATETIME, status TEXT NOT NULL DEFAULT 'PENDING', embedding BLOB, last_alerted_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP, owner_user_id TEXT NOT NULL DEFAULT '');
INSERT INTO workspaces (id, team_name, bot_token) VALUES ('T_TEST', 'Test', 'xoxb');
INSERT INTO contacts (id, workspace_id, name, email, company, owner_user_id) VALUES
    ('c-alex', 'T_TEST', 'Alex Chen', 'alex@acme.com', 'Acme', 'U1'),
    ('c-sam', 'T_TEST', 'Sam Lee', NULL, NULL, 'U2');
INSERT INTO interactions (id, workspace_id, contact_id, source_type, channel_id, raw_text, summary, commitment, owner_user_id) VALUES
    ('i-1', 'T_TEST', 'c-alex', 'NOTE_INGEST', 'D1', 'x', 'Discussed the Q3 budget', 'send the budget deck', 'U1');
"""


async def test_mem_09_contacts_migrate_once(tmp_path: Path, open_db) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as raw:
        raw.executescript(OLD_SCHEMA)

    for _ in range(2):
        repo = await open_db(path)
        await MemoryStore(repo, "T_TEST").migrate_contacts(START)
        await repo.close()

    repo = await open_db(path)
    people = await rows(repo, "SELECT owner_user_id, id, contact_id, body, aliases FROM memory_records WHERE type = 'person' ORDER BY id")
    assert [(p["owner_user_id"], p["id"], p["contact_id"]) for p in people] == [
        ("U1", "person:alex-chen", "c-alex"), ("U2", "person:sam-lee", "c-sam")
    ]
    assert "Discussed the Q3 budget (commitment: send the budget deck)" in people[0]["body"]
    assert "Acme" in people[0]["aliases"] and "alex@acme.com" in people[0]["aliases"]
    columns = {row["name"] for row in await rows(repo, "PRAGMA table_info(interactions)")}
    assert {"next_check_at", "on_no_progress", "waiting_on"} <= columns
    assert "Alex Chen" in (await MemoryStore(repo, "T_TEST").profile("U1"))["body"]


def _fastembed_or_skip() -> None:
    from knappy.ingestion import embed

    try:
        from fastembed import TextEmbedding  # noqa: F401
    except ImportError:
        pytest.skip("fastembed is not installed")
    if embed._load() is None:
        pytest.skip("fastembed model could not load (offline?)")


async def test_mem_10_semantic_search_finds_paraphrases(repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNAPPY_EMBEDDER", "fastembed")
    _fastembed_or_skip()
    store = MemoryStore(repo, "T_TEST", Clock())
    async with repo.transaction():
        for title, aliases, body in (
            ("Pasta preference", ["food", "italian"], "- Loves carbonara"),
            ("Car insurance", ["policy"], "- Renews in March"),
            ("Manager", ["boss"], "- Priya runs the payments team"),
        ):
            await store.create_record("U1", record_id=await store.free_id("U1", "preference", title), type="preference",
                                      title=title, aliases=aliases, body=body, source="remember", now=START, sources=[])

    assert "preference:pasta-preference" in [hit["id"] for hit in await store.search("U1", "italian noodles")]
    paraphrase = await store.search("U1", "noodles")
    assert [hit["id"] for hit in paraphrase][:1] == ["preference:pasta-preference"]

    monkeypatch.setenv("KNAPPY_EMBEDDER", "hash")
    assert await store.search("U1", "noodles") == [], "without real embeddings only text matches rank"


async def test_mem_11_small_talk_is_discarded(repo: SqliteRepository) -> None:
    def chatty(payload):
        turns = user_turns(payload)
        return {
            "events": [event("learned", f"Said {turn['text']}", [turn["id"]], score=0.1) for turn in turns],
            "ops": [op("create", events=[0], score=0.2, type="fact", title="Chat", body="- said lol")],
            "discarded": [f"small talk: {turn['text']}" for turn in turns],
        }

    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=chatty, clock=clock)
    for index, text in enumerate(("lol", "thanks!", "ok see you")):
        await runtime.handle_event(dm(text, f"{index}.0"))
    clock.advance(minutes=21)
    report = await runtime.memory_engine.reconcile("U1")

    assert report.discarded == ["small talk: lol", "small talk: thanks!", "small talk: ok see you"]
    assert len(report.dropped) == 4
    assert await rows(repo, "SELECT id FROM memory_events") == []
    assert await rows(repo, "SELECT id FROM memory_records") == []
    assert await rows(repo, "SELECT id FROM conversation_turns WHERE reconciled_at IS NULL") == []


async def test_mem_12_nothing_is_written_without_provenance(repo: SqliteRepository) -> None:
    def unsourced(payload):
        turn = user_turns(payload)[0]["id"]
        return {
            "events": [
                event("learned", "Has a dog named Rex", [turn]),
                event("learned", "Invented fact", ["turn-that-does-not-exist"]),
            ],
            "ops": [
                op("create", events=[], type="fact", title="Dog", body="- Has a dog named Rex"),
                op("create", events=[1], type="fact", title="Invented", body="- made up"),
            ],
        }

    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=unsourced, clock=clock)
    await runtime.handle_event(dm("my dog Rex is sick", "1.0"))
    clock.advance(minutes=21)
    report = await runtime.memory_engine.reconcile("U1")

    assert await rows(repo, "SELECT id FROM memory_records") == []
    assert [row["summary"] for row in await rows(repo, "SELECT summary FROM memory_events")] == ["Has a dog named Rex"]
    assert any("has no provenance" in line for line in report.dropped)
    assert any("no source turn" in line for line in report.dropped)
    orphans = await rows(repo, """
        SELECT e.id FROM memory_events e WHERE NOT EXISTS (
            SELECT 1 FROM memory_provenance p WHERE p.target_type = 'event' AND p.target_id = e.id)""")
    assert orphans == []


def diet_and_dinners(payload: dict) -> dict:
    """Reconciler double: a diet preference, and a dinner workstream that cites it."""
    known = {record["id"] for record in payload["records"]}
    events, ops = [], []
    for turn in user_turns(payload):
        text = turn["text"].lower()
        if "i'm vegetarian" in text or "i am vegetarian" in text:
            events.append(event("learned", "User is vegetarian", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="preference:diet", type="preference",
                          title="Diet", body="- Vegetarian", aliases=["food", "meat"]))
        if "dinner menu" in text:
            events.append(event("learned", "Planning a dinner menu for the week", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="workstream:dinner-menu", type="workstream",
                          title="Weekly dinner menu", body="- Plan dinners for the week\n- Keep every meal vegetarian",
                          from_records=["preference:diet"] if "preference:diet" in known else []))
    return {"events": events, "ops": ops, "discarded": []}


async def test_mem_13_forget_cascades_through_provenance(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(
        repo, reconcile=diet_and_dinners, clock=clock,
        script={"forget": ("forget", {"query_or_id": "preference:diet"})},
    )
    await runtime.handle_event(dm("I'm vegetarian", "1.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("plan a dinner menu for my week", "2.0", thread_ts="2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    assert "vegetarian" in (await runtime.store.get_record("U1", "workstream:dinner-menu"))["body"]

    reply = await runtime.handle_event(dm("forget that I'm vegetarian", "3.0"))

    assert (await runtime.store.get_record("U1", "preference:diet"))["status"] == "FORGOTTEN"
    statuses = {row["summary"]: row["status"] for row in await rows(repo, "SELECT summary, status FROM memory_events")}
    assert statuses["User is vegetarian"] == "RETRACTED"
    assert statuses["Planning a dinner menu for the week"] == "ACTIVE"
    workstream = await runtime.store.get_record("U1", "workstream:dinner-menu")
    assert workstream["status"] == "ACTIVE" and workstream["body"] == "- Plan dinners for the week"
    active = await rows(repo, "SELECT title || aliases || body AS text FROM memory_records WHERE status = 'ACTIVE'")
    assert active and not [row for row in active if "egetarian" in row["text"]]
    assert "egetarian" not in (await runtime.store.profile("U1"))["body"]
    assert (await runtime.memory_engine.read("U1", "workstream:dinner-menu", history=True))["history"] == []
    assert "Diet" in reply.text and "workstream:dinner-menu" in reply.text
    kinds = [row["kind"] for row in await rows(repo, "SELECT kind FROM memory_events ORDER BY created_at")]
    assert kinds[-1] == "forgotten"

    clock.advance(minutes=21)
    await runtime.memory_engine.reconcile("U1", everything=True)
    active = await rows(repo, "SELECT body FROM memory_records WHERE status = 'ACTIVE'")
    assert not [row for row in active if "egetarian" in row["body"]], "neither old turns nor the forget request are re-learned"


async def test_mem_14_why_do_you_think_that_cites_the_source(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(
        repo, reconcile=employer, clock=clock,
        script={"why": ("memory_read", {"id": "fact:employer"})},
    )
    await runtime.handle_event(dm("I work at Google", "1.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("I moved from Google to Stripe", "2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()

    await runtime.handle_event(dm("why do you think I work at Stripe?", "3.0"))

    read = tool_results(model.requests[-1].contents)[0].result
    assert read["sources"] == [{"said": "Moved from Google to Stripe", "kind": "changed", "when": "2026-10-03 12:30 UTC"}]


async def test_mem_15_follow_through_surfaces_without_progress(repo: SqliteRepository) -> None:
    def follow_through(payload):
        events, ops = [], []
        for turn in user_turns(payload):
            text = turn["text"].lower()
            if "contract" in text:
                events.append(event("commitment_made", "Chase Alex if no contract by Thursday", [turn["id"]]))
                ops.append(op("commitment_add", events=[len(events) - 1], title="Chase Alex for the contract",
                              person="Alex", next_check_at="2026-10-08T17:00:00Z", waiting_on="Alex",
                              on_no_progress="Draft a follow-up asking Alex for the contract"))
            if "lease" in text:
                events.append(event("commitment_made", "Check on the lease renewal Monday", [turn["id"]]))
                ops.append(op("commitment_add", events=[len(events) - 1], title="Check the lease renewal",
                              next_check_at="2026-10-05T09:00:00Z", on_no_progress="Remind me"))
            if "landlord replied" in text:
                lease = next(row["id"] for row in payload["open_commitments"] if "lease" in row["commitment"])
                events.append(event("commitment_progress", "Landlord replied about the lease", [turn["id"]]))
                ops.append(op("commitment_progress", events=[len(events) - 1], record_id=lease))
        return {"events": events, "ops": ops}

    clock = Clock()
    runtime, _model, client = runtime_for(repo, reconcile=follow_through, clock=clock)
    await runtime.handle_event(dm("If Alex doesn't send the contract by Thursday, remind me to chase him.", "1.0"))
    await runtime.handle_event(dm("check on the lease renewal Monday", "2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("the landlord replied", "3.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()

    chase = (await rows(repo, "SELECT * FROM interactions WHERE commitment LIKE 'Chase%'"))[0]
    assert (chase["next_check_at"], chase["waiting_on"]) == ("2026-10-08 17:00:00", "Alex")
    assert chase["on_no_progress"] == "Draft a follow-up asking Alex for the contract"

    client.posts.clear()
    clock.now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    await runtime.heartbeat.run_tick()
    assert client.posts == [], "not due yet; the lease saw progress"

    clock.now = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
    await runtime.heartbeat.run_tick()
    assert len(client.posts) == 1
    assert "Draft a follow-up asking Alex for the contract" in client.posts[0]["text"]


async def test_mem_16_raw_turns_are_dropped_after_retention(repo: SqliteRepository) -> None:
    def allergy(payload):
        if any("UNDIGESTED" in turn["text"] for turn in payload["turns"]):
            raise RuntimeError("model unavailable")
        turn = user_turns(payload)[0]
        return {"events": [event("learned", "Allergic to peanuts", [turn["id"]], occurred_at="2026-06-25T12:00:00Z")],
                "ops": [op("create", events=[0], type="fact", title="Peanut allergy", body="- Allergic to peanuts")]}

    clock = Clock(datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc))
    store = MemoryStore(repo, "T_TEST", clock)
    engine = MemoryEngine(store, FakeModel(structured=memory_structured(allergy)))
    from knappy.agent.session import Turn

    await store.append("U1", "dm:D1", Turn("user", "I'm allergic to peanuts"))
    await engine.reconcile("U1", everything=True)
    for day in range(1, 100):
        clock.now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc) + timedelta(days=day)
        turn_id = await store.append("U1", "dm:D1", Turn("user", f"day {day} chatter"))
        await store.mark_reconciled([turn_id], clock.now)
        if day == 2:
            await store.append("U1", "dm:D1", Turn("user", "UNDIGESTED thought"))

    clock.now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    await engine.nightly("U1", clock.now)

    kept = [row["content"] for row in await rows(repo, "SELECT content FROM conversation_turns ORDER BY seq")]
    assert "I'm allergic to peanuts" not in kept
    assert "UNDIGESTED thought" in kept
    assert kept[1:] == [f"day {day} chatter" for day in range(10, 100)]
    assert "day 9 chatter" not in kept
    read = await engine.read("U1", "fact:peanut-allergy")
    assert read["sources"] == [{"said": "Allergic to peanuts", "kind": "learned", "when": "2026-06-25 12:00 UTC"}]


def versioned(tag: str):
    def reconcile(payload: dict) -> dict:
        known = {record["id"] for record in payload["records"]}
        events, ops = [], []
        for turn in user_turns(payload):
            text = turn["text"].lower()
            for needle, record_id, record_type, title, fact in (
                ("vegetarian", "preference:diet", "preference", "Diet", "Vegetarian"),
                ("work at stripe", "fact:employer", "fact", "Employer", "Works at Stripe"),
                ("pottery", "fact:hobby", "fact", "Hobby", "Does pottery"),
            ):
                if needle in text and not text.startswith(("forget", "remember")):
                    events.append(event("learned", f"{fact} ({tag})", [turn["id"]], occurred_at=turn["at"]))
                    ops.append(op("update" if record_id in known else "create", events=[len(events) - 1],
                                  record_id=record_id, type=record_type, title=title, body=f"- {fact} ({tag})"))
        return {"events": events, "ops": ops}

    return reconcile


REBUILD_SCRIPT = {
    "remember": ("remember", {"text": "Prefers aisle seats", "type": "preference", "about": "seating"}),
    "forget": ("forget", {"query_or_id": "vegetarian"}),
}


async def drive(repo: SqliteRepository, reconcile, clock: Clock) -> KnappyRuntime:
    """Two days of conversation with idle reconciles, an explicit remember, a forget, and nightly passes."""
    runtime, _model, _client = runtime_for(repo, reconcile=reconcile, clock=clock, script=REBUILD_SCRIPT)
    await runtime.handle_event(dm("I'm vegetarian", "1.0"))
    clock.advance(minutes=1)
    await runtime.handle_event(dm("and I work at Stripe", "2.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("remember I like aisle seats", "3.0", thread_ts="3.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    clock.now = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    await runtime.memory_engine.tick()
    await runtime.handle_event(dm("forget that I'm vegetarian", "4.0"))
    clock.advance(minutes=1)
    await runtime.handle_event(dm("I started doing pottery", "5.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    clock.now = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    await runtime.memory_engine.tick()
    return runtime


async def snapshot(repo: SqliteRepository) -> dict[str, Any]:
    """Compiled memory, minus when the reconciler happened to run (live ticks and replay ticks differ by minutes)."""
    records = await rows(repo, "SELECT id, type, title, aliases, body, status, supersedes, expires_at FROM memory_records ORDER BY id")
    events = await rows(repo, "SELECT kind, summary, status, occurred_at FROM memory_events ORDER BY kind, summary")
    profile = (await rows(repo, "SELECT body FROM user_profile WHERE owner_user_id = 'U1'"))[0]["body"]
    return {"records": records, "events": events, "profile": profile.rsplit(" Profile generated", 1)[0]}


async def test_mem_17_rebuild_matches_a_fresh_run(tmp_path: Path, open_db) -> None:
    clock = Clock()
    repo = await open_db(tmp_path / "rebuilt.db")
    runtime = await drive(repo, versioned("v1"), clock)
    assert "(v1)" in (await runtime.store.get_record("U1", "fact:employer"))["body"]

    v2 = MemoryEngine(runtime.store, FakeModel(structured=memory_structured(versioned("v2"))))
    await v2.rebuild("U1")
    rebuilt = await snapshot(repo)

    fresh_repo = await open_db(tmp_path / "fresh.db")
    await drive(fresh_repo, versioned("v2"), Clock())
    fresh = await snapshot(fresh_repo)

    assert rebuilt == fresh
    assert not [r for r in rebuilt["records"] if r["status"] == "ACTIVE" and "egetarian" in r["body"]]
    assert {r["id"]: r["status"] for r in rebuilt["records"]}["preference:diet"] == "FORGOTTEN"
    assert "(v1)" not in json.dumps(rebuilt)
    assert {r["id"] for r in rebuilt["records"] if r["status"] == "ACTIVE"} >= {
        "fact:employer", "fact:hobby", "preference:seating", "episode_daily:2026-10-03"
    }


async def test_mem_17_rebuild_since_keeps_earlier_memory(tmp_path: Path, open_db) -> None:
    clock = Clock()
    repo = await open_db(tmp_path / "k.db")
    runtime = await drive(repo, versioned("v1"), clock)

    v2 = MemoryEngine(runtime.store, FakeModel(structured=memory_structured(versioned("v2"))))
    await v2.rebuild("U1", since=datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc))

    assert "(v1)" in (await runtime.store.get_record("U1", "fact:employer"))["body"]
    assert "(v2)" in (await runtime.store.get_record("U1", "fact:hobby"))["body"]
    assert (await runtime.store.get_record("U1", "preference:diet"))["status"] == "FORGOTTEN"
    assert await rows(repo, "SELECT id FROM memory_records WHERE id = 'fact:hobby@v1'") == []


async def test_failing_reconciler_backs_off_then_recovers(repo: SqliteRepository) -> None:
    calls: list[str] = []

    def flaky(payload):
        calls.append("call")
        if len(calls) == 1:
            raise RuntimeError("timeout")
        turn = user_turns(payload)[0]
        return {"events": [event("learned", "Lives in Lisbon", [turn["id"]])],
                "ops": [op("create", events=[0], type="fact", title="Home", body="- Lives in Lisbon")]}

    clock = Clock()
    runtime, _model, _client = runtime_for(repo, reconcile=flaky, clock=clock)
    await runtime.handle_event(dm("I live in Lisbon", "1.0"))
    clock.advance(minutes=21)
    await runtime.memory_engine.tick()
    clock.advance(minutes=1)
    await runtime.memory_engine.tick()
    assert calls == ["call"], "no retry inside the backoff window"
    assert await rows(repo, "SELECT id FROM conversation_turns WHERE reconciled_at IS NOT NULL") == []

    clock.advance(minutes=2)
    await runtime.memory_engine.tick()
    assert len(calls) == 2
    assert (await runtime.store.get_record("U1", "fact:home"))["body"] == "- Lives in Lisbon"


async def test_dm_gap_starts_a_new_segment_with_a_recap(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(repo, clock=clock)
    await runtime.handle_event(dm("the offsite is in Lisbon", "1.0"))
    clock.advance(minutes=2)
    await runtime.handle_event(dm("budget is 40k", "2.0"))
    clock.advance(hours=7)
    await runtime.handle_event(dm("morning!", "3.0"))

    request = model.requests[-1]
    assert request.contents == [UserMessage("morning!")]
    recap = request.system.split("Earlier in this conversation:\n", 1)[1]
    assert "Lisbon" in recap and "40k" in recap

    clock.advance(minutes=1)
    await runtime.handle_event(dm("what's the budget again?", "4.0"))
    assert model.requests[-1].contents[:2] == [UserMessage("morning!"), ModelTurn(text="ok")]
    assert len([request for request in model.structured_requests if request[0].__name__ == "RecapDraft"]) == 1


async def test_failed_recap_keeps_the_old_segment_out_and_retries(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(repo, clock=clock)
    drafts = model._structured
    failures = [RuntimeError("model unavailable")]

    async def flaky(schema, system, text):
        if schema.__name__ == "RecapDraft" and failures:
            raise failures.pop()
        return await drafts(schema, system, text)

    model._structured = flaky
    await runtime.handle_event(dm("the offsite is in Lisbon", "1.0"))
    clock.advance(hours=7)
    await runtime.handle_event(dm("morning!", "2.0"))
    assert "Earlier in this conversation" not in model.requests[-1].system
    clock.advance(minutes=1)
    await runtime.handle_event(dm("where is the offsite?", "3.0"))

    request = model.requests[-1]
    assert request.contents == [UserMessage("morning!"), ModelTurn(text="ok"), UserMessage("where is the offsite?")]
    assert "Lisbon" in request.system.split("Earlier in this conversation:\n", 1)[1]


async def test_long_thread_gets_a_recap_after_the_window_slides(repo: SqliteRepository) -> None:
    clock = Clock()
    runtime, model, _client = runtime_for(repo, clock=clock)
    for index in range(16):
        await runtime.handle_event(dm(f"point {index}", f"{index + 10}.0", thread_ts="9.0"))
        clock.advance(minutes=1)

    request = model.requests[-1]
    assert len(request.contents) == 21
    assert "point 0" in request.system.split("Earlier in this conversation:\n", 1)[1]
    assert UserMessage("point 0") not in request.contents


async def test_transaction_is_atomic_against_concurrent_writers(repo: SqliteRepository) -> None:
    store = MemoryStore(repo, "T_TEST", Clock())
    started = asyncio.Event()

    async def failing_batch():
        async with repo.transaction():
            await store.add_event("U1", kind="learned", summary="half-written", occurred_at=START, score=1.0, now=START, sources=[])
            await repo.upsert_contact("T_TEST", "Half Written", owner_user_id="U1")
            started.set()
            await asyncio.sleep(0.05)
            raise RuntimeError("crash mid-batch")

    async def other_writer():
        await started.wait()
        await repo.upsert_workspace("T_OTHER", "Other", "xoxb")

    results = await asyncio.gather(failing_batch(), other_writer(), return_exceptions=True)

    assert isinstance(results[0], RuntimeError)
    assert await rows(repo, "SELECT id FROM memory_events") == []
    assert await rows(repo, "SELECT id FROM contacts") == [], "a commit inside the block waits for the block"
    assert await rows(repo, "SELECT id FROM workspaces WHERE id = 'T_OTHER'") == [{"id": "T_OTHER"}]


async def test_title_and_alias_matches_outrank_body_matches(repo: SqliteRepository) -> None:
    store = MemoryStore(repo, "T_TEST", Clock())
    async with repo.transaction():
        await store.create_record("U1", record_id="fact:commute", type="fact", title="Commute",
                                  body="- Bikes past the Stripe office", source="remember", now=START, sources=[])
        await store.create_record("U1", record_id="fact:employer", type="fact", title="Employer", aliases=["stripe"],
                                  body="- Payments company", source="remember", now=START, sources=[])

    assert [hit["id"] for hit in await store.search("U1", "stripe")] == ["fact:employer", "fact:commute"]


async def test_passive_learning_survives_restart(tmp_path: Path, open_db) -> None:
    def manager(payload):
        turn = user_turns(payload)[0]
        return {"events": [event("learned", "Manager is Priya; started at Stripe", [turn["id"]])], "ops": [
            op("create", events=[0], type="person", title="Priya", body="- The user's manager at Stripe", aliases=["manager", "boss"]),
        ]}

    clock = Clock()
    repo = await open_db(tmp_path / "k.db")
    runtime, _model, _client = runtime_for(repo, reconcile=manager, clock=clock)
    await runtime.handle_event(dm("I just started at Stripe, my manager is Priya", "1.0"))
    clock.advance(minutes=30)
    await runtime.memory_engine.tick()
    await repo.close()

    repo = await open_db(tmp_path / "k.db")
    runtime, model, _client = runtime_for(repo, clock=clock)
    await runtime.handle_event(dm("who's my manager?", "2.0", thread_ts="2.0"))
    assert "Priya: The user's manager at Stripe" in model.requests[-1].system
