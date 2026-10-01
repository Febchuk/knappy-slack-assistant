"""Spec 04: fast-path router and bounded ReAct loop."""

from __future__ import annotations

import time

import pytest

from knappy.agent.memory import ThreadMemory
from knappy.agent.react import AgentReply, ModelTurn, ReActAgent
from knappy.agent.router import IntentClassification, SystemOneRouter
from knappy.agent.tools import ToolRegistry
from knappy.db.repository import SqliteRepository


def _router(repo: SqliteRepository, classify, complete) -> tuple[SystemOneRouter, ReActAgent]:
    tools = ToolRegistry(repo, "T_TEST")
    agent = ReActAgent(tools, complete, ThreadMemory())
    return SystemOneRouter(tools, agent, classify), agent


@pytest.mark.asyncio
async def test_react_01_fast_path_skips_loop(repo: SqliteRepository) -> None:
    calls = {"react": 0, "tool": 0}
    original = ToolRegistry.search_commitments

    async def search(self, query, status="PENDING", due_before=None):
        calls["tool"] += 1
        return await original(self, query, status, due_before)

    ToolRegistry.search_commitments = search

    async def classify(query, context):
        return IntentClassification("search_commitments", 0.95, 0.2, 0.0)

    async def complete(messages):
        calls["react"] += 1
        return ModelTurn(text="should not run")

    router, _agent = _router(repo, classify, complete)
    started = time.perf_counter()
    try:
        reply = await router.route_and_execute("What did I promise to send Alex?", {"thread_ts": "A"})
    finally:
        ToolRegistry.search_commitments = original
    assert calls["tool"] == 1
    assert calls["react"] == 0
    assert time.perf_counter() - started < 0.4
    assert isinstance(reply, AgentReply)


@pytest.mark.asyncio
async def test_react_02_multihop_escalates(repo: SqliteRepository) -> None:
    seen: list[str] = []

    async def classify(query, context):
        return IntentClassification("complex_reasoning", 0.4, 1.6, 0.2)

    async def complete(messages):
        tools = [item for item in messages if item.get("role") == "tool"]
        if not tools:
            return ModelTurn(tool_name="get_meeting_context", tool_args={"contact_name": "Alex"})
        if len(tools) == 1:
            return ModelTurn(tool_name="query_relationship_graph", tool_args={"contact_name": "Alex"})
        return ModelTurn(text="Here is the combined note and commitment.")

    router, agent = _router(repo, classify, complete)
    original = agent.tools.call

    async def call(name, arguments):
        seen.append(name)
        return await original(name, arguments)

    agent.tools.call = call
    reply = await router.route_and_execute(
        "Find my last note with Alex and draft a follow-up email",
        {"thread_ts": "A", "user_id": "U1", "channel_id": "D1"},
    )
    assert seen == ["get_meeting_context", "query_relationship_graph"]
    assert "combined" in reply.text


@pytest.mark.asyncio
async def test_react_03_observation_synthesis(repo: SqliteRepository) -> None:
    async def complete(messages):
        tools = [item for item in messages if item.get("role") == "tool"]
        if not tools:
            return ModelTurn(tool_name="search_commitments", tool_args={"query": "budget"})
        return ModelTurn(text="Alex from Acme Corp promised to send the revised budget by Thursday.")

    async def search(self, query, status="PENDING", due_before=None):
        return [{"contact": "Alex", "company": "Acme Corp", "commitment": "send the revised budget by Thursday"}]

    router, agent = _router(repo, _never, complete)
    agent.tools.search_commitments = search.__get__(agent.tools, ToolRegistry)
    reply = await agent.run("budget?", {"thread_ts": "A", "user_id": "U1", "channel_id": "D1"})
    assert reply.text == "Alex from Acme Corp promised to send the revised budget by Thursday."


async def _never(query, context):
    raise AssertionError("classifier should not run")


@pytest.mark.asyncio
async def test_react_04_missing_data(repo: SqliteRepository) -> None:
    async def classify(query, context):
        return IntentClassification("search_commitments", 0.97, 0.1, 0.0)

    async def complete(messages):
        raise AssertionError("ReAct should not run for an empty fast-path result")

    router, _agent = _router(repo, classify, complete)
    reply = await router.route_and_execute("What did I promise about widgets?", {"thread_ts": "A"})
    assert "couldn't find any commitments" in reply.text
    assert "widgets" in reply.text


@pytest.mark.asyncio
async def test_react_05_stages_instead_of_sending(repo: SqliteRepository) -> None:
    async def classify(query, context):
        return IntentClassification("stage_action", 0.8, 1.4, 0.9)

    async def complete(messages):
        return ModelTurn(
            tool_name="stage_outbound_action",
            tool_args={
                "action_type": "SEND_SLACK_DM",
                "recipient": "Alex",
                "summary": "Ask for the deck",
                "payload": {"staged_content": "Could you send the deck?", "recipient_identifier": "U_ALEX"},
            },
        )

    router, _agent = _router(repo, classify, complete)
    reply = await router.route_and_execute(
        "Follow up with Alex and ask for the deck",
        {"thread_ts": "A", "user_id": "U1", "channel_id": "D1"},
    )
    assert reply.draft_id is not None
    draft = await repo.get_draft(reply.draft_id)
    assert draft is not None
    assert draft["status"] == "PENDING"
    assert draft["executed_at"] is None


def test_react_06_thread_isolation() -> None:
    memory = ThreadMemory()
    memory.append("A", "user", "secret from thread A")
    agent = ReActAgent(tools=None, complete=None, memory=memory)  # type: ignore[arg-type]
    prompt = agent.build_messages("hello", {"thread_ts": "B"})
    assert "secret from thread A" not in prompt[1]["content"]
    prompt_a = agent.build_messages("hello", {"thread_ts": "A"})
    assert "secret from thread A" in prompt_a[1]["content"]


@pytest.mark.asyncio
async def test_react_07_stops_at_three(repo: SqliteRepository) -> None:
    calls = {"n": 0}

    async def complete(messages):
        calls["n"] += 1
        return ModelTurn(tool_name="search_commitments", tool_args={"query": "ambiguous"})

    async def search(self, query, status="PENDING", due_before=None):
        return [{"note": "ambiguous"}]

    _router_obj, agent = _router(repo, _never, complete)
    agent.tools.search_commitments = search.__get__(agent.tools, ToolRegistry)
    reply = await agent.run("keep looking", {"thread_ts": "A", "user_id": "U1", "channel_id": "D1"})
    assert calls["n"] == 3
    assert "clarify" in reply.text
