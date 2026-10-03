"""Spec 11: Gemini model layer, tool calling contract, usage and budget."""

from __future__ import annotations

import json

import pytest
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from fakes import FakeSlack, dm, tool_results
from knappy.agent.loop import AgentLoop, InboundMessage
from knappy.agent.tools import ToolRegistry, current_owner
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.triage import TriageJudgment, model_triage
from knappy.llm import client as client_module
from knappy.llm.client import GeminiClient, ModelIds, to_contents
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn, ToolCall, ToolResult, UserMessage
from knappy.runtime import BUDGET_TEXT, KnappyRuntime, usage_recorder
from knappy.slack.egress import build_say

IDS = ModelIds(agent="gemini-3-flash-preview", light="gemini-3.1-flash-lite-preview")


def _response(parts: list[types.Part], prompt: int = 1000, out: int = 200, thoughts: int = 0, text_json: str | None = None):
    if text_json is not None:
        parts = [types.Part.from_text(text=text_json)]
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts
        ),
    )


class FakeSdk:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []
        self.aio = self
        self.models = self

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(client_module, "RETRY_DELAYS_S", (0.0, 0.0))


@pytest.mark.asyncio
async def test_client_parses_tool_calls_and_skips_thoughts() -> None:
    sdk = FakeSdk([
        _response([
            types.Part(text="thinking about it", thought=True),
            types.Part(function_call=types.FunctionCall(id="c1", name="search_commitments", args={"query": "deck"})),
        ])
    ])
    client = GeminiClient("k", IDS, sdk=sdk)
    turn = await client.generate(tier="agent", system="sys", contents=[UserMessage("hi")], tools=ToolRegistry(None, "T").specs())  # type: ignore[arg-type]
    assert turn.text is None
    assert turn.tool_calls == [ToolCall(id="c1", name="search_commitments", args={"query": "deck"})]
    config = sdk.calls[0]["config"]
    assert sdk.calls[0]["model"] == IDS.agent
    names = [decl.name for decl in config.tools[0].function_declarations]
    assert "stage_outbound_action" in names
    assert config.system_instruction == "sys"


@pytest.mark.asyncio
async def test_client_costs_include_thinking_tokens_and_report_usage() -> None:
    seen = []

    async def on_usage(tier, model, usage):
        seen.append((tier, model, usage))

    sdk = FakeSdk([_response([types.Part.from_text(text="ok")], prompt=1_000_000, out=100_000, thoughts=100_000)])
    turn = await GeminiClient("k", IDS, on_usage=on_usage, sdk=sdk).generate(tier="light", system="s", contents=[UserMessage("x")])
    assert turn.text == "ok"
    assert turn.usage.output_tokens == 200_000
    assert turn.usage.cost_usd == pytest.approx(0.25 + 0.2 * 1.50)
    assert seen[0][:2] == ("light", IDS.light)


def test_contents_replay_raw_model_content_and_group_tool_results() -> None:
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(id="a", name="x", args={}))])
    first, second = ToolCall("a", "x", {}), ToolCall("b", "y", {})
    contents = to_contents([
        UserMessage("hi"),
        ModelTurn(tool_calls=[first, second], raw=raw),
        ToolResult(first, [{"when": "today"}]),
        ToolResult(second, {"ok": True}),
    ])
    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[1] is raw
    responses = [part.function_response for part in contents[2].parts]
    assert [(r.id, r.name) for r in responses] == [("a", "x"), ("b", "y")]
    assert responses[0].response == {"result": [{"when": "today"}]}


@pytest.mark.asyncio
async def test_client_retries_rate_limits_but_not_bad_requests() -> None:
    ok = _response([types.Part.from_text(text="done")])
    sdk = FakeSdk([errors.APIError(429, {}), errors.APIError(503, {}), ok])
    turn = await GeminiClient("k", IDS, sdk=sdk).generate(tier="agent", system="s", contents=[UserMessage("x")])
    assert turn.text == "done"
    assert len(sdk.calls) == 3

    sdk = FakeSdk([errors.APIError(400, {}), ok])
    with pytest.raises(errors.APIError):
        await GeminiClient("k", IDS, sdk=sdk).generate(tier="agent", system="s", contents=[UserMessage("x")])
    assert len(sdk.calls) == 1


class Pick(BaseModel):
    choice: str
    score: float


@pytest.mark.asyncio
async def test_structured_retries_once_then_raises() -> None:
    sdk = FakeSdk([_response([], text_json="not json"), _response([], text_json='{"choice": "a", "score": 0.5}')])
    result = await GeminiClient("k", IDS, sdk=sdk).generate_structured(tier="light", system="s", text="t", schema=Pick)
    assert result == Pick(choice="a", score=0.5)
    assert sdk.calls[0]["config"].response_json_schema == Pick.model_json_schema()

    sdk = FakeSdk([_response([], text_json="{}"), _response([], text_json="{}")])
    with pytest.raises(ValidationError):
        await GeminiClient("k", IDS, sdk=sdk).generate_structured(tier="light", system="s", text="t", schema=Pick)


