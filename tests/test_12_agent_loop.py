"""Spec 12: model-driven agent loop, prompt assembly, sessions, and the Slack reply lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest

from knappy.agent.loop import AgentLoop, InboundMessage
from knappy.agent.prompt import FINAL_TURN_NOTE, MemoryContext
from knappy.agent.tools import ToolRegistry
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, ToolCall, ToolResult, UserMessage
from knappy.runtime import KnappyRuntime
from knappy.slack.egress import PLACEHOLDER, build_say
from fakes import FakeSlack, HeuristicModel, dm, mention, tool_results, tool_turn


def _runtime(repo: SqliteRepository, model, client: FakeSlack | None = None, **kwargs) -> KnappyRuntime:
    client = client or FakeSlack()
    return KnappyRuntime(
        repo, workspace_id="T_TEST", model=model, say=build_say(client), slack=client, **kwargs
    )


async def _commitment(repo: SqliteRepository, owner: str, text: str, contact: str = "Alex") -> str:
    _contact, interaction_id = await repo.record_interaction(
        workspace_id="T_TEST",
        contact_name=contact,
        source_type="NOTE_INGEST",
        channel_id="D1",
        raw_text=text,
        summary=text,
        commitment=text,
        owner_user_id=owner,
    )
    return interaction_id


async def test_loop_01_general_question_answered_in_placeholder(repo: SqliteRepository) -> None:
    client = FakeSlack()
    model = FakeModel([ModelTurn(text="Lima is the capital of Peru.")])
    reply = await _runtime(repo, model, client).handle_event(dm("what's the capital of Peru?", "1.0"))

    assert reply.text == "Lima is the capital of Peru."
    assert [post["text"] for post in client.posts] == [PLACEHOLDER]
    assert client.shown("100.1")["text"] == "Lima is the capital of Peru."
    assert model.requests[0].contents[-1] == UserMessage("what's the capital of Peru?")


def test_loop_01_no_refusal_text_in_codebase() -> None:
    source = "\n".join(path.read_text() for path in Path("knappy").rglob("*.py"))
    assert not re.search(r"can.t answer general questions", source, re.IGNORECASE)
    assert not Path("knappy/agent/router.py").exists()


async def test_loop_02_multi_step_answer_is_the_models(repo: SqliteRepository) -> None:
    await _commitment(repo, "U1", "send the revised budget by Thursday")

    async def respond(request):
        results = tool_results(request.contents)
        if not results:
            return tool_turn("query_relationship_graph", {"contact_name": "Alex"})
        if len(results) == 1:
            return tool_turn("search_commitments", {"query": "budget"})
        promised = results[1].result[0]["commitment"]
        return ModelTurn(text=f"Alex is waiting on you to {promised}.")

    model = FakeModel(respond)
    reply = await _runtime(repo, model).handle_event(dm("what do I owe Alex?", "1.0"))

    assert len(model.requests) == 3
    assert reply.text == "Alex is waiting on you to send the revised budget by Thursday."


async def test_loop_03_endless_tools_stop_at_max_steps(repo: SqliteRepository) -> None:
    async def respond(request):
        if not request.tools:
            return ModelTurn(text="I searched a lot but found nothing about widgets.")
        return tool_turn("search_commitments", {"query": "widgets"})

    client = FakeSlack()
    model = FakeModel(respond)
    reply = await _runtime(repo, model, client).handle_event(dm("find the widgets", "1.0"))

    assert len(model.requests) == AgentLoop.MAX_STEPS + 1
    assert all(request.tools for request in model.requests[:-1])
    assert model.requests[-1].tools == []
    assert FINAL_TURN_NOTE in model.requests[-1].system
    assert model.requests[-1].contents[-1] == UserMessage(FINAL_TURN_NOTE), "a closing turn asks for text"
    assert client.shown("100.1")["text"] == reply.text == "I searched a lot but found nothing about widgets."


async def test_loop_03_wall_clock_forces_final_turn(repo: SqliteRepository) -> None:
    async def respond(request):
        if not request.tools:
            return ModelTurn(text="Out of time; here is what I have.")
        await asyncio.sleep(0.05)
        return tool_turn("search_commitments", {"query": "x"})

    model = FakeModel(respond)
    loop = AgentLoop(ToolRegistry(repo, "T_TEST"), model, wall_clock_s=0.12)
    reply = await loop.run(InboundMessage(text="go", system="s"))

    assert reply.text == "Out of time; here is what I have."
    assert 2 <= len(model.requests) <= 4
    assert model.requests[-1].tools == []


async def test_loop_04_tool_error_goes_back_to_the_model(repo: SqliteRepository) -> None:
    async def respond(request):
        results = tool_results(request.contents)
        if not results:
            return tool_turn("search_commitments", {"query": "budget"})
        return ModelTurn(text=f"I couldn't reach your commitments ({results[0].result['error']}).")

    client = FakeSlack()
    runtime = _runtime(repo, FakeModel(respond), client)

    async def broken(**kwargs):
        raise RuntimeError("database is locked")

    runtime.tools.search_commitments = broken
    reply = await runtime.handle_event(dm("what's on the budget?", "1.0"))

    assert reply.text == "I couldn't reach your commitments (RuntimeError: database is locked)."
    assert client.shown("100.1")["text"] == reply.text


async def test_loop_05_staged_dm_posts_card_and_sends_nothing(repo: SqliteRepository) -> None:
    async def respond(request):
        results = tool_results(request.contents)
        if not results:
            return tool_turn(
                "stage_outbound_action",
                {
                    "action_type": "SEND_SLACK_DM",
                    "recipient": "Alex",
                    "summary": "Ask for the deck",
                    "staged_content": "Could you send the deck?",
                    "recipient_identifier": "U_ALEX",
                    "user_id": "U_ATTACKER",
                },
            )
        return ModelTurn(text=f"Drafted a note to Alex. {results[0].result['status']}")

    client = FakeSlack()
    model = FakeModel(respond)
    reply = await _runtime(repo, model, client).handle_event(dm("Follow up with Alex about the deck", "1.0"))

    shown = client.shown("100.1")
    action_ids = [element["action_id"] for block in shown["blocks"] if block["type"] == "actions" for element in block["elements"]]
    assert action_ids == ["btn_approve_action", "btn_edit_draft", "btn_cancel_action"]
    assert shown["blocks"][0]["text"]["text"] == reply.text
    assert "not sent" in reply.text
    assert [post["channel"] for post in client.posts] == ["D1"]
    draft = await repo.get_draft(reply.draft_id)
    assert draft["status"] == "PENDING" and draft["executed_at"] is None
    assert draft["user_id"] == "U1"
    assert "blocks" not in tool_results(model.requests[1].contents)[0].result


async def test_loop_06_same_conversation_is_processed_in_order(repo: SqliteRepository) -> None:
    async def respond(request):
        text = request.contents[-1].text
        if text == "first":
            await asyncio.sleep(0.15)
        return ModelTurn(text=f"answer to {text}")

    client = FakeSlack()
    model = FakeModel(respond)
    runtime = _runtime(repo, model, client)

    async def second():
        await asyncio.sleep(0.05)
        return await runtime.handle_event(dm("second", "2.0"))

    await asyncio.gather(runtime.handle_event(dm("first", "1.0")), second())

    assert [request.contents[-1].text for request in model.requests] == ["first", "second"]
    assert model.requests[1].contents[:2] == [UserMessage("first"), ModelTurn(text="answer to first")]
    answers = [update["text"] for update in client.updates]
    assert answers == ["answer to first", "answer to second"]


async def test_loop_06_conversations_do_not_share_turns(repo: SqliteRepository) -> None:
    model = FakeModel([ModelTurn(text="noted"), ModelTurn(text="ok"), ModelTurn(text="ok")])
    runtime = _runtime(repo, model)
    await runtime.handle_event(dm("my secret is 42", "1.0"))
    await runtime.handle_event(dm("hello", "2.0", thread_ts="1.5"))
    await runtime.handle_event(dm("hello", "3.0", user="U2", channel="D2"))

    for request in model.requests[1:]:
        texts = [getattr(item, "text", None) or "" for item in request.contents]
        assert not any("secret" in text for text in texts)


async def test_loop_07_failure_replaces_placeholder_with_reference(
    repo: SqliteRepository, caplog: pytest.LogCaptureFixture
) -> None:
    async def respond(request):
        raise RuntimeError("model exploded")

    client = FakeSlack()
    caplog.set_level(logging.INFO, logger="knappy")
    await _runtime(repo, FakeModel(respond), client).handle_event(dm("hi", "1.0"))

    shown = client.shown("100.1")["text"]
    ref = re.search(r"Reference: `([0-9a-f]{8})`", shown)
    assert ref is not None
    assert f"handle_event failed ref={ref.group(1)}" in caplog.text
    assert "model exploded" in caplog.text


async def test_loop_07_channel_failure_is_ephemeral_and_clears_reaction(repo: SqliteRepository) -> None:
    async def respond(request):
        raise RuntimeError("model exploded")

    client = FakeSlack()
    await _runtime(repo, FakeModel(respond), client).handle_event(mention("hi", "5.0"))

    assert client.posts == []
    assert "Reference:" in client.ephemerals[0]["text"]
    assert [kind for kind, _ in client.reactions] == ["add", "remove"]


class NoAgentTurns(HeuristicModel):
    async def generate(self, **kwargs):
        raise AssertionError("note: must not take a model turn")


async def test_loop_08_note_shortcut_skips_the_loop_and_is_logged(repo: SqliteRepository) -> None:
    client = FakeSlack()
    runtime = _runtime(repo, NoAgentTurns(), client)
    await runtime.handle_event(dm("note: met with Sam, promised to send the deck Friday", "1.0"))

    assert "Sam" in client.shown("100.1")["text"]
    rows = await repo.search_commitments("T_TEST", query="deck", owner_user_id="U1")
    assert rows[0]["commitment"].startswith("send the deck")
    turns = await runtime.conversations.window("U1", "dm:D1")
    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].text == "note: met with Sam, promised to send the deck Friday"


async def test_channel_mention_reacts_then_answers_ephemerally(repo: SqliteRepository) -> None:
    client = FakeSlack()
    await _runtime(repo, FakeModel([ModelTurn(text="Hi there.")]), client).handle_event(mention("hello", "7.0"))

    assert client.posts == []
    assert client.ephemerals == [{"channel": "C1", "user": "U1", "text": "Hi there."}]
    assert client.reactions == [
        ("add", {"channel": "C1", "timestamp": "7.0", "name": "eyes"}),
        ("remove", {"channel": "C1", "timestamp": "7.0", "name": "eyes"}),
    ]


async def test_placeholder_shows_tool_status_before_answer(repo: SqliteRepository) -> None:
    client = FakeSlack()
    model = FakeModel([tool_turn("search_commitments", {"query": "x"}), ModelTurn(text="Nothing due.")])
    await _runtime(repo, model, client).handle_event(dm("anything due?", "1.0"))

    assert [update["text"] for update in client.updates] == ["_checking your commitments…_", "Nothing due."]


async def test_long_answer_is_split_without_losing_text(repo: SqliteRepository) -> None:
    paragraphs = [f"Paragraph {index}. " + "word " * 300 for index in range(6)]
    answer = "\n\n".join(paragraphs)
    client = FakeSlack()
    await _runtime(repo, FakeModel([ModelTurn(text=answer)]), client).handle_event(dm("write a lot", "1.0"))

    pieces = [client.shown("100.1")["text"]] + [post["text"] for post in client.posts[1:]]
    assert len(pieces) > 1
    assert all(len(piece) <= 3000 for piece in pieces)
    assert re.sub(r"\s+", " ", " ".join(pieces)) == re.sub(r"\s+", " ", answer.strip())


async def test_tool_calls_in_one_turn_run_concurrently(repo: SqliteRepository) -> None:
    started = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def tool(mine: str, other: str):
        started[mine].set()
        await asyncio.wait_for(started[other].wait(), timeout=1)
        return [{"name": mine}]

    registry = ToolRegistry(repo, "T_TEST")

    async def call(name, arguments):
        return await (tool("a", "b") if name == "query_relationship_graph" else tool("b", "a"))

    registry.call = call
    model = FakeModel([
        ModelTurn(tool_calls=[
            ToolCall("1", "query_relationship_graph", {"contact_name": "Alex"}),
            ToolCall("2", "get_meeting_context", {"contact_name": "Alex"}),
        ]),
        ModelTurn(text="done"),
    ])
    await AgentLoop(registry, model).run(InboundMessage(text="who", system="s"))

    assert [result.result for result in tool_results(model.requests[1].contents)] == [[{"name": "a"}], [{"name": "b"}]]


async def test_prompt_has_timezone_profile_open_loops_and_recap_in_order(repo: SqliteRepository) -> None:
    await _commitment(repo, "U1", "send the revised budget")
    await _commitment(repo, "U2", "send the secret plan")

    class Memory:
        async def load(self, owner, conversation_key):
            assert (owner, conversation_key) == ("U1", "dm:D1")
            return MemoryContext(profile="Works at Stripe.", recap="We planned the offsite.")

    client = FakeSlack(tz="Asia/Tokyo")
    model = FakeModel([ModelTurn(text="a"), ModelTurn(text="b")])
    runtime = _runtime(repo, model, client, memory=Memory())
    await runtime.handle_event(dm("hi", "1.0"))
    await runtime.handle_event(dm("hi again", "2.0"))

    system = model.requests[0].system
    assert "Asia/Tokyo" in system and "UTC+0900" in system
    order = [system.index(marker) for marker in ("You are Knappy", "Current time", "Works at Stripe", "send the revised budget", "We planned the offsite")]
    assert order == sorted(order)
    assert "secret plan" not in system
    assert client.users_info_calls == 1


async def test_add_then_complete_commitment_is_owner_scoped(repo: SqliteRepository) -> None:
    due = (utc_now() + timedelta(days=2)).replace(microsecond=0)

    async def respond(request):
        results = tool_results(request.contents)
        if results:
            return ModelTurn(text=str(results[0].result))
        text = request.contents[-1].text
        if text.startswith("remind me"):
            return tool_turn("add_commitment", {"commitment": "renew passport", "due": due.isoformat()})
        commitment_id = re.search(r"\[([^\]]+)\] renew passport", request.system).group(1)
        return tool_turn("complete_commitment", {"commitment_id": commitment_id})

    model = FakeModel(respond)
    runtime = _runtime(repo, model)
    await runtime.handle_event(dm("remind me to renew my passport", "1.0"))
    row = (await repo.search_commitments("T_TEST", query="passport", owner_user_id="U1"))[0]
    assert row["due_date"] == format_ts(due)
    assert row["contact_id"] is None

    model_u2 = FakeModel([tool_turn("complete_commitment", {"commitment_id": row["id"]}), ModelTurn(text="x")])
    other = _runtime(repo, model_u2)
    await other.handle_event(dm("done with passport", "2.0", user="U2", channel="D2"))
    assert "error" in tool_results(model_u2.requests[1].contents)[0].result
    assert (await repo.get_interaction(row["id"]))["status"] == "PENDING"

    await runtime.handle_event(dm("I renewed it", "3.0"))
    assert (await repo.get_interaction(row["id"]))["status"] == "FULFILLED"


async def test_add_commitment_rejects_due_without_timezone(repo: SqliteRepository) -> None:
    model = FakeModel([
        tool_turn("add_commitment", {"commitment": "call mom", "person": "Mom", "due": "2026-10-09T17:00:00"}),
        ModelTurn(text="Which timezone?"),
    ])
    await _runtime(repo, model).handle_event(dm("remind me to call mom friday", "1.0"))

    assert "due" in tool_results(model.requests[1].contents)[0].result["error"]
    assert await repo.search_commitments("T_TEST", query="", match_text=False, owner_user_id="U1") == []


async def test_history_window_feeds_the_model_as_turns(repo: SqliteRepository) -> None:
    model = FakeModel([ModelTurn(text="first answer"), ModelTurn(text="second answer")])
    runtime = _runtime(repo, model)
    await runtime.handle_event(dm("first", "1.0"))
    await runtime.handle_event(dm("second", "2.0"))

    contents = model.requests[1].contents
    assert contents == [UserMessage("first"), ModelTurn(text="first answer"), UserMessage("second")]
    assert not any(isinstance(item, ToolResult) for item in contents)
