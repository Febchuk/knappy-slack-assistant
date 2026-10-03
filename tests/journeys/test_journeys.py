"""Spec 17 §3: the acceptance journeys, offline. Scripted model, fake Slack, a real SQLite file, restarts, and time.

Assertions are on behavior: which tools ran with which arguments, what reached Slack, and what is in the database.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeSlack, agent, event, memory_structured, op, user_turns
from journey import FakeFile, dm_channel, placeholder_replaced
from knappy.agent.prompt import IDENTITY
from knappy.llm.fake import FakeModel, GenerateRequest
from knappy.llm.types import ModelTurn, UserMessage
from knappy.memory.types import ReconcileResult

def scripted(script=None, reconcile=None) -> FakeModel:
    return FakeModel(agent(script), structured=memory_structured(reconcile))


def prompts(model: FakeModel, text: str) -> list[str]:
    """The system prompt of each first agent request made for the user message `text`."""
    return [request.system for request in model.requests if request.contents[-1] == UserMessage(text)]


def asked(model: FakeModel, text: str) -> GenerateRequest:
    """The first agent request made for the user message `text`."""
    for request in model.requests:
        if request.contents[-1] == UserMessage(text):
            return request
    raise AssertionError(f"the model was never asked {text!r}")


def memory_context(system: str) -> str:
    """The part of the system prompt that grows with history: profile, open loops, workstreams, recap."""
    return system.split(IDENTITY, 1)[1].split("\n\n", 2)[-1]


def results(capture, name: str) -> list:
    return [use.result for use in capture.called(name)]


async def test_j01_talk(journey) -> None:
    async def answer(request: GenerateRequest) -> ModelTurn:
        return ModelTurn(text=f"answer #{len(request.contents)}")

    model = FakeModel(answer)
    j = await journey(model)
    hey = await j.dm("U1", "hey")
    agenda = await j.dm("U1", "what's a good way to structure a 1:1 agenda?")

    for capture in (hey, agenda):
        assert len(capture.posts) == 1, "one message per answer: the placeholder, then edited"
        assert placeholder_replaced(capture)
    assert agenda.reply["text"] == "answer #3", "the model's own answer reaches Slack, with the first exchange as context"


async def test_j02_remember_survives_restart(journey) -> None:
    model = scripted({
        "remember i'm vegetarian": [
            ("remember", {"text": "Vegetarian", "type": "preference", "about": "diet"}),
            ("remember", {"text": "Hates early meetings", "type": "preference", "about": "meetings"}),
        ],
    })
    j = await journey(model)
    await j.dm("U1", "Remember I'm vegetarian and I hate early meetings.")
    await j.restart()
    await j.advance(days=1)
    lunch = "pick a lunch spot near Union Square and suggest a time to meet Sam"
    reply = await j.dm("U1", lunch, thread="new")

    assert [use.args["about"] for use in j.called("remember")] == ["diet", "meetings"]
    request = asked(model, lunch)
    assert [getattr(item, "text", None) for item in request.contents] == [lunch], "nothing restated"
    assert "Vegetarian" in request.system and "Hates early meetings" in request.system
    assert placeholder_replaced(reply)


def manager_at_stripe(payload: dict) -> dict:
    events, ops = [], []
    for turn in user_turns(payload):
        if "my manager is priya" in turn["text"].lower():
            events.append(event("learned", "Started at Stripe; manager is Priya", [turn["id"]]))
            ops.append(op("create", events=[0], type="person", title="Priya", body="- The user's manager at Stripe",
                          aliases=["manager", "boss"]))
            ops.append(op("create", events=[0], type="org", title="Stripe", body="- The user's employer, since October 2026",
                          aliases=["employer", "work"]))
    return {"events": events, "ops": ops, "discarded": []}


async def test_j03_learns_in_passing(journey) -> None:
    model = scripted(reconcile=manager_at_stripe)
    j = await journey(model)
    await j.dm("U1", "I just started at Stripe, my manager is Priya")
    await j.advance(minutes=30)
    await j.restart()
    await j.dm("U1", "who's my manager?", thread="new")

    assert j.called("remember") == [], "nobody said remember"
    people = await j.rows("SELECT id, body FROM memory_records WHERE type = 'person' AND status = 'ACTIVE'")
    assert people == [{"id": "person:priya", "body": "- The user's manager at Stripe"}]
    said = (await j.runtime.memory_engine.read("U1", "person:priya"))["sources"]
    assert [source["said"] for source in said] == ["Started at Stripe; manager is Priya"]
    assert "Priya: The user's manager at Stripe" in asked(model, "who's my manager?").system


def employer(payload: dict) -> dict:
    known = {record["id"] for record in payload["records"]}
    events, ops = [], []
    for turn in user_turns(payload):
        text = turn["text"].lower()
        if "i work at google" in text:
            events.append(event("learned", "Works at Google", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="fact:employer", type="fact", title="Employer",
                          body="- Works at Google", aliases=["job", "work", "company"]))
        if "moved to stripe" in text:
            events.append(event("changed", "Moved from Google to Stripe", [turn["id"]]))
            ops.append(op("supersede" if "fact:employer" in known else "create", events=[len(events) - 1],
                          record_id="fact:employer", type="fact", title="Employer", body="- Works at Stripe",
                          aliases=["job", "work", "company"]))
    return {"events": events, "ops": ops, "discarded": []}


async def test_j04_correct_then_forget(journey) -> None:
    model = scripted(
        {
            "where do i work": [
                ("memory_search", {"query": "where I work"}),
                ("search_conversations", {"query": "work Google Stripe"}),
            ],
            "forget where i work": ("forget", {"query_or_id": "where I work"}),
        },
        reconcile=employer,
    )
    j = await journey(model)
    await j.dm("U1", "I work at Google")
    await j.advance(minutes=30)
    await j.dm("U1", "actually I moved to Stripe")
    await j.advance(minutes=30)
    before = await j.dm("U1", "where do I work?")
    forgot = await j.dm("U1", "forget where I work")
    after = await j.dm("U1", "where do I work?")

    found = results(before, "memory_search")[0]
    assert [hit["id"] for hit in found][:1] == ["fact:employer"]
    assert "Stripe" in found[0]["snippet"] and "Google" not in json.dumps(found)
    assert results(forgot, "forget")[0]["forgotten"] == ["Employer"]
    assert results(after, "memory_search") == [[]]
    said = [hit["text"] for hit in results(after, "search_conversations")[0]]
    assert said and not {"I work at Google", "actually I moved to Stripe", "forget where I work"} & set(said)
    last = model.requests[-2]
    assert last.contents[-1] == UserMessage("where do I work?")
    assert not re.search("stripe|google", f"{last.system} {last.contents}", re.I), "not even in earlier answers"
    assert not [text for text in await j.active_memory("U1") if re.search("stripe|google", text, re.I)]


@pytest.mark.xfail(strict=True, reason="spec 14")
async def test_j05_research(journey) -> None:
    model = scripted({"what's the latest python release": ("web_search", {"query": "latest Python release"})})
    j = await journey(model)
    reply = await j.dm("U1", "what's the latest Python release and what changed?")

    searched = reply.called("web_search")
    assert searched and searched[0].args["query"]
    hits = searched[0].result
    assert isinstance(hits, list) and hits and "error" not in hits[0], "web_search is a real tool"
    assert re.search(r"https?://", reply.text), "the answer cites a source"


@pytest.mark.xfail(strict=True, reason="spec 14")
async def test_j06_read_a_link(journey) -> None:
    url = "https://docs.python.org/3/whatsnew/3.13.html"
    model = scripted({"tl;dr": ("fetch_url", {"url": url})})
    j = await journey(model)
    reply = await j.dm("U1", f"tl;dr this <{url}>")

    fetched = reply.called("fetch_url")
    assert [use.args["url"] for use in fetched] == [url]
    assert "error" not in fetched[0].result and fetched[0].result.get("text"), "the page was read"


@pytest.mark.xfail(strict=True, reason="spec 15")
async def test_j07_files_in(journey) -> None:
    model = scripted({"what did that pdf say": ("search_documents", {"query": "pricing"})})
    j = await journey(model)
    pdf = FakeFile("pricing.pdf", "application/pdf", b"%PDF-1.4 Pricing: the Pro plan is $40 per seat per month.")
    await j.dm("U1", "summarize this", files=(pdf,))
    await j.advance(days=1)
    later = await j.dm("U1", "what did that PDF say about pricing?", thread="new")

    documents = await j.rows("SELECT * FROM memory_records WHERE type = 'document' AND status = 'ACTIVE'")
    assert len(documents) == 1
    assert "$40" in json.dumps(results(later, "search_documents"))


@pytest.mark.xfail(strict=True, reason="spec 15")
async def test_j08_files_out(journey) -> None:
    model = scripted({"write me a one-page launch plan": (
        "create_document", {"title": "Q3 offsite launch plan", "markdown": "# Launch plan\n- Goals\n- Owners"}
    )})
    j = await journey(model)
    reply = await j.dm("U1", "Write me a one-page launch plan for the Q3 offsite.")

    assert len(reply.called("create_document")) == 1
    assert [upload["channel"] for upload in j.slack.uploads] == [dm_channel("U1")]
    assert "btn_approve_action" not in reply.text, "a document for the user needs no approval card"


def open_id(commitment: str):
    def read(request: GenerateRequest) -> dict:
        found = re.search(rf"\[(\S+)\] {re.escape(commitment)}", request.system)
        assert found, f"{commitment!r} is not in the open commitments"
        return {"commitment_id": found.group(1)}

    return read


async def test_j09_commitments_by_talking(journey) -> None:
    model = scripted({
        "i told alex": ("add_commitment", {"commitment": "send Alex the budget", "person": "Alex",
                                           "due": "2026-10-08T17:00:00+00:00"}),
        "i sent alex": ("complete_commitment", open_id("send Alex the budget")),
    })
    j = await journey(model)
    await j.dm("U1", "I told Alex I'd send the budget by Thursday")
    await j.dm("U1", "what do I owe people?")
    await j.dm("U1", "I sent Alex the budget")
    await j.dm("U1", "what do I owe people?")

    added = j.called("add_commitment")
    assert [(use.args["commitment"], use.args["person"]) for use in added] == [("send Alex the budget", "Alex")]
    assert [use.result["status"] for use in j.called("complete_commitment")] == ["FULFILLED"]
    owed = prompts(model, "what do I owe people?")
    assert "send Alex the budget (for Alex), due 2026-10-08 17:00:00 UTC" in owed[0]
    assert "Open commitments: none." in owed[-1]
    rows = await j.rows("SELECT commitment, status FROM interactions")
    assert rows == [{"commitment": "send Alex the budget", "status": "FULFILLED"}], "no note: ingestion"


async def test_j10_act_only_after_approval(journey) -> None:
    text = "Hi Alex, any update on the budget? Want to close it out this week."
    def draft(request: GenerateRequest) -> dict:
        mentioned = re.search(r"<@(U[A-Z0-9]+)>", request.contents[-1].text)
        assert mentioned, "the mention reaches the model, so it knows who Alex is"
        return {"action_type": "SEND_SLACK_DM", "recipient": "Alex", "recipient_identifier": mentioned.group(1),
                "summary": "Follow up with Alex about the budget", "staged_content": text}

    model = scripted({"follow up with": ("stage_outbound_action", draft)})
    j = await journey(model)
    card = await j.dm("U1", "Follow up with <@UALEX> about the budget")
    draft_id = (await j.rows("SELECT id FROM action_drafts"))[0]["id"]

    assert "btn_approve_action" in card.text and draft_id in card.text
    assert j.slack.posts and not [post for post in j.slack.posts if post["channel"] == "UALEX"], "nothing before approval"
    stranger = await j.click("U2", "btn_approve_action", draft_id)
    assert stranger.to("UALEX") == [] and stranger.ephemerals, "only the owner can approve"

    approved = await j.click("U1", "btn_approve_action", draft_id)
    again = await j.click("U1", "btn_approve_action", draft_id)

    assert [post["text"] for post in approved.to("UALEX")] == [text]
    assert again.posts == []
    assert [post["text"] for post in j.slack.posts if post["channel"] == "UALEX"] == [text], "exactly one DM to Alex"
    draft = (await j.rows("SELECT status, executed_at FROM action_drafts"))[0]
    assert draft["status"] == "APPROVED" and draft["executed_at"]


@pytest.mark.xfail(strict=True, reason="spec 16")
async def test_j11_morning_brief(journey) -> None:
    model = scripted(reconcile=lambda payload: ReconcileResult())
    j = await journey(model, at=datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc), slack=FakeSlack(tz="America/New_York"))
    await j.dm("U1", "hi")
    contact = await j.repo.upsert_contact("T_JOURNEY", "Alex", slack_user_id="UALEX", owner_user_id="U1")
    await j.repo.insert_interaction(
        workspace_id="T_JOURNEY", contact_id=contact, source_type="DIRECT_DM", channel_id=dm_channel("U1"),
        raw_text="send Alex the deck", summary="send Alex the deck", commitment="send Alex the deck",
        due_date="2026-10-07 14:00:00", owner_user_id="U1",
    )
    # 08:00 in New York on Oct 7 is 12:00 UTC; the commitment is due two hours later.
    brief = await j.advance(hours=26)

    to_owner = [post for post in brief.posts if post["channel"] in ("U1", dm_channel("U1"))]
    assert len(to_owner) == 1, "one morning brief"
    draft = (await j.rows("SELECT id, payload FROM action_drafts"))[0]
    await j.click("U1", "btn_approve_proactive_action", draft["id"])
    sent = [post["text"] for post in j.slack.posts if post["channel"] == "UALEX"]
    assert len(sent) == 1 and "Alex" in sent[0] and "you promised" not in sent[0].lower()

    thread_ts = brief.post_ts[0]
    await j.dm("U1", "actually tell him I need until Monday", thread=thread_ts)
    prompt = model.requests[-1]
    assert any("deck" in (getattr(item, "text", None) or "") for item in prompt.contents), "the brief is in context"


async def test_j12_owners_are_isolated(journey) -> None:
    model = scripted({
        "remember my manager is sam": ("remember", {"text": "My manager is Sam", "about": "manager"}),
        "remember my manager is priya": ("remember", {"text": "My manager is Priya", "about": "manager"}),
        "who's my manager": ("memory_search", {"query": "manager"}),
    })
    j = await journey(model)
    await j.dm("U1", "remember my manager is Sam")
    await j.dm("U2", "remember my manager is Priya")
    statements: list[str] = []
    await j.repo.connection.set_trace_callback(statements.append)
    first = await j.dm("U1", "who's my manager?")
    u1_statements, statements[:] = list(statements), []
    second = await j.dm("U2", "who's my manager?")
    await j.repo.connection.set_trace_callback(None)

    assert [hit["snippet"] for hit in results(first, "memory_search")[0]] == ["My manager is Sam"]
    assert [hit["snippet"] for hit in results(second, "memory_search")[0]] == ["My manager is Priya"]
    systems = prompts(model, "who's my manager?")
    assert "Sam" in systems[0] and "Priya" not in systems[0]
    assert "Priya" in systems[1] and "Sam" not in systems[1]
    assert u1_statements and statements
    assert not [sql for sql in u1_statements if "'U2'" in sql], "U1's turn never queried with U2's id"
    assert not [sql for sql in statements if "'U1'" in sql], "U2's turn never queried with U1's id"


async def test_j13_failure_is_visible(journey, caplog: pytest.LogCaptureFixture) -> None:
    async def down(*args, **kwargs):
        raise RuntimeError("model unavailable")

    model = FakeModel(down, structured=down)
    j = await journey(model)
    caplog.set_level(logging.INFO, logger="knappy")
    replies = [await j.dm("U1", "hey"), await j.dm("U1", "are you there?")]
    background = await j.advance(minutes=30)

    for reply in replies:
        assert len(reply.posts) == 1 and placeholder_replaced(reply)
        ref = re.search(r"Reference: `([0-9a-f]{8})`", reply.reply["text"])
        assert ref, reply.reply["text"]
        assert f"handle_event failed ref={ref.group(1)}" in caplog.text
    assert background.posts == [] and "memory reconcile failed" in caplog.text


FACTS = [
    ("My sister Ana lives in Porto", "person", "Ana", "- The user's sister; lives in Porto", ["sister"], "sister"),
    ("My dentist is Dr. Okafor", "person", "Dr. Okafor", "- The user's dentist", ["dentist"], "dentist"),
    ("I'm allergic to penicillin", "fact", "Allergy", "- Allergic to penicillin", ["allergic", "allergy"], "allergic"),
    ("My car is a 2019 Civic", "fact", "Car", "- Drives a 2019 Honda Civic", ["car"], "car"),
    ("I run the payments team", "fact", "Role", "- Runs the payments team", ["team", "role", "job"], "team"),
    ("My anniversary is June 14", "fact", "Anniversary", "- Anniversary on June 14", ["anniversary"], "anniversary"),
    ("I take my coffee black", "preference", "Coffee", "- Black coffee, no sugar", ["coffee"], "coffee"),
    ("I prefer aisle seats", "preference", "Seating", "- Aisle seats on flights", ["seat", "flights"], "seat"),
    ("My son Leo plays violin", "person", "Leo", "- The user's son; plays violin", ["son"], "son"),
    ("I'm training for the Berlin marathon", "fact", "Marathon", "- Training for the Berlin marathon",
     ["marathon", "running"], "marathon"),
    ("My landlord is Mr. Haddad", "person", "Mr. Haddad", "- The user's landlord", ["landlord"], "landlord"),
    ("My gym is Equinox on 14th", "fact", "Gym", "- Goes to Equinox on 14th Street", ["gym"], "gym"),
    ("I'm learning Portuguese", "fact", "Language", "- Learning Portuguese", ["portuguese", "learning"], "portuguese"),
    ("My budget for the kitchen is 30k", "fact", "Kitchen", "- Kitchen renovation budget 30k", ["kitchen"], "kitchen"),
    ("My doctor is Dr. Lin", "person", "Dr. Lin", "- The user's doctor", ["doctor"], "doctor"),
]
SMALL_TALK = ["lol", "thanks!", "ok cool", "haha nice", "good morning", "sounds good", "brb", "how's it going?"]


def facts_only(payload: dict) -> dict:
    events, ops, discarded = [], [], []
    for turn in user_turns(payload):
        fact = next((fact for fact in FACTS if fact[0] == turn["text"]), None)
        if fact is None:
            events.append(event("learned", f"Said {turn['text']}", [turn["id"]], score=0.1))
            ops.append(op("create", events=[len(events) - 1], score=0.2, type="fact", title="Chat", body=f"- {turn['text']}"))
            continue
        text, kind, title, body, aliases, _ = fact
        events.append(event("learned", text, [turn["id"]], occurred_at=turn["at"]))
        ops.append(op("create", events=[len(events) - 1], type=kind, title=title, body=body, aliases=aliases))
    return {"events": events, "ops": ops, "discarded": discarded}


async def test_j14_scales_with_history(journey) -> None:
    model = scripted(
        {f"what do you know about my {key}": ("memory_search", {"query": key}) for *_, key in FACTS},
        reconcile=facts_only,
    )
    j = await journey(model)
    turns = 0
    for day in range(60):
        for conversation in range(8):
            thread = None if conversation == 0 else "new"
            fact = FACTS[day // 4] if day % 4 == 0 and conversation == 3 else None
            text = fact[0] if fact else SMALL_TALK[(day + conversation) % len(SMALL_TALK)]
            await j.dm("U1", text, thread=thread)
            turns += 1
            j.clock.advance(minutes=2)
        await j.advance(hours=24 - 16 / 60, step=timedelta(hours=6))
    assert turns == 480

    asked_about = [FACTS[index] for index in (0, 4, 7, 11, 14)]
    answers = [await j.dm("U1", f"what do you know about my {key}", thread="new") for *_, key in asked_about]

    for fact, answer in zip(asked_about, answers):
        assert fact[3][2:] in json.dumps(results(answer, "memory_search")), fact[0]
    recalled = [request.system for request in model.requests]
    assert max(len(memory_context(system)) / 4 for system in recalled) < 6000
    kept = await j.rows("SELECT id FROM memory_records WHERE status = 'ACTIVE' AND type NOT LIKE 'episode%'")
    assert len(kept) == len(FACTS), "small talk created no records"
    episodes = await j.rows("SELECT type, COUNT(*) AS n FROM memory_records WHERE type LIKE 'episode%' GROUP BY type")
    assert {row["type"]: row["n"] for row in episodes} == {"episode_daily": len(FACTS), "episode_weekly": 8}, "nightly ran"
    assert len(await j.rows("SELECT id FROM memory_events WHERE kind = 'learned'")) == len(FACTS)


def diet(payload: dict) -> dict:
    known = {record["id"] for record in payload["records"]}
    events, ops = [], []
    for turn in user_turns(payload):
        text = turn["text"].lower()
        if "i love steak" in text:
            events.append(event("learned", "Loves steak", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="preference:diet", type="preference",
                          title="Diet", body="- Loves steak", aliases=["food", "meat"]))
        if "plan my dinners" in text:
            events.append(event("learned", "Planning dinners for the week", [turn["id"]]))
            ops.append(op("create", events=[len(events) - 1], record_id="workstream:dinners", type="workstream",
                          title="This week's dinners", body="- Plan seven dinners\n- Steak on Friday",
                          from_records=["preference:diet"]))
        if "vegetarian now" in text:
            events.append(event("changed", "Became vegetarian", [turn["id"]]))
            ops.append(op("supersede", events=[len(events) - 1], record_id="preference:diet", type="preference",
                          title="Diet", body="- Vegetarian", aliases=["food", "vegetarian"]))
            if "workstream:dinners" in known:
                ops.append(op("update", events=[len(events) - 1], record_id="workstream:dinners",
                              body="- Plan seven dinners\n- All vegetarian", from_records=["preference:diet"]))
    return {"events": events, "ops": ops, "discarded": []}


async def test_j15_reversal_without_bleed(journey) -> None:
    model = scripted(
        {
            "forget that i used to eat meat": ("forget", {"query_or_id": "steak"}),
            "what do you know about my diet": [
                ("memory_search", {"query": "diet"}),
                ("memory_read", {"id": "preference:diet", "history": True}),
            ],
        },
        reconcile=diet,
    )
    script = model._respond

    async def confirm_by_restating(request: GenerateRequest) -> ModelTurn:
        turn = await script(request)
        if request.contents[-1] != UserMessage("forget that I used to eat meat") and turn.text and "forgotten" in turn.text:
            return ModelTurn(text="Done. I've forgotten that you used to love steak.")
        return turn

    model._respond = confirm_by_restating
    j = await journey(model)
    await j.dm("U1", "I love steak")
    await j.advance(minutes=30)
    await j.dm("U1", "plan my dinners this week", thread="new")
    await j.advance(minutes=30)
    await j.dm("U1", "actually I'm vegetarian now")
    await j.advance(minutes=30)
    await j.restart()
    await j.dm("U1", "suggest a dinner", thread="new")
    suggest = asked(model, "suggest a dinner").system

    assert "Vegetarian" in suggest and "steak" not in suggest.lower()
    workstream = await j.runtime.memory_engine.read("U1", "workstream:dinners")
    assert workstream["body"] == "- Plan seven dinners\n- All vegetarian"

    await j.dm("U1", "forget that I used to eat meat")
    known = await j.dm("U1", "what do you know about my diet?")

    assert "steak" not in json.dumps([use.result for use in known.tools]).lower(), "the answer's sources"
    assert "steak" not in known.text.lower()
    assert "steak" not in str(asked(model, "what do you know about my diet?").contents).lower(), "nor the conversation"
    assert not [text for text in await j.active_memory("U1") if "steak" in text.lower()]
    assert "Vegetarian" in (await j.runtime.memory_engine.read("U1", "preference:diet"))["body"], "only the past was forgotten"
    assert (await j.runtime.memory_engine.read("U1", "workstream:dinners"))["body"] == workstream["body"]
    events = {row["summary"]: row["status"] for row in await j.rows("SELECT summary, status FROM memory_events")}
    assert (events["Loves steak"], events["Planning dinners for the week"]) == ("RETRACTED", "ACTIVE")


async def test_j16_quiet_week(journey) -> None:
    model = scripted({
        "remind me to send sam": ("add_commitment", {"commitment": "send Sam the photos", "person": "Sam"}),
        "sent sam": ("complete_commitment", open_id("send Sam the photos")),
    })
    j = await journey(model)
    await j.dm("U1", "haha that meeting was wild")
    await j.dm("U1", "remind me to send Sam the photos")
    await j.dm("U1", "sent Sam the photos")
    await j.repo.upsert_contact("T_JOURNEY", "Priya", owner_user_id="U1",
                                last_interaction_ts=(j.clock() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    week = await j.advance(days=7)

    assert (week.posts, week.updates, week.ephemerals) == ([], [], []), "zero unprompted Slack posts all week"


@pytest.mark.xfail(strict=True, reason="spec 16")
async def test_j16_every_quiet_tick_is_logged_silent(journey, caplog: pytest.LogCaptureFixture) -> None:
    j = await journey(scripted())
    await j.dm("U1", "haha that meeting was wild")
    caplog.set_level(logging.INFO, logger="knappy")
    await j.advance(days=1)

    ticks = [record.getMessage() for record in caplog.records if record.getMessage().startswith("heartbeat tick")]
    assert len(ticks) == 48
    assert all("owner=U1" in line and "outcome=silent" in line for line in ticks)


def chase_alex(payload: dict) -> dict:
    events, ops = [], []
    for turn in user_turns(payload):
        if "contract" in turn["text"].lower():
            events.append(event("commitment_made", "Chase Alex for the contract if nothing by Thursday", [turn["id"]]))
            ops.append(op("commitment_add", events=[0], title="Chase Alex for the contract", person="Alex",
                          next_check_at="2026-10-08T17:00:00Z", waiting_on="Alex",
                          on_no_progress="Draft a chase to Alex asking for the contract"))
    return {"events": events, "ops": ops, "discarded": []}


async def test_j17_follow_through(journey) -> None:
    j = await journey(scripted(reconcile=chase_alex))
    await j.dm("U1", "I asked Alex for the contract. If he hasn't sent it by Thursday, help me chase him.")
    quiet = await j.advance(days=3)
    assert quiet.posts == [], "nothing before Thursday"

    nudge = await j.advance(hours=12)

    assert [post["channel"] for post in nudge.posts] == ["U1"], "one DM to the owner"
    draft = (await j.rows("SELECT id, user_id, status, payload FROM action_drafts"))[0]
    assert (draft["user_id"], draft["status"]) == ("U1", "PENDING")
    assert json.loads(draft["payload"])["recipient_name"] == "Alex"
    assert "btn_approve_proactive_action" in json.dumps(nudge.posts[0]["blocks"]) and draft["id"] in json.dumps(nudge.posts)
    assert "chase to Alex" in nudge.posts[0]["text"]
    assert [post for post in j.slack.posts if post["channel"] not in ("U1", dm_channel("U1"))] == [], "nothing sent to Alex"
    later = await j.advance(hours=12)
    assert later.posts == [], "not repeated"