@pytest.mark.asyncio
async def test_llm_03_parallel_tool_calls_all_run_and_return(repo: SqliteRepository) -> None:
    await repo.upsert_contact("T_TEST", "Alex", company="Acme")
    turns = [
        ModelTurn(tool_calls=[
            ToolCall("1", "query_relationship_graph", {"contact_name": "Alex"}),
            ToolCall("2", "get_meeting_context", {"contact_name": "Alex"}),
        ]),
        ModelTurn(text="Alex works at Acme."),
    ]
    model = FakeModel(list(turns))
    reply = await AgentLoop(ToolRegistry(repo, "T_TEST"), model).run(InboundMessage(text="who is Alex?", system="s"))
    assert reply.text == "Alex works at Acme."
    results = tool_results(model.requests[1].contents)
    assert [result.call.name for result in results] == ["query_relationship_graph", "get_meeting_context"]
    assert results[0].result[0]["name"] == "Alex"


@pytest.mark.asyncio
async def test_llm_04_invalid_args_do_not_run_the_tool(repo: SqliteRepository) -> None:
    ran = []
    registry = ToolRegistry(repo, "T_TEST")
    original = registry.call

    async def call(name, arguments):
        ran.append(name)
        return await original(name, arguments)

    registry.call = call
    model = FakeModel([
        ModelTurn(tool_calls=[
            ToolCall("1", "get_meeting_context", {"limit": 99}),
            ToolCall("2", "delete_everything", {}),
        ]),
        ModelTurn(text="I need a name."),
    ])
    reply = await AgentLoop(registry, model).run(InboundMessage(text="notes?", system="s"))
    assert ran == []
    errors_sent = [result.result["error"] for result in tool_results(model.requests[1].contents)]
    assert "contact_name" in errors_sent[0] and "limit" in errors_sent[0]
    assert "Unknown tool" in errors_sent[1]
    assert reply.text == "I need a name."


@pytest.mark.asyncio
async def test_llm_05_budget_exhausted_skips_the_model(repo: SqliteRepository) -> None:
    await repo.add_model_usage("T_TEST", "U1", input_tokens=1, output_tokens=1, cost_usd=1.5)
    client = FakeSlack()
    model = FakeModel()
    runtime = KnappyRuntime(repo, workspace_id="T_TEST", model=model, daily_budget_usd=1.0, say=build_say(client))
    reply = await runtime.handle_event(dm("hello", "1.0"))
    assert reply.text == BUDGET_TEXT
    assert model.requests == [] and model.structured_requests == []
    assert client.shown("100.1")["text"] == BUDGET_TEXT

    other = await runtime.handle_event(dm("hello", "2.0", user="U2", channel="D2"))
    assert other.text != BUDGET_TEXT


@pytest.mark.asyncio
async def test_usage_recorder_accumulates_per_owner(repo: SqliteRepository) -> None:
    record = usage_recorder(repo, "T_TEST")
    token = current_owner.set("U1")
    try:
        await record("agent", IDS.agent, client_module.Usage(10, 5, 0, 0.25))
        await record("light", IDS.light, client_module.Usage(10, 5, 0, 0.5))
    finally:
        current_owner.reset(token)
    assert await repo.spend_today("T_TEST", "U1") == pytest.approx(0.75)
    assert await repo.spend_today("T_TEST", "U2") == 0.0


@pytest.mark.asyncio
async def test_triage_sends_candidate_view_to_light_model() -> None:
    async def structured(schema, system, text):
        assert schema is TriageJudgment
        payload = json.loads(text)
        assert payload == {"kind": "COMMITMENT", "contact_name": "Alex", "hours_until_due": 2}
        return TriageJudgment(interrupt_probability=0.9, strategy="immediate_dm", strategy_confidence=0.8, consequence_score=2)

    classify = model_triage(FakeModel(structured=structured))
    decision = await classify({"kind": "COMMITMENT", "contact_name": "Alex", "hours_until_due": 2, "embedding": b"x", "owner_user_id": "U1"})
    assert decision["strategy"] == "immediate_dm"


@pytest.mark.live_model
@pytest.mark.asyncio
async def test_llm_06_live_gemini_tool_turn() -> None:
    import os

    from knappy.config import DEFAULT_MODEL_AGENT, DEFAULT_MODEL_LIGHT, load_dotenv

    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY not set")
    client = GeminiClient(key, ModelIds(agent=DEFAULT_MODEL_AGENT, light=DEFAULT_MODEL_LIGHT))
    turn = await client.generate(
        tier="agent",
        system="Use the search_commitments tool to answer questions about promises.",
        contents=[UserMessage("What did I promise Alex?")],
        tools=ToolRegistry(None, "T").specs(),  # type: ignore[arg-type]
    )
    assert [call.name for call in turn.tool_calls] == ["search_commitments"]
    assert turn.usage.input_tokens > 0 and turn.usage.cost_usd > 0
    pick = await client.generate_structured(tier="light", system="Pick a letter.", text="a or b?", schema=Pick)
    assert pick.choice
