"""Spec 17 §3 at the live-model level: the same journeys against real Gemini, checked the way a person would.

Run with `pytest -m live_model tests/journeys`. Needs GEMINI_API_KEY (read from .env).
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel

from journey import Journey, placeholder_replaced
from knappy.llm.types import Model

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
