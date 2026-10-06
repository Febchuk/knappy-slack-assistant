"""Spec 17 §3 at the live-model level: the same journeys against real Gemini, checked the way a person would.

Run with `pytest -m live_model tests/journeys`. Needs GEMINI_API_KEY (read from .env).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from fakes import FakeSlack, pdf_with
from journey import FakeFile, Journey, placeholder_replaced
from knappy.heartbeat.brief import Item
from knappy.llm.types import Model
from knappy.slack.users import Recipient

pytestmark = pytest.mark.live_model

REFUSALS = ("i can't answer", "i can only help", "i'm not able to help", "outside my scope")


class Verdict(BaseModel):
    reason: str
    passed: bool


async def judge(model: Model, requirement: str, reply: str) -> Verdict:
    """A light-tier check for what code can't decide: does this reply meet the requirement?"""
    return await model.generate_structured(
        tier="light",
        system=(
            "You check one reply from a personal assistant against one requirement. "
            "Give a one-sentence reason, then passed=true only if the reply clearly meets the requirement."
        ),
        text=f"Requirement: {requirement}\n\nReply:\n{reply}",
        schema=Verdict,
    )


async def expect(model: Model, requirement: str, reply: str) -> None:
    verdict = await judge(model, requirement, reply)
    assert verdict.passed, f"{requirement}\nreply: {reply}\njudge: {verdict.reason}"


async def test_j01_talk_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.dm("U1", "hey")
    agenda = await j.dm("U1", "what's a good way to structure a 1:1 agenda?")

    assert placeholder_replaced(agenda)
    assert not any(phrase in agenda.reply["text"].lower() for phrase in REFUSALS)
    await expect(live_model, "Gives concrete, usable advice on how to structure a 1:1 meeting agenda.", agenda.reply["text"])


async def test_j02_remember_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.dm("U1", "Remember I'm vegetarian and I hate early meetings.")
    await j.restart()
    await j.advance(days=1)
    lunch = await j.dm("U1", "pick a lunch spot near Union Square and suggest a time to meet Sam", thread="new")

    memory = " ".join(await j.active_memory("U1")).lower()
    assert "vegetarian" in memory and "early" in memory
    assert "are you vegetarian" not in lunch.reply["text"].lower()
    await expect(
        live_model,
        "Suggests a specific lunch spot near Union Square that suits a vegetarian, and proposes a meeting time of 10am or later.",
        lunch.reply["text"],
    )


async def test_j03_learn_passively_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.dm("U1", "I just started at Stripe, my manager is Priya")
    await j.advance(minutes=30)
    await j.restart()
    answer = await j.dm("U1", "who's my manager?", thread="new")

    assert any("priya" in text.lower() for text in await j.active_memory("U1"))
    assert "Priya" in answer.reply["text"]


async def test_j04_correct_and_forget_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.dm("U1", "I work at Google")
    await j.advance(minutes=30)
    await j.dm("U1", "actually I moved to Stripe")
    await j.advance(minutes=30)
    before = await j.dm("U1", "where do I work?")
    await j.dm("U1", "forget where I work")
    after = await j.dm("U1", "where do I work?")

    assert "Stripe" in before.reply["text"]
    assert j.called("forget"), "the agent used the forget tool"
    assert not [text for text in await j.active_memory("U1") if re.search("stripe|google", text, re.I)]
    await expect(
        live_model,
        "Says plainly that it does not currently know where the user works.",
        after.reply["text"],
    )


async def test_j10_act_with_approval_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.repo.upsert_contact("T_JOURNEY", "Alex", slack_user_id="UALEX", owner_user_id="U1")
    await j.dm("U1", "I told Alex I'd send the budget by Thursday")
    card = await j.dm("U1", "Follow up with Alex about the budget")
    drafts = await j.rows("SELECT id, payload FROM action_drafts")
    others = lambda: [post for post in j.slack.posts if post["channel"] != "DU1"]  # noqa: E731

    assert len(drafts) == 1 and "btn_approve_action" in card.text, card.reply["text"]
    assert others() == [], "nothing reaches Alex before approval"
    await expect(live_model, "Says a message to Alex is drafted and waiting for approval. It does not claim it was sent.",
                 card.reply["text"])

    await j.click("U1", "btn_approve_action", drafts[0]["id"])
    await j.click("U1", "btn_approve_action", drafts[0]["id"])
    sent = others()
    assert len(sent) == 1, "exactly one DM to Alex"
    assert "budget" in sent[0]["text"].lower()


