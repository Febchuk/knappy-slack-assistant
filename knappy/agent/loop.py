"""Model-driven tool loop (Spec 12 §3). The model writes every answer; tools never end the loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from knappy.agent.prompt import FINAL_TURN_NOTE
from knappy.agent.tools import TOOL_SPECS, TOOL_STATUS, StagedDraft, ToolRegistry
from knappy.llm.types import Message, Model, ToolCall, ToolResult, UserMessage
from knappy.web import sources_of, with_citations

logger = logging.getLogger("knappy")

StatusFn = Callable[[str], Awaitable[None]]
EMPTY_ANSWER = "I couldn't put an answer together for that. Could you try asking again?"
# Gemini retries an empty search with reworded queries until the step limit. A tool that came back empty this many
# times is withdrawn for the rest of the message.
EMPTY_LIMIT = 2
WITHDRAWN = "This tool found nothing twice already and is withdrawn for this message. Answer with what you have."
NOTHING_FOUND = (
    "Nothing found. This source has nothing on it; rewording the query will not change that. "
    "Don't search it again for this message. Answer with what you have, or ask the user."
)


@dataclass(frozen=True)
class InboundMessage:
    text: str
    system: str
    history: list[Message] = field(default_factory=list)


@dataclass
class AgentReply:
    text: str
    blocks: list[dict[str, Any]] | None = None
    draft_id: str | None = None
    tool_results: list[ToolResult] = field(default_factory=list)


class AgentLoop:
    MAX_STEPS = 8
    WALL_CLOCK_S = 60.0

    def __init__(
        self,
        tools: ToolRegistry,
        model: Model,
        *,
        max_steps: int = MAX_STEPS,
        wall_clock_s: float = WALL_CLOCK_S,
    ) -> None:
        self.tools = tools
        self.model = model
        self.max_steps = max_steps
        self.wall_clock_s = wall_clock_s

    async def run(self, message: InboundMessage, on_status: StatusFn | None = None) -> AgentReply:
        started = time.monotonic()
        deadline = started + self.wall_clock_s
        contents: list[Message] = [*message.history, UserMessage(message.text)]
        drafts: list[StagedDraft] = []
        ran: list[ToolResult] = []
        specs = self.tools.specs()
        empty: Counter[str] = Counter()
        withdrawn: set[str] = set()
        steps = 0
        for steps in range(1, self.max_steps + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                turn = await asyncio.wait_for(
                    self.model.generate(tier="agent", system=message.system, contents=contents, tools=specs),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            if not turn.tool_calls:
                logger.info("agent steps=%d stop=answer ms=%d", steps, _ms(started))
                return _reply(turn.text, drafts, ran)
            contents.append(turn)
            if on_status is not None:
                await on_status(_status(turn.tool_calls))
            results = await self._run_tools(turn.tool_calls, deadline, withdrawn)
            drafts.extend(result.result for result in results if isinstance(result.result, StagedDraft))
            ran.extend(_for_model(result) for result in results)
            contents.extend(_for_model(result) for result in results)
            empty.update(result.call.name for result in results if result.result == [])
            withdrawn = {name for name, count in empty.items() if count >= EMPTY_LIMIT}
            specs = [spec for spec in specs if spec.name not in withdrawn]
        # Gemini keeps calling tools after a tool result even when none are declared; a closing user turn gets text.
        final = await self.model.generate(
            tier="agent",
            system=f"{message.system}\n\n{FINAL_TURN_NOTE}",
            contents=[*contents, UserMessage(FINAL_TURN_NOTE)],
            tools=None,
        )
        logger.info("agent steps=%d stop=limit ms=%d", steps, _ms(started))
        return _reply(final.text, drafts, ran)

    async def _run_tools(self, calls: list[ToolCall], deadline: float, withdrawn: set[str]) -> list[ToolResult]:
        tasks = [asyncio.ensure_future(self._run_tool(call, call.name in withdrawn)) for call in calls]
        _done, pending = await asyncio.wait(tasks, timeout=max(deadline - time.monotonic(), 0))
        for task in pending:
            task.cancel()
        return [
            task.result() if task not in pending else ToolResult(call, {"error": "Timed out before this tool finished."})
            for call, task in zip(calls, tasks)
        ]

    async def _run_tool(self, call: ToolCall, withdrawn: bool) -> ToolResult:
        started = time.monotonic()
        if withdrawn:
            logger.info("tool name=%s outcome=withdrawn", call.name)
            return ToolResult(call, {"error": WITHDRAWN})
        arguments = _validated(call)
        if isinstance(arguments, str):
            logger.info("tool name=%s outcome=invalid", call.name)
            return ToolResult(call, {"error": arguments})
        try:
            result = await self.tools.call(call.name, arguments)
        except Exception as exc:
            logger.warning("tool name=%s outcome=error ms=%d error=%s", call.name, _ms(started), type(exc).__name__)
            return ToolResult(call, {"error": f"{type(exc).__name__}: {exc}"})
        logger.info("tool name=%s outcome=ok ms=%d", call.name, _ms(started))
        return ToolResult(call, result)


def _validated(call: ToolCall) -> dict[str, Any] | str:
    spec = TOOL_SPECS.get(call.name)
    if spec is None:
        return f"Unknown tool {call.name}"
    try:
        return spec.args_model.model_validate(call.args).model_dump()
    except ValidationError as exc:
        return f"Invalid arguments for {call.name}: {exc.errors(include_url=False)}"


def _for_model(result: ToolResult) -> ToolResult:
    if isinstance(result.result, StagedDraft):
        return ToolResult(result.call, result.result.for_model())
    if result.result == []:
        return ToolResult(result.call, {"matches": [], "note": NOTHING_FOUND})
    return result


def _status(calls: list[ToolCall]) -> str:
    labels = dict.fromkeys(TOOL_STATUS.get(call.name, "working") for call in calls)
    return ", ".join(labels)


def _reply(text: str | None, drafts: list[StagedDraft], ran: list[ToolResult]) -> AgentReply:
    sources = [source for result in ran for source in sources_of(result.call.name, result.result)]
    answer = with_citations((text or "").strip() or EMPTY_ANSWER, sources)
    if not drafts:
        return AgentReply(text=answer, tool_results=ran)
    blocks = [block for draft in drafts for block in draft.blocks]
    return AgentReply(text=answer, blocks=blocks, draft_id=drafts[-1].draft_id, tool_results=ran)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
