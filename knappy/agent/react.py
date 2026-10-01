"""Bounded ReAct loop. Mutating tools only stage drafts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from knappy.agent.memory import ThreadMemory
from knappy.agent.tools import ToolRegistry, format_commitment_results

CompleteFn = Callable[[list[dict[str, Any]]], Awaitable["ModelTurn"]]


@dataclass
class ModelTurn:
    text: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentReply:
    text: str
    blocks: list[dict[str, Any]] | None = None
    draft_id: str | None = None


class ReActAgent:
    MAX_ITERATIONS = 3

    def __init__(self, tools: ToolRegistry, complete: CompleteFn, memory: ThreadMemory) -> None:
        self.tools = tools
        self.complete = complete
        self.memory = memory

    def build_messages(self, query: str, thread_context: dict[str, Any]) -> list[dict[str, Any]]:
        thread_ts = thread_context.get("thread_ts") or ""
        history = self.memory.prompt_block(thread_ts)
        return [
            {
                "role": "system",
                "content": "You are Knappy, a relationship assistant. Use tools. Never send messages yourself.",
            },
            {"role": "user", "content": f"Thread {thread_ts}\n{history}\nUser: {query}"},
        ]

    async def run(self, query: str, thread_context: dict[str, Any]) -> AgentReply:
        messages = self.build_messages(query, thread_context)
        for _ in range(self.MAX_ITERATIONS):
            turn = await self.complete(messages)
            if not turn.tool_name:
                return AgentReply(text=turn.text or "")
            arguments = dict(turn.tool_args)
            if turn.tool_name == "stage_outbound_action":
                arguments.setdefault("user_id", thread_context.get("user_id", ""))
                arguments.setdefault("channel_id", thread_context.get("channel_id", ""))
                arguments.setdefault("thread_ts", thread_context.get("thread_ts"))
            result = await self.tools.call(turn.tool_name, arguments)
            if turn.tool_name == "stage_outbound_action":
                return AgentReply(
                    text=f"Staged a {arguments.get('action_type', 'message')} for {arguments.get('recipient', 'the recipient')}.",
                    blocks=result["blocks"],
                    draft_id=result["draft_id"],
                )
            if turn.tool_name == "search_commitments" and result == []:
                return AgentReply(text=format_commitment_results([], query))
            messages.append({"role": "tool", "name": turn.tool_name, "content": json.dumps(result, default=str)})
        return AgentReply(text="I need a bit more detail before I can answer. Could you clarify what you want?")
