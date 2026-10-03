"""Spec 16: correct follow-ups, timezone briefs, deliberate silence, follow-through, and thread handoff."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from fakes import (
    FakeClock,
    FakeSlack,
    agent,
    knappy_runtime,
    member,
    memory_structured,
    seed_commitment,
)
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.brief import ProactiveDraft
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, UserMessage

# Tuesday. 15:00 UTC is daytime in UTC; 08:00 UTC is brief time there.
DAY = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
MORNING = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


async def setup(repo: SqliteRepository, at: datetime, *, slack: FakeSlack | None = None, model=None, zones=None):
    slack = slack or FakeSlack(tz="UTC")
    clock = FakeClock(at)
    runtime = knappy_runtime(repo, slack, clock, model=model)
    for owner, zone in (zones or {"U1": "UTC"}).items():
        await runtime.store.set_timezone(owner, zone)
    return runtime, slack, clock


def buttons(post: dict) -> list[str]:
    return [e["action_id"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]]


async def rows(repo: SqliteRepository, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await repo.connection.execute(sql, params)
    return [dict(row) for row in await cursor.fetchall()]


def triage(by_text: dict[str, tuple[str, float]]):
    """Triage by commitment text: (strategy, consequence). Immediate items are interrupt-worthy."""

    async def classify(candidate):
        strategy, consequence = by_text[candidate["commitment"]]
        return {"interrupt_probability": 0.9 if strategy == "immediate_dm" else 0.5, "strategy": strategy,
                "strategy_confidence": 0.9, "consequence_score": consequence}

    return ProactiveAlertTriager(classify)


async def test_pro_01_approving_sends_the_drafted_message_not_the_reminder(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2))
    runtime, slack, _ = await setup(repo, DAY)
    await runtime.heartbeat.run_tick()

    [nudge] = slack.posts
    [draft] = await rows(repo, "SELECT id, payload FROM action_drafts")
    staged = json.loads(draft["payload"])
    result = await runtime.gateway.approve(draft["id"], "U1")

    to_alex = [post["text"] for post in slack.posts if post["channel"] == "UALEX"]
    assert result.executed and to_alex == [staged["staged_content"]]
    assert to_alex[0].startswith("Hi Alex"), "written to Alex by the writer"
    assert nudge["text"] not in to_alex[0] and "send Alex the deck, due" not in to_alex[0], "not the user's reminder"


async def test_pro_02_no_slack_id_and_no_email_means_no_send_button(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2), slack_user_id=None)
    runtime, slack, _ = await setup(repo, DAY, slack=FakeSlack(tz="UTC", members=[member("USAM", "Sam Lee")]))
    await runtime.heartbeat.run_tick()

    [nudge] = slack.posts
    assert buttons(nudge) == ["btn_resolve_commitment", "btn_snooze_commitment"]
    assert "couldn't find Alex" in json.dumps(nudge["blocks"])
    assert await rows(repo, "SELECT id FROM action_drafts") == []


async def test_pro_02_email_lookup_makes_alex_reachable(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2), slack_user_id=None, email="alex@acme.test")
    slack = FakeSlack(tz="UTC", members=[member("UALEX", "Alexander Kim", email="alex@acme.test")])
    runtime, slack, _ = await setup(repo, DAY, slack=slack)
    await runtime.heartbeat.run_tick()

    assert buttons(slack.posts[0])[0] == "btn_approve_proactive_action"
    [draft] = await rows(repo, "SELECT payload FROM action_drafts")
    assert json.loads(draft["payload"])["recipient_identifier"] == "UALEX"


async def test_pro_03_brief_runs_at_eight_in_each_owners_timezone(repo: SqliteRepository) -> None:
    yesterday = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    await seed_commitment(repo, "file the Lagos permit", due=yesterday, owner="ULAGOS", contact=None)
    await seed_commitment(repo, "file the LA permit", due=yesterday, owner="ULA", contact=None)
    zones = {"ULAGOS": "Africa/Lagos", "ULA": "America/Los_Angeles"}
    runtime, slack, _ = await setup(repo, datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc), zones=zones)
    await runtime.heartbeat.run_tick()

    assert [post["channel"] for post in slack.posts] == ["DULAGOS"], "08:00 in Lagos; midnight in Los Angeles"
    assert slack.posts[0]["text"].startswith("*Brief*") and "Lagos permit" in slack.posts[0]["text"]
    assert [row["owner_user_id"] for row in await repo.list_queued_briefings("T_TEST")] == ["ULA"], "overdue waits for LA's brief"


async def test_pro_04_one_brief_per_morning_even_across_a_restart(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))
    runtime, slack, clock = await setup(repo, MORNING)
    await runtime.heartbeat.run_tick()
    clock.now += timedelta(minutes=30)
    await knappy_runtime(repo, slack, clock).heartbeat.run_tick()
    assert [post["channel"] for post in slack.posts] == ["DU1"]

    await seed_commitment(repo, "call the bank", due=clock.now + timedelta(hours=2), contact=None)
    clock.now += timedelta(minutes=30)
    await knappy_runtime(repo, slack, clock).heartbeat.run_tick()
    assert [post["text"][:7] for post in slack.posts] == ["*Brief*", "• call "], "a later item is a nudge, not a second brief"


async def test_pro_05_thread_reply_reschedules_without_a_second_commitment(repo: SqliteRepository) -> None:
    commitment = await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))

    def push(request) -> dict:
        assert commitment in request.contents[0].text, "the brief names the commitment"
        return {"commitment_id": commitment, "due": "2026-10-12T09:00:00+00:00"}

    model = FakeModel(agent({"push it to monday": ("reschedule_commitment", push)}), structured=memory_structured())
    runtime, slack, _ = await setup(repo, MORNING, model=model)
    await runtime.heartbeat.run_tick()
    brief_ts = "100.1"
    await runtime.handle_event({"text": "push it to Monday", "channel": "DU1", "channel_type": "im", "user": "U1",
                                "ts": "200.0", "thread_ts": brief_ts})

    asked = [request for request in model.requests if request.contents[-1] == UserMessage("push it to Monday")][0]
    assert isinstance(asked.contents[0], ModelTurn) and "send Alex the deck" in asked.contents[0].text
    assert await rows(repo, "SELECT commitment, due_date, last_alerted_at FROM interactions") == [
        {"commitment": "send Alex the deck", "due_date": "2026-10-12 09:00:00", "last_alerted_at": None}
    ]


async def test_pro_06_brief_falls_back_to_a_plain_list(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))
    structured = memory_structured()

    async def failing(schema, system, text):
        if schema is ProactiveDraft:
            raise RuntimeError("model down")
        return await structured(schema, system, text)

    runtime, slack, _ = await setup(repo, MORNING, model=FakeModel(agent(), structured=failing))
    await runtime.heartbeat.run_tick()

    [brief] = slack.posts
    assert brief["text"] == "*Your morning brief*\n• send Alex the deck for Alex, overdue since Tue 05:00"
    assert buttons(brief) == ["btn_resolve_commitment", "btn_snooze_commitment"], "no drafted message to send"
    assert "draft a message to Alex" in json.dumps(brief["blocks"])


async def test_pro_06_spent_budget_falls_back_without_a_model_call(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))
    model = FakeModel(agent(), structured=memory_structured())
    runtime, slack, _ = await setup(repo, MORNING, model=model)
    runtime.daily_budget_usd = 0.0
    await runtime.heartbeat.run_tick()

    assert slack.posts[0]["text"].startswith("*Your morning brief*")
    assert [schema for schema, _, _ in model.structured_requests] == []


async def test_pro_07_a_commitment_without_an_owner_is_never_sent(repo: SqliteRepository, caplog) -> None:
    await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2), owner="")
    model = FakeModel(agent(), structured=memory_structured())
    runtime, slack, _ = await setup(repo, DAY, model=model, zones={})
    caplog.set_level(logging.WARNING, logger="knappy")
    await runtime.heartbeat.run_tick()

    assert slack.posts == [] and slack.users_info_calls == 0 and model.structured_requests == []
    assert any("no owner" in record.getMessage() for record in caplog.records)


async def test_pro_08_an_empty_day_is_silent_and_logged(repo: SqliteRepository, caplog) -> None:
    await seed_commitment(repo, "send Alex the deck", due=MORNING + timedelta(days=3))
    model = FakeModel(agent(), structured=memory_structured())
    runtime, slack, _ = await setup(repo, MORNING, model=model)
    caplog.set_level(logging.INFO, logger="knappy")
    await runtime.heartbeat.run_tick()

    assert slack.posts == [] and model.structured_requests == [] and model.requests == []
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("heartbeat tick")] == [
        "heartbeat tick owner=U1 candidates=0 sent=0 queued=0 outcome=silent"
    ]


async def test_pro_09_two_immediate_dms_a_day_then_the_brief(repo: SqliteRepository) -> None:
    for index in range(5):
        await seed_commitment(repo, f"task {index}", due=DAY + timedelta(hours=2), contact=None)
    runtime, slack, clock = await setup(repo, DAY)
    [tick] = await runtime.heartbeat.run_tick()

    assert (tick.sent, tick.queued, tick.outcome) == (2, 3, "sent")
    assert len(slack.posts) == 2
    clock.now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    await runtime.heartbeat.run_tick()
    brief = slack.posts[2]["text"]
    assert len(slack.posts) == 3 and sum(f"task {index}" in brief for index in range(5)) == 3


async def test_pro_09_the_cap_resets_each_local_day(repo: SqliteRepository) -> None:
    for index in range(3):
        await seed_commitment(repo, f"task {index}", due=DAY + timedelta(hours=2), contact=None)
    runtime, slack, clock = await setup(repo, DAY)
    await runtime.heartbeat.run_tick()
    clock.now = DAY + timedelta(days=1)
    await seed_commitment(repo, "task 3", due=clock.now + timedelta(hours=2), contact=None)
    await runtime.heartbeat.run_tick()

    assert [post["text"].split(",")[0] for post in slack.posts] == ["• task 0", "• task 1", "• task 3"]
    assert [row["summary"].split(",")[0] for row in await repo.list_queued_briefings("T_TEST")] == ["task 2"]


async def test_pro_10_an_item_in_one_brief_is_not_in_the_next(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))
    runtime, slack, clock = await setup(repo, MORNING)
    await runtime.heartbeat.run_tick()
    clock.now += timedelta(days=1)
    await seed_commitment(repo, "book the venue", due=clock.now - timedelta(hours=1), contact=None)
    await runtime.heartbeat.run_tick()

    assert len(slack.posts) == 2
    assert "send Alex the deck" in slack.posts[0]["text"]
    assert "book the venue" in slack.posts[1]["text"] and "deck" not in slack.posts[1]["text"]


async def test_pro_11_follow_through_stages_a_chase_behind_approval(repo: SqliteRepository) -> None:
    commitment = await seed_commitment(repo, "get the contract from Alex", due=None)
    await repo.connection.execute(
        "UPDATE interactions SET next_check_at = ?, on_no_progress = ?, waiting_on = ? WHERE id = ?",
        ("2026-10-05 17:00:00", "draft a chase to Alex", "Alex", commitment),
    )
    runtime, slack, _ = await setup(repo, DAY)
    await runtime.heartbeat.run_tick()

    [nudge] = slack.posts
    [draft] = await rows(repo, "SELECT status, payload FROM action_drafts")
    staged = json.loads(draft["payload"])
    assert "You asked me to: draft a chase to Alex" in nudge["text"]
    assert draft["status"] == "PENDING" and staged["recipient_identifier"] == "UALEX"
    assert buttons(nudge)[0] == "btn_approve_proactive_action" and staged["staged_content"] in json.dumps(nudge["blocks"])
    assert [post for post in slack.posts if post["channel"] == "UALEX"] == [], "nothing reaches Alex before approval"


async def test_pro_11_progress_means_no_chase(repo: SqliteRepository) -> None:
    commitment = await seed_commitment(repo, "get the contract from Alex", due=None)
    await repo.connection.execute(
        "UPDATE interactions SET next_check_at = ?, on_no_progress = ? WHERE id = ?",
        ("2026-10-05 17:00:00", "draft a chase to Alex", commitment),
    )
    runtime, slack, _ = await setup(repo, DAY)
    async with repo.transaction():
        await runtime.store.add_event(
            "U1", kind="commitment_progress", summary="Alex sent a draft", occurred_at=DAY, score=0.9, now=DAY,
            sources=[], commitment_id=commitment,
        )
    await runtime.heartbeat.run_tick()

    assert slack.posts == []


@pytest.mark.parametrize(
    ("hour", "consequence", "due_in", "sent"),
    [
        (22, 1.0, 3, False),   # quiet hours: queued for the brief
        (22, 2.5, 3, True),    # serious and due before 08:00: worth waking for
        (22, 2.5, 11, False),  # serious but due after the brief: the brief is soon enough
        (3, 2.5, -2, False),   # already overdue: waits for the brief
        (20, 1.0, 3, True),    # 20:00 is not quiet
    ],
)
async def test_quiet_hours(repo: SqliteRepository, hour: int, consequence: float, due_in: int, sent: bool) -> None:
    at = datetime(2026, 10, 6, hour, 0, tzinfo=timezone.utc)
    await seed_commitment(repo, "call the bank", due=at + timedelta(hours=due_in), contact=None)
    runtime, slack, _ = await setup(repo, at)
    runtime.heartbeat.triager = triage({"call the bank": ("immediate_dm", consequence)})
    [tick] = await runtime.heartbeat.run_tick()

    assert (len(slack.posts) == 1) is sent
    assert tick.outcome == ("sent" if sent else "queued")


async def test_quiet_hours_follow_the_owner_not_the_server(repo: SqliteRepository) -> None:
    at = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)  # 15:00 UTC is 00:00 in Tokyo
    await seed_commitment(repo, "call the bank", due=at + timedelta(hours=3), contact=None)
    runtime, slack, _ = await setup(repo, at, zones={"U1": "Asia/Tokyo"})
    runtime.heartbeat.triager = triage({"call the bank": ("immediate_dm", 1.0)})
    await runtime.heartbeat.run_tick()

    assert slack.posts == []


async def test_snoozed_items_return_when_the_snooze_ends(repo: SqliteRepository) -> None:
    commitment = await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2))
    runtime, slack, clock = await setup(repo, DAY)
    await runtime.heartbeat.run_tick()
    await runtime.heartbeat.snooze(commitment)
    for hours in (1, 23):
        clock.now += timedelta(hours=hours)
        await runtime.heartbeat.run_tick()

    assert len(slack.posts) == 2, "once, then again the day after, not in between"
    assert "send Alex the deck" in slack.posts[1]["text"]


async def test_queued_items_done_or_snoozed_since_stay_out_of_the_brief(repo: SqliteRepository) -> None:
    done = await seed_commitment(repo, "file the expenses", due=DAY + timedelta(hours=11), contact=None)
    snoozed = await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=11))
    kept = await seed_commitment(repo, "book the venue", due=DAY + timedelta(hours=11), contact=None)
    runtime, slack, clock = await setup(repo, DAY)
    runtime.heartbeat.triager = triage({text: ("batch_into_morning_digest", 1.0)
                                        for text in ("file the expenses", "send Alex the deck", "book the venue")})
    await runtime.heartbeat.run_tick()
    await runtime.heartbeat.mark_done(done)
    clock.now = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)
    await runtime.heartbeat.snooze(snoozed)
    clock.now = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
    await runtime.heartbeat.run_tick()

    [brief] = slack.posts
    assert "book the venue" in brief["text"] and "expenses" not in brief["text"] and "deck" not in brief["text"]
    assert kept in json.dumps(brief["blocks"])


async def test_a_suppressed_item_is_not_triaged_again(repo: SqliteRepository) -> None:
    await seed_commitment(repo, "water the plants", due=DAY + timedelta(hours=2), contact=None)
    runtime, slack, clock = await setup(repo, DAY)
    calls: list[dict] = []

    async def classify(candidate):
        calls.append(candidate)
        return {"interrupt_probability": 0.1, "strategy": "suppress_low_value", "strategy_confidence": 0.9,
                "consequence_score": 0.1}

    runtime.heartbeat.triager = ProactiveAlertTriager(classify)
    for _ in range(3):
        await runtime.heartbeat.run_tick()
        clock.now += timedelta(minutes=15)

    assert len(calls) == 1 and slack.posts == []


async def test_a_proactive_message_is_the_first_turn_of_its_thread(repo: SqliteRepository) -> None:
    commitment = await seed_commitment(repo, "send Alex the deck", due=DAY + timedelta(hours=2))
    runtime, slack, _ = await setup(repo, DAY)
    await runtime.heartbeat.run_tick()

    [turn] = await rows(repo, "SELECT conversation_key, role, content, slack_ts FROM conversation_turns")
    [draft] = await rows(repo, "SELECT payload FROM action_drafts")
    assert (turn["conversation_key"], turn["role"], turn["slack_ts"]) == ("thread:DU1:100.1", "assistant", "100.1")
    assert slack.posts[0]["text"] in turn["content"] and commitment in turn["content"]
    assert json.loads(draft["payload"])["staged_content"] in turn["content"]


async def test_done_in_a_brief_settles_only_that_item(repo: SqliteRepository) -> None:
    from fakes import FakeApp
    from knappy.slack.actions import register_actions

    first = await seed_commitment(repo, "send Alex the deck", due=MORNING - timedelta(hours=3))
    await seed_commitment(repo, "book the venue", due=MORNING - timedelta(hours=2), contact=None)
    runtime, slack, _ = await setup(repo, MORNING)
    await runtime.heartbeat.run_tick()
    app = FakeApp()
    register_actions(app, runtime)

    async def ack() -> None:
        return None

    body = {"user": {"id": "U1"}, "channel": {"id": "DU1"}, "message": {"ts": "100.1", "blocks": slack.posts[0]["blocks"]},
            "actions": [{"action_id": "btn_resolve_commitment", "value": first}]}
    await app.handlers["btn_resolve_commitment"](ack=ack, body=body, client=slack)

    shown = json.dumps(slack.shown("100.1")["blocks"])
    assert "Marked complete" in shown and "book the venue" in shown and first not in shown
    assert (await repo.get_interaction(first))["status"] == "FULFILLED"