async def test_j15_reversal_without_bleed_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    await j.dm("U1", "I love steak")
    await j.advance(minutes=30)
    await j.dm("U1", "plan my dinners this week", thread="new")
    await j.advance(minutes=30)
    await j.dm("U1", "actually I'm vegetarian now")
    await j.advance(minutes=30)
    await j.restart()
    dinner = await j.dm("U1", "suggest a dinner", thread="new")
    await expect(live_model, "Suggests a dinner that is vegetarian: no meat and no fish.", dinner.reply["text"])

    await j.dm("U1", "forget that I used to eat meat")
    diet = await j.dm("U1", "what do you know about my diet?")

    assert "steak" not in diet.reply["text"].lower()
    assert not [text for text in await j.active_memory("U1") if "steak" in text.lower()]
    await expect(
        live_model,
        "Describes the user's diet as vegetarian and does not mention that they used to eat meat or steak.",
        diet.reply["text"],
    )


async def test_j05_research_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    reply = await j.dm("U1", "what's the latest Python release and what changed?")

    assert reply.called("web_search"), "the agent searched the web"
    assert re.search(r"<https?://[^|>]+\|[^>]+>", reply.reply["text"]), f"cites a source as a Slack link: {reply.reply['text']}"
    await expect(live_model, "Names a specific recent Python version and says something concrete about what changed in it.",
                 reply.reply["text"])


async def test_j06_read_a_link_live(journey, live_model) -> None:
    url = "https://docs.python.org/3/whatsnew/3.13.html"
    j: Journey = await journey(live_model)
    reply = await j.dm("U1", f"tl;dr this <{url}>")

    fetched = reply.called("fetch_url")
    assert fetched and fetched[0].args["url"] == url
    assert fetched[0].result.get("text"), fetched[0].result
    await expect(live_model, "Summarizes what is new in Python 3.13 in a few points.", reply.reply["text"])


async def test_web_05_weather_cites_a_source_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    reply = await j.dm("U1", "what's the weather in Lagos today?")

    assert re.search(r"https?://", reply.reply["text"]), f"cites at least one source URL: {reply.reply['text']}"


async def test_j07_files_in_live(journey, live_model) -> None:
    j: Journey = await journey(live_model)
    pdf = FakeFile("acme-q3-proposal.pdf", "application/pdf", pdf_with(
        "Q3 proposal for Acme Corp. Scope: migrate the billing system to the new platform by September.",
        "Pricing: the Pro plan is $40 per seat per month, billed annually. Over 100 seats the price drops to $34.",
        "Timeline: kickoff on July 1, pilot in August, rollout complete by the end of September.",
    ))
    summary = await j.dm("U1", "summarize this", files=(pdf,))
    await j.advance(days=1)
    later = await j.dm("U1", "what did that PDF say about pricing?", thread="new")

    assert placeholder_replaced(summary)
    await expect(live_model, "Summarizes a billing-migration proposal for Acme, including its pricing or timeline.", summary.reply["text"])
    assert later.called("list_files") or later.called("memory_search") or later.called("read_file"), "looked the file up"
    assert not later.called("web_search")
    assert "$40" in later.reply["text"], later.reply["text"]


async def test_file_04_image_description_live(journey, live_model) -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 200), "white")
    ImageDraw.Draw(image).text((20, 80), "PRO PLAN: $40 PER SEAT", fill="black", font_size=40)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    j: Journey = await journey(live_model)
    reply = await j.dm("U1", "what does this screenshot say?", files=(FakeFile("pricing.png", "image/png", buffer.getvalue()),))

    [stored] = await j.rows("SELECT text FROM documents")
    assert "40" in stored["text"], stored["text"]
    assert "40" in reply.reply["text"], reply.reply["text"]



