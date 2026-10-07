"""Spec 18 §9: workspace awareness, offline. TEST-AWARE-01..12 against the journey harness.

Workspace messages arrive the way Slack delivers user-scoped events over Socket Mode, through the same routing
the Bolt handler uses. A keyword observer stands in for the relevance model.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fakes import FakeSlack, agent, member, memory_structured, observer
from journey import Journey, dm_channel
from knappy.awareness.relevance import Relevance
from knappy.llm.fake import FakeModel, GenerateRequest

OWNER = "U1"
MEMBERS = [
    member(OWNER, "Febe Chukwuma", display_name="febe"),
    member("U_SAM", "Sam Lee"),
    member("U_ALEX", "Alex Kim"),
    member("U_PAT", "Pat Doe"),
    member("U2", "Riley Moss"),
]
DECK = "Sam asked the user to review the launch deck by Thursday"
REQUESTS = {
    "review the deck": {"kind": "asks_user", "summary": DECK, "who": "Sam", "due": "2026-10-08T17:00:00+00:00"},
    "budget numbers": {"kind": "asks_user", "summary": "Pat asked the user for the Q4 budget numbers", "who": "Pat"},
    "launch moves": {"kind": "workstream_update", "summary": "The launch moved from Oct 14 to Oct 20", "who": "Pat"},
}


def model(rules=None, script=None, reconcile=None) -> FakeModel:
    return FakeModel(agent(script), structured=memory_structured(reconcile, observer(rules or {})))


def relevance_calls(fake: FakeModel) -> list[str]:
    return [text for schema, _system, text in fake.structured_requests if schema is Relevance]


async def watching(journey, fake: FakeModel, **kwargs) -> Journey:
    return await journey(fake, slack=FakeSlack(tz="UTC", members=MEMBERS), owner=OWNER, **kwargs)


def attention_id(summary: str):
    def read(request: GenerateRequest) -> dict:
        found = re.search(rf"\[([0-9a-f]{{12}})\] {re.escape(summary)}", request.system)
        assert found, f"{summary!r} is not in the attention list"
        return found.group(1)

    return read


async def items(j: Journey) -> list[dict]:
    return await j.rows("SELECT * FROM attention_items ORDER BY created_at, source_ts")


def database_bytes(j: Journey) -> bytes:
    return Path(j.settings.database_url.removeprefix("sqlite:///")).read_bytes()


async def test_aware_01_user_events_go_to_awareness(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    seen = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    direct = await j.workspace("U_PAT", "D_PAT", "can you send me the budget numbers today?")
    flushed = await j.advance(minutes=10)

    for capture in (seen, direct):
        assert (capture.posts, capture.ephemerals, capture.reactions, capture.updates) == ([], [], [], []), "Knappy does not reply"
    assert fake.requests == [], "the agent loop never ran"
    assert await j.rows("SELECT * FROM conversation_turns WHERE conversation_key LIKE 'thread:C_DESIGN%'") == []
    assert len(relevance_calls(fake)) == 2 and "Sam Lee: @febe can you review the deck" in relevance_calls(fake)[0]
    assert flushed.posts == []


async def test_aware_01_knappy_dm_and_mentions_keep_their_path(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    j.slack.visible.add(dm_channel(OWNER))
    dm = {"type": "message", "channel": dm_channel(OWNER), "channel_type": "im", "user": OWNER, "text": "hello there Knappy", "ts": "1759676400.000001"}
    await j.deliver(dm)
    mention = {"type": "message", "channel": "C_DESIGN", "channel_type": "channel", "user": "U_SAM",
               "text": "<@UBOT> please review the deck", "ts": "1759676400.000002"}
    await j.deliver(mention)
    await j.advance(minutes=10)

    assert len(fake.requests) == 1, "the owner's DM with Knappy reached the agent loop once"
    assert relevance_calls(fake) == [], "neither message was read by awareness"


async def test_aware_02_one_message_processed_once(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    first = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.deliver({**first.event}, is_bot=True)
    await j.advance(minutes=10)
    await j.deliver({**first.event}, is_bot=True)
    await j.advance(minutes=10)
    await j.restart()
    await j.advance(hours=2)

    texts = relevance_calls(fake)
    assert len(texts) == 1 and texts[0].count(first.event["ts"]) == 1
    assert len(await items(j)) == 1
    assert len(await j.rows("SELECT * FROM memory_events")) == 1


async def test_aware_03_chatter_leaves_no_trace(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    chatter = [f"lunch idea number {index} is tacos again" for index in range(30)]
    for line in chatter:
        await j.workspace("U_PAT", "C_RANDOM", line)
    await j.advance(minutes=1)
    await j.close()

    assert len(relevance_calls(fake)) == 1, "one light call for the full buffer"
    await j.start()
    assert await j.rows("SELECT * FROM memory_events") == []
    assert await items(j) == []
    stored = database_bytes(j)
    assert not [line for line in chatter if line.encode() in stored], "raw text never reaches the database"


async def test_aware_04_request_becomes_attention_item(journey) -> None:
    j = await watching(journey, model(REQUESTS))
    asked = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(minutes=10)

    [item] = await items(j)
    ts = asked.event["ts"]
    assert (item["kind"], item["status"], item["who"], item["who_slack_id"]) == ("asks_user", "OPEN", "Sam", "U_SAM")
    assert item["due_at"] == "2026-10-08 17:00:00" and item["channel_name"] == "#design"
    assert item["permalink"] == f"https://slack.test/archives/C_DESIGN/p{ts.replace('.', '')}"
    [source] = await j.rows("SELECT source_type, source_id FROM memory_provenance WHERE target_type = 'event'")
    assert source == {"source_type": "slack_message", "source_id": f"C_DESIGN:{ts}"}
    [event] = await j.rows("SELECT summary, metadata FROM memory_events")
    assert event["summary"] == DECK and json.loads(event["metadata"])["permalink"] == item["permalink"]
    j_bytes = database_bytes(j)
    assert b"can you review the deck by Thursday" not in j_bytes


async def test_aware_05_reply_in_thread_answers_it(journey) -> None:
    j = await watching(journey, model(REQUESTS))
    asked = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(minutes=10)
    await j.workspace(OWNER, "C_DESIGN", "yes, on it today", thread=asked.event["ts"])
    await j.advance(minutes=10)
    morning = await j.advance(hours=19)

    [item] = await items(j)
    assert item["status"] == "ANSWERED" and item["resolved_at"]
    assert morning.posts == [], "nothing left for the brief"


async def test_aware_05_dm_reply_answers_it(journey) -> None:
    j = await watching(journey, model(REQUESTS))
    await j.workspace("U_PAT", "D_PAT", "can you send me the budget numbers today?")
    await j.workspace(OWNER, "D_PAT", "sent!")
    await j.advance(minutes=10)

    [item] = await items(j)
    assert item["status"] == "ANSWERED", "a request and its answer in one batch"


async def test_aware_06_fulfilled_chase_is_closed(journey) -> None:
    rules = {"contract attached": lambda context: {
        "kind": "commitment_moved", "summary": "Alex sent the contract", "who": "Alex", "completed": True,
        "commitment_id": context["open_commitments"][0]["id"],
    }}
    j = await watching(journey, model(rules))
    contact = await j.repo.upsert_contact("T_JOURNEY", "Alex", slack_user_id="U_ALEX", owner_user_id=OWNER)
    chase = await j.repo.insert_interaction(
        workspace_id="T_JOURNEY", contact_id=contact, source_type="DIRECT_DM", channel_id=dm_channel(OWNER),
        raw_text="Chase Alex for the contract", summary="Chase Alex for the contract", commitment="Chase Alex for the contract",
        owner_user_id=OWNER,
    )
    await j.runtime.store.set_commitment_plan(
        chase, next_check_at=datetime(2026, 10, 8, 17, tzinfo=timezone.utc), on_no_progress="Draft a chase to Alex", waiting_on="Alex"
    )
    await j.advance(days=1, hours=2)
    await j.workspace("U_ALEX", "D_ALEX", "here you go, contract attached")
    week = await j.advance(days=3)

    [row] = await j.rows("SELECT status FROM interactions")
    assert row["status"] == "FULFILLED"
    [done] = await j.rows("SELECT kind, commitment_id FROM memory_events")
    assert done == {"kind": "commitment_done", "commitment_id": chase}
    assert week.posts == [], "no chase on Thursday"


async def test_aware_07_brief_sections(journey) -> None:
    j = await watching(journey, model(REQUESTS))
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.workspace("U_PAT", "D_PAT", "can you send me the budget numbers this week?")
    await j.workspace("U_PAT", "C_LAUNCH", "heads up: the launch moves to Oct 20")
    await j.advance(minutes=10)
    brief = await j.advance(hours=18, minutes=30)

    [post] = brief.posts
    blocks = json.dumps(post["blocks"])
    assert blocks.count("btn_attention_done") == 2 and blocks.count("btn_attention_reply") == 2
    assert blocks.count("Open in Slack") == 3, "each item links to its message"
    assert "The launch moved from Oct 14 to Oct 20" in blocks
    later = await j.advance(days=1)
    assert "launch moved" not in json.dumps(later.posts), "an update is worth knowing once"


async def test_aware_07_brief_buttons(journey) -> None:
    j = await watching(journey, model(REQUESTS))
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.workspace("U_PAT", "D_PAT", "can you send me the budget numbers this week?")
    await j.advance(minutes=10)
    await j.advance(hours=18, minutes=30)
    deck, budget = await items(j)

    await j.click(OWNER, "btn_attention_done", deck["id"])
    await j.click(OWNER, "btn_attention_snooze", budget["id"])
    tomorrow = await j.advance(days=1)
    after = await j.advance(days=1)

    assert tomorrow.posts == [], "one item done, the other snoozed past the next brief"
    assert [post["channel"] for post in after.posts] == [dm_channel(OWNER)]
    assert "Q4 budget" in json.dumps(after.posts) and DECK not in json.dumps(after.posts), "the snooze ran out"


async def test_aware_08_whats_waiting_on_me(journey) -> None:
    fake = model(REQUESTS, script={"what's waiting on me": ("list_attention", {})})
    j = await watching(journey, fake)
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(minutes=10)
    reply = await j.dm(OWNER, "what's waiting on me?")

    [use] = reply.called("list_attention")
    [item] = use.result["items"]
    assert item["summary"] == DECK and item["link"].startswith("https://slack.test/archives/C_DESIGN/")
    assert item["link"] in reply.reply["text"]
    assert re.search(rf"Waiting on you \(attention id in brackets\):\n- \[[0-9a-f]{{12}}\] {DECK}", fake.requests[0].system)


async def test_aware_09_stop_watching(journey) -> None:
    def workstreams(payload: dict) -> dict:
        turns = [turn for turn in payload["turns"] if turn["conversation"] == "awareness:C_RANDOM"]
        if not turns:
            return {"events": [], "ops": [], "discarded": []}
        return {
            "events": [{"kind": "learned", "summary": "Offsite planning is underway", "occurred_at": "2026-10-05T14:00:00Z",
                        "source_turn_ids": [turns[0]["id"]], "admission_score": 0.8}],
            "ops": [{"op": "create", "type": "workstream", "title": "Offsite", "body": "- Planning underway",
                     "from_events": [0], "admission_score": 0.8, "reason": "test"}],
            "discarded": [],
        }

    rules = {**REQUESTS, "offsite": {"kind": "fyi", "summary": "The team offsite is being planned for November"}}
    fake = model(rules, script={"stop watching": ("stop_watching", {"conversation": "#random"})}, reconcile=workstreams)
    j = await watching(journey, fake)
    await j.workspace("U_PAT", "C_RANDOM", "we are planning the offsite for November")
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(hours=1)
    assert [row["title"] for row in await j.rows("SELECT title FROM memory_records WHERE status = 'ACTIVE'")] == ["Offsite"]

    stopped = await j.dm(OWNER, "stop watching #random")
    calls_before = len(relevance_calls(fake))
    await j.workspace("U_PAT", "C_RANDOM", "the offsite venue is booked now, everyone")
    await j.advance(minutes=10)
    await j.restart()
    await j.advance(hours=2)

    assert stopped.called("stop_watching")[0].result["stopped_watching"] == "#random"
    assert len(relevance_calls(fake)) == calls_before, "nothing more read from #random, not even by catch-up"
    assert not [call for call in j.user_slack.api_calls if call.startswith("conversations.history C_RANDOM")][1:]
    events = await j.rows("SELECT summary, status FROM memory_events WHERE kind != 'forgotten' ORDER BY created_at")
    assert {row["summary"]: row["status"] for row in events} == {
        "The team offsite is being planned for November": "RETRACTED",
        "Offsite planning is underway": "RETRACTED",
        DECK: "ACTIVE",
    }
    assert await j.rows("SELECT * FROM memory_records WHERE status = 'ACTIVE'") == []
    assert await j.rows("SELECT * FROM conversation_turns WHERE conversation_key = 'awareness:C_RANDOM'") == []
    assert [row["channel_id"] for row in await items(j)] == ["C_DESIGN"]


async def test_aware_10_reply_posts_as_the_user_after_approval(journey) -> None:
    fake = model(REQUESTS, script={"reply to sam": ("stage_outbound_action", lambda request: {
        "action_type": "POST_THREAD_REPLY", "attention_id": attention_id(DECK)(request), "recipient": "Sam",
        "summary": "Reply to Sam", "staged_content": "Thursday works, I'll have notes by then.",
    })})
    j = await watching(journey, fake)
    asked = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(minutes=10)
    staged = await j.dm(OWNER, "reply to Sam that Thursday works")

    assert j.user_slack.posts == [] and [post for post in j.slack.posts if post["channel"] == "C_DESIGN"] == []
    assert "Post as you in #design" in staged.text
    [draft] = await j.rows("SELECT id, action_type, status FROM action_drafts")
    assert draft["action_type"] == "POST_THREAD_REPLY"

    stranger = await j.click("U2", "btn_approve_action", draft["id"])
    assert j.user_slack.posts == [] and "Unauthorized" in json.dumps(stranger.ephemerals)
    await j.click(OWNER, "btn_approve_action", draft["id"])
    await j.click(OWNER, "btn_approve_action", draft["id"])

    assert j.user_slack.posts == [{
        "channel": "C_DESIGN", "text": "Thursday works, I'll have notes by then.", "thread_ts": asked.event["ts"],
    }], "posted once, as the user, in the request's thread"
    assert [post for post in j.slack.posts if post["channel"] == "C_DESIGN"] == [], "never as the bot"


async def test_aware_10_reply_needs_the_token_owner(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    draft_id = await j.repo.create_draft(
        workspace_id="T_JOURNEY", user_id="U2", channel_id=dm_channel("U2"), action_type="POST_THREAD_REPLY",
        payload={"action_type": "POST_THREAD_REPLY", "recipient_identifier": "C_DESIGN", "recipient_name": "#design",
                 "staged_content": "posted as someone else", "metadata": {"reply_thread_ts": ""}},
    )
    j.slack.posts.append({"channel": dm_channel("U2"), "text": "card", "blocks": [{"type": "actions", "elements": [
        {"action_id": "btn_approve_action", "value": draft_id}]}]})
    await j.click("U2", "btn_approve_action", draft_id)

    assert j.user_slack.posts == [], "the user token posts only for its owner"
    [draft] = await j.rows("SELECT status FROM action_drafts")
    assert draft["status"] == "FAILED"


async def test_aware_11_no_user_token(journey, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="knappy")
    fake = model(REQUESTS, script={"what's waiting": ("list_attention", {})})
    j = await journey(fake, slack=FakeSlack(tz="UTC", members=MEMBERS))
    await j.restart()

    assert j.runtime.awareness is None
    assert caplog.text.count("workspace awareness off") == 2, "logged once per start"
    event = {"type": "message", "channel": "C_DESIGN", "channel_type": "channel", "user": "U_SAM",
             "text": f"<@{OWNER}> can you review the deck by Thursday?", "ts": "1759676400.000001"}
    from knappy.slack.events import on_message

    async def ack() -> None:
        return None

    await on_message(event, ack, processor=j.runtime.handle_event, awareness=j.runtime.awareness)
    reply = await j.dm(OWNER, "what's waiting on me?")
    assert relevance_calls(fake) == [], "nothing read the channel message"
    assert all("review the deck" not in str(request.contents) for request in fake.requests), "nor did the agent"
    assert reply.called("list_attention")[0].result["items"] == []
    assert "Waiting on you" not in fake.requests[0].system


async def test_aware_12_owners_are_isolated(journey) -> None:
    fake = model(REQUESTS, script={
        "what's waiting": ("list_attention", {}),
        "reply to sam": ("stage_outbound_action", lambda request: {
            "action_type": "POST_THREAD_REPLY", "attention_id": stolen[0], "recipient": "Sam", "summary": "x",
            "staged_content": "hijack",
        }),
    })
    j = await watching(journey, fake)
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    await j.advance(minutes=10)
    stolen = [(await items(j))[0]["id"]]
    await j.runtime.attention.upsert_item(
        "U2", kind="asks_user", summary="Jo asked Riley for the roadmap", who="Jo", who_slack_id="U_JO",
        channel_id="C_ROADMAP", channel_name="#roadmap", thread_ts=None, source_ts="1.000001", permalink=None,
        due_at=None, urgency="low", now=j.clock(),
    )
    await j.repo.connection.commit()
    statements: list[str] = []
    await j.repo.connection.set_trace_callback(statements.append)
    mine = await j.dm("U2", "what's waiting on me?")
    staged = await j.dm("U2", "reply to Sam about the deck")
    await j.repo.connection.set_trace_callback(None)

    assert [item["summary"] for item in mine.called("list_attention")[0].result["items"]] == ["Jo asked Riley for the roadmap"]
    assert DECK not in fake.requests[0].system and "Jo asked Riley for the roadmap" in fake.requests[0].system
    assert "error" in staged.called("stage_outbound_action")[0].result
    assert await j.rows("SELECT * FROM action_drafts") == []
    assert not [sql for sql in statements if f"'{OWNER}'" in sql and "attention_items" in sql]


async def test_aware_threshold(journey) -> None:
    rules = {
        "maybe relevant": {"kind": "fyi", "summary": "Borderline context", "relevance": 0.49},
        "just relevant": {"kind": "fyi", "summary": "Context at the bar", "relevance": 0.5},
    }
    j = await watching(journey, model(rules))
    await j.workspace("U_PAT", "C_LAUNCH", "this is maybe relevant to you")
    await j.workspace("U_PAT", "C_LAUNCH", "and this is just relevant enough")
    await j.advance(minutes=10)

    assert [row["summary"] for row in await j.rows("SELECT summary FROM memory_events")] == ["Context at the bar"]


async def test_aware_structural_filter(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    await j.workspace("U_PAT", "C_LAUNCH", "deploy finished successfully today", bot=True)
    await j.workspace("U_PAT", "C_LAUNCH", "lol")
    await j.workspace("U_PAT", "C_LAUNCH", ":tada: :tada: :rocket:")
    await j.workspace(OWNER, "C_LAUNCH", "ok")
    await j.deliver({"type": "message", "subtype": "channel_join", "channel": "C_LAUNCH", "channel_type": "channel",
                     "user": "U_PAT", "text": "<@U_PAT> has joined the channel", "ts": "1759676400.000009"})
    await j.advance(minutes=10)

    [text] = relevance_calls(fake)
    lines = text.split("Messages:\n", 1)[1].splitlines()
    assert lines == [line for line in lines if "(the user)" in line] and len(lines) == 1, "only the owner's own message"


async def test_aware_edit_keeps_latest_version(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    first = await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the dek by Thursday?")
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?", edit_of=first.event["ts"])
    await j.advance(minutes=10)
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday!", edit_of=first.event["ts"])
    await j.advance(minutes=10)

    [text] = relevance_calls(fake)
    assert "review the deck" in text and "dek" not in text, "one pass, on the latest version; a typo fix is not news"
    assert len(await items(j)) == 1


async def test_aware_over_budget_holds_newest(journey) -> None:
    fake = model(REQUESTS)
    j = await watching(journey, fake)
    await j.repo.add_model_usage("T_JOURNEY", OWNER, input_tokens=0, output_tokens=0, cost_usd=5.0)
    for index in range(40):
        await j.workspace("U_PAT", "C_RANDOM", f"chatter line {index} about nothing much")
    await j.advance(minutes=10)

    assert relevance_calls(fake) == [], "over budget: no calls"
    held = j.runtime.awareness._buffers["C_RANDOM"]
    assert len(held) == 30 and "chatter line 39 about nothing much" in [message.text for message in held.values()]


async def test_aware_migrates_an_older_database(tmp_path: Path) -> None:
    import sqlite3

    from knappy.db.repository import SqliteRepository
    from knappy.db.schema import SQLITE_SCHEMA

    old = (
        SQLITE_SCHEMA.replace(", 'slack_message'", "").replace(", 'POST_THREAD_REPLY'", "")
        .split("CREATE TABLE IF NOT EXISTS attention_items")[0]
    )
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as raw:
        raw.executescript(old)
        raw.execute("INSERT INTO workspaces (id, team_name, bot_token) VALUES ('T', 'T', 'x')")
        raw.execute("INSERT INTO memory_provenance VALUES ('event', 'e1', 'turn', 't1', 'U1')")
        raw.execute("INSERT INTO action_drafts (id, workspace_id, user_id, channel_id, action_type, payload) VALUES ('d1', 'T', 'U1', 'D1', 'SEND_SLACK_DM', '{}')")
        assert "slack_message" not in raw.execute("SELECT sql FROM sqlite_master WHERE name = 'memory_provenance'").fetchone()[0]

    for _start in range(2):
        repo = SqliteRepository(str(path))
        await repo.connect()
        await repo.init_schema()
        await repo.close()
    repo = SqliteRepository(str(path))
    await repo.connect()
    await repo.connection.execute("INSERT INTO memory_provenance VALUES ('event', 'e2', 'slack_message', 'C1:1.0', 'U1')")
    await repo.create_draft(workspace_id="T", user_id="U1", channel_id="D1", action_type="POST_THREAD_REPLY", payload={})
    cursor = await repo.connection.execute("SELECT source_type FROM memory_provenance ORDER BY target_id")
    assert [row[0] for row in await cursor.fetchall()] == ["turn", "slack_message"], "old rows kept"
    cursor = await repo.connection.execute("SELECT action_type FROM action_drafts ORDER BY id = 'd1' DESC")
    assert [row[0] for row in await cursor.fetchall()] == ["SEND_SLACK_DM", "POST_THREAD_REPLY"]
    cursor = await repo.connection.execute("PRAGMA table_info(memory_events)")
    assert "metadata" in {row[1] for row in await cursor.fetchall()}
    await repo.close()


async def test_aware_urgent_item_interrupts_once(journey) -> None:
    rules = {"prod is down": {"kind": "asks_user", "summary": "Lee needs the user to approve the hotfix now", "who": "Lee",
                              "urgency": "now", "due": "2026-10-05T16:00:00+00:00"},
             **REQUESTS}
    j = await watching(journey, model(rules))
    await j.workspace("U_PAT", "C_ENG", f"<@{OWNER}> prod is down, please approve the hotfix PR asap")
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")
    nudge = await j.advance(hours=1)
    later = await j.advance(hours=5)

    [post] = nudge.posts
    assert post["channel"] == dm_channel(OWNER) and "approve the hotfix" in post["text"] and DECK not in post["text"]
    assert later.posts == [], "decided once; the rest waits for the brief"


async def test_aware_urgent_item_waits_out_quiet_hours(journey) -> None:
    rules = {"prod is down": {"kind": "asks_user", "summary": "Lee needs the user to approve the hotfix", "who": "Lee",
                              "urgency": "now"}}
    j = await watching(journey, model(rules), at=datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc))
    await j.workspace("U_PAT", "C_ENG", f"<@{OWNER}> prod is down, please approve the hotfix PR asap")
    night = await j.advance(hours=9)
    morning = await j.advance(hours=2)

    assert night.posts == []
    assert "approve the hotfix" in json.dumps(morning.posts), "in the morning brief instead"


async def test_first_run_dm_lists_what_was_found_once(journey) -> None:
    """Spec 23 §6: once the first catch-up is read, the installer hears what needs them, and only once."""
    j = await journey(model(REQUESTS), slack=FakeSlack(tz="UTC", members=MEMBERS), owner=OWNER, first_run=True)
    await j.workspace("U_SAM", "C_DESIGN", f"<@{OWNER}> can you review the deck by Thursday?")

    first = await j.advance(minutes=10)
    [welcome] = [post for post in first.posts if post["channel"] == dm_channel(OWNER)]
    assert welcome["text"].startswith("I've read your recent Slack conversations. Here's what looks like it needs you:")
    assert DECK in welcome["text"]

    await j.restart()
    later = await j.advance(minutes=30)
    assert not any("I've read your recent Slack conversations" in post["text"] for post in later.posts)
