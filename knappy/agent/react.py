"""Bounded ReAct loop. Mutating tools only stage drafts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from knappy.agent.memory import ThreadMemory
from knappy.agent.tools import TOOL_SPECS, ToolRegistry, format_commitment_results, format_history_results
from knappy.llm.types import Message, Model, ToolCall, ToolResult, UserMessage

logger = logging.getLogger("knappy")

SYSTEM_PROMPT = "You are Knappy, a relationship assistant. Use tools. Never send messages yourself."


@dataclass
class AgentReply:
    text: str
    blocks: list[dict[str, Any]] | None = None
    draft_id: str | None = None


class ReActAgent:
    MAX_ITERATIONS = 3

    def __init__(self, tools: ToolRegistry, model: Model, memory: ThreadMemory) -> None:
        self.tools = tools
        self.model = model
        self.memory = memory

    def build_messages(self, query: str, thread_context: dict[str, Any]) -> list[Message]:
        thread_ts = thread_context.get("thread_ts") or ""
        history = self.memory.prompt_block(thread_ts)
        return [UserMessage(f"Thread {thread_ts}\n{history}\nUser: {query}")]

    async def run(self, query: str, thread_context: dict[str, Any]) -> AgentReply:
        contents = self.build_messages(query, thread_context)
        for _ in range(self.MAX_ITERATIONS):
            turn = await self.model.generate(
                tier="agent", system=SYSTEM_PROMPT, contents=contents, tools=self.tools.specs()
            )
            if not turn.tool_calls:
                return AgentReply(text=turn.text or "")
            contents.append(turn)
            for call in turn.tool_calls:
                logger.info("tool %s", call.name)
                arguments = _validated(call)
                if isinstance(arguments, str):
                    contents.append(ToolResult(call, {"error": arguments}))
                    continue
                if call.name == "stage_outbound_action":
                    arguments.update(
                        user_id=thread_context.get("user_id", ""),
                        channel_id=thread_context.get("channel_id", ""),
                        thread_ts=thread_context.get("thread_ts"),
                    )
                result = await self.tools.call(call.name, arguments)
                if call.name == "stage_outbound_action":
                    return AgentReply(
                        text=f"Staged a {arguments['action_type']} for {arguments['recipient']}.",
                        blocks=result["blocks"],
                        draft_id=result["draft_id"],
                    )
                if call.name == "search_commitments" and result == []:
                    return AgentReply(text=format_commitment_results([], query))
                if call.name == "search_slack_history":
                    return AgentReply(text=format_history_results(result, query))
                contents.append(ToolResult(call, result))
        return AgentReply(text="I need a bit more detail before I can answer. Could you clarify what you want?")


def _validated(call: ToolCall) -> dict[str, Any] | str:
    spec = TOOL_SPECS.get(call.name)
    if spec is None:
        return f"Unknown tool {call.name}"
    try:
        return spec.args_model.model_validate(call.args).model_dump()
    except ValidationError as exc:
        return f"Invalid arguments for {call.name}: {exc.errors(include_url=False)}"