async def test_brief_writer_live(journey, live_model) -> None:
    """Spec 16 §3: one agent-tier call writes a short brief, and separate messages to people in the user's voice."""
    j: Journey = await journey(live_model)
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    zone = ZoneInfo("America/New_York")
    store = j.runtime.store
    async with j.repo.transaction():
        await store.create_record(
            "U1", record_id="preference:style", type="preference", title="Writing style",
            body="- Prefers terse, lowercase Slack messages with no exclamation marks", source="remember", now=now, sources=[],
        )
        await store.create_record(
            "U1", record_id="episode_daily:2026-10-06", type="episode_daily", title="Day of 2026-10-06",
            body="- Started planning the Q3 offsite\n- Waiting on Priya's headcount numbers", source="reconciler", now=now,
            sources=[],
        )
        await store.rebuild_profile("U1", now)
    items = [
        Item(kind="COMMITMENT", owner="U1", interaction_id="c1", contact_name="Alex", commitment="send Alex the Q3 deck",
             due=now + timedelta(hours=2), recipient=Recipient("Alex", "UALEX")),
        Item(kind="COMMITMENT", owner="U1", interaction_id="c2", commitment="renew passport", due=now - timedelta(days=1)),
        Item(kind="CADENCE", owner="U1", contact_id="k1", contact_name="Sam", days_quiet=45, recipient=Recipient("Sam", "USAM")),
    ]
    written = await j.runtime.heartbeat.writer.write("U1", "brief", items, now, zone)

    assert len(written.text.split()) < 150, written.text
    assert "deck" in written.text.lower() and "passport" in written.text.lower(), written.text
    assert set(written.messages) == {0, 2}, written.messages
    await expect(live_model, "A morning brief for the user that lists what is due today and overdue, and who to follow up "
                 "with. It may mention the offsite or Priya as carryover. It does not invent tasks.", written.text)
    for index, name in ((0, "Alex"), (2, "Sam")):
        await expect(live_model, f"A Slack message written by the user, in the first person, addressed to {name}, ready to "
                     "send. It is not a reminder to the user and does not mention an assistant or a promise.",
                     written.messages[index])


async def test_j11_proactive_live(journey, live_model) -> None:
    j: Journey = await journey(live_model, at=datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc),
                               slack=FakeSlack(tz="America/New_York"))
    await j.dm("U1", "hi")
    contact = await j.repo.upsert_contact("T_JOURNEY", "Alex", slack_user_id="UALEX", owner_user_id="U1")
    await j.repo.insert_interaction(
        workspace_id="T_JOURNEY", contact_id=contact, source_type="DIRECT_DM", channel_id="DU1",
        raw_text="send Alex the deck", summary="send Alex the deck", commitment="send Alex the deck",
        due_date="2026-10-07 14:00:00", owner_user_id="U1",
    )
    overnight = await j.advance(hours=25, minutes=30)
    brief = await j.advance(minutes=30)

    assert overnight.posts == [] and [post["channel"] for post in brief.posts] == ["DU1"]
    text = brief.posts[0]["text"]
    assert "deck" in text.lower() and len(text.split()) < 150, text
    [draft] = await j.rows("SELECT id, payload FROM action_drafts")
    staged = json.loads(draft["payload"])
    assert staged["recipient_identifier"] == "UALEX" and "promised" not in staged["staged_content"].lower()
    await expect(live_model, "A message to Alex, written as the user, about the deck. Not a reminder addressed to the user.",
                 staged["staged_content"])

    reply = await j.dm("U1", "actually tell him I need until Monday", thread=brief.post_ts[0])
    assert any(use.args.get("recipient", "").lower().startswith("alex") for use in reply.called("stage_outbound_action")), \
        [use.name for use in reply.tools]
    assert len(await j.rows("SELECT id FROM interactions WHERE commitment IS NOT NULL")) == 1, "no second commitment"
    assert [post for post in j.slack.posts if post["channel"] == "UALEX"] == [], "nothing sent without approval"


# Spec 18 §3: a hand-labelled 40-message workspace. Each message's expected observation kind, or None.
WORKSPACE_SAMPLE: list[tuple[str, str, str, str | None]] = [
    ("U_JO", "C_RANDOM", "anyone tried the new ramen place on 5th street?", None),
    ("U_LEE", "C_RANDOM", "the coffee machine on floor three is broken again", None),
    ("U_PAT", "C_RANDOM", "who's in for friday climbing after work?", None),
    ("U_JO", "C_RANDOM", "count me in for climbing, I need the exercise", None),
    ("U_SAM", "C_RANDOM", "happy birthday Lee, hope it's a good one!", None),
    ("U_LEE", "C_RANDOM", "thanks everyone, cake is in the kitchen", None),
    ("U_JO", "C_RANDOM", "does anyone have a spare phone charger I can borrow", None),
    ("U_PAT", "C_RANDOM", "the parking garage closes early on friday this week", None),
    ("U_ALEX", "C_RANDOM", "great article on remote work culture, worth a read", None),
    ("U_SAM", "C_RANDOM", "lunch order is going in at noon, add yours to the sheet", None),
    ("U_SAM", "C_DESIGN", "<@U1> can you review the launch deck before Thursday's sync?", "asks_user"),
    ("U_JO", "C_DESIGN", "I updated the color tokens in figma this morning", None),
    ("U_SAM", "C_DESIGN", "Jo, can you export the new icons by tomorrow?", None),
    ("U_JO", "C_DESIGN", "on it, will have the icons exported by noon", None),
    ("U_LEE", "C_DESIGN", "the hover states on the settings page look great now", None),
    ("U_SAM", "C_DESIGN", "let's keep the old illustration style for the onboarding flow", None),
    ("U_PAT", "C_LAUNCH", "heads up: the Atlas launch moves from Oct 14 to Oct 20, QA needs another week", "workstream_update"),
    ("U_PAT", "C_LAUNCH", "<@U1> you own the release notes for Atlas, please have a draft by Friday", "assigns_user"),
    ("U_JO", "C_LAUNCH", "I'll update the status page once we have the new date", None),
    ("U1", "C_LAUNCH", "Got it, I'll send the release notes draft to Pat by Thursday", "user_committed"),
    ("U_LEE", "C_LAUNCH", "load testing for the launch environment starts tonight", None),
    ("U_PAT", "C_LAUNCH", "FYI finance approved the Atlas budget this morning", "fyi"),
    ("U_SAM", "D_SAM", "hey, are you free to pair on the pricing page tomorrow afternoon?", "asks_user"),
    ("U_SAM", "D_SAM", "also I'm blocked on your API review before I can merge the billing PR", "waiting_on_user"),
    ("U_ALEX", "D_ALEX", "here you go, the signed contract is attached", "commitment_moved"),
    ("U_ALEX", "D_ALEX", "have a nice weekend when it comes", None),
    ("U_LEE", "C_ENG", "deploy of the billing service finished, no errors", None),
    ("U_LEE", "C_ENG", "rotating the staging certificates, expect a short blip", None),
    ("U_JO", "C_ENG", "flaky test in the auth suite again, I'm looking into it", None),
    ("U_ALEX", "C_ENG", "bumped the node version on CI to 22 for all repos", None),
    ("U_LEE", "C_ENG", "Jo can you pair with me on the cache bug later today?", None),
    ("U_JO", "C_ENG", "sure, ping me after standup and we can dig in", None),
    ("U_PAT", "C_ENG", "reminder: code freeze for the mobile app starts next Monday", None),
    ("U_LEE", "C_ENG", "the logging dashboard now has a dark mode toggle", None),
    ("U_ALEX", "C_GENERAL", "welcome to our new designer Morgan, say hi!", None),
    ("U_SAM", "C_GENERAL", "all hands recording is up on the wiki", None),
    ("U_PAT", "C_GENERAL", "office will be closed on the 24th for the holiday", None),
    ("U_JO", "C_GENERAL", "the book club is reading Project Hail Mary this month", None),
    ("U_LEE", "C_GENERAL", "please update your emergency contact info in the HR portal", None),
    ("U_ALEX", "C_GENERAL", "quarterly survey closes Friday, two minutes to fill in", None),
]


async def test_relevance_precision_recall_live(journey, live_model, capsys) -> None:
    from conftest import LIVE_COST_USD

    from fakes import member
    from knappy.agent.tools import current_owner

    people = [member("U1", "Febe Chukwuma", display_name="febe")] + [
        member(user, f"{user[2:].title()} Smith") for user in ("U_SAM", "U_JO", "U_PAT", "U_LEE", "U_ALEX")
    ]
    j: Journey = await journey(live_model, slack=FakeSlack(tz="UTC", members=people), owner="U1")
    contact = await j.repo.upsert_contact("T_JOURNEY", "Alex", slack_user_id="U_ALEX", owner_user_id="U1")
    chase = await j.repo.insert_interaction(
        workspace_id="T_JOURNEY", contact_id=contact, source_type="DIRECT_DM", channel_id="DU1",
        raw_text="Chase Alex for the signed contract", summary="Chase Alex for the signed contract",
        commitment="Chase Alex for the signed contract", owner_user_id="U1",
    )
    await j.runtime.store.set_commitment_plan(chase, next_check_at=None, on_no_progress=None, waiting_on="Alex")
    async with j.repo.transaction():
        await j.runtime.store.create_record(
            "U1", record_id="workstream:atlas-launch", type="workstream", title="Atlas launch",
            body="- The user is writing the release notes", source="remember", now=j.clock(), sources=[],
        )
    expected: dict[str, str | None] = {}
    said: dict[str, str] = {}
    for user, channel, text, kind in WORKSPACE_SAMPLE:
        sent = await j.workspace(user, channel, text)
        expected[f"{channel}:{sent.event['ts']}"] = kind
        said[f"{channel}:{sent.event['ts']}"] = text
    spent_before = sum(LIVE_COST_USD)
    token = current_owner.set("U1")
    await j.advance(minutes=10, step=timedelta(minutes=10))
    current_owner.reset(token)
    spent = sum(LIVE_COST_USD) - spent_before

    rows = await j.rows(
        "SELECT p.source_id, e.metadata FROM memory_provenance p JOIN memory_events e ON e.id = p.target_id "
        "WHERE p.source_type = 'slack_message'"
    )
    got = {row["source_id"]: json.loads(row["metadata"])["observation"] for row in rows}
    kinds = sorted({kind for kind in expected.values() if kind} | set(got.values()))
    with capsys.disabled():
        print(f"\nrelevance live: {len(WORKSPACE_SAMPLE)} messages, ${spent:.5f} (${spent / len(WORKSPACE_SAMPLE):.6f}/message)")
        for kind in kinds:
            predicted = {key for key, value in got.items() if value == kind}
            actual = {key for key, value in expected.items() if value == kind}
            hits = len(predicted & actual)
            print(f"  {kind:18} precision {hits}/{len(predicted)}  recall {hits}/{len(actual)}")
        relevant = {key for key, value in expected.items() if value}
        print(f"  {'any (admitted)':18} precision {len(set(got) & relevant)}/{len(got)}  recall {len(set(got) & relevant)}/{len(relevant)}")
        for key in sorted(set(got) | relevant):
            if got.get(key) != expected.get(key):
                print(f"  mismatch: expected {expected.get(key)} got {got.get(key)}: {said[key]}")
    relevant = {key for key, value in expected.items() if value}
    assert len(set(got) & relevant) / max(len(got), 1) >= 0.8, "chatter is not admitted"
    assert len(set(got) & relevant) / len(relevant) >= 0.75, "what concerns the user is kept"
    [status] = await j.rows("SELECT status FROM interactions WHERE commitment LIKE 'Chase Alex%'")
    assert status["status"] in ("FULFILLED", "PENDING")


async def test_brief_needs_you_live(journey, live_model) -> None:
    """Spec 18 §6: the brief's Needs you and Worth knowing sections, each item with its link."""
    j: Journey = await journey(live_model)
    now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    deck, budget, launch = (f"https://slack.test/archives/{path}" for path in ("C_DESIGN/p1", "D_PAT/p2", "C_LAUNCH/p3"))
    items = [
        Item(kind="ATTENTION", owner="U1", attention_id="a1", summary="Sam asked you to review the launch deck",
             permalink=deck, urgency="today", due=now + timedelta(days=1)),
        Item(kind="ATTENTION", owner="U1", attention_id="a2", summary="Pat asked for the Q4 budget numbers",
             permalink=budget, urgency="low"),
        Item(kind="UPDATE", owner="U1", event_id="e1", summary="The Atlas launch moved from Oct 14 to Oct 20", permalink=launch),
    ]
    written = await j.runtime.heartbeat.writer.write("U1", "brief", items, now, ZoneInfo("UTC"))

    assert "needs you" in written.text.lower() and "worth knowing" in written.text.lower(), written.text
    assert all(link in written.text for link in (deck, budget, launch)), written.text
    assert written.messages == {}
