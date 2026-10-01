"""Fast intent router. High-confidence simple lookups skip the ReAct loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from knappy.agent.react import AgentReply, ReActAgent
from knappy.agent.tools import (
    ToolRegistry,
    format_commitment_results,
    format_contact_results,
    format_meeting_results,
)


@dataclass
class IntentClassification:
    intent: str
    confidence: float
    complexity: float
    is_outbound: float


class IntentClassifier(Protocol):
    async def classify(self, query: str, thread_context: dict[str, Any]) -> IntentClassification: ...


ClassifyFn = Callable[[str, dict[str, Any]], Awaitable[IntentClassification]]


class SystemOneRouter:
    INTENT_CONFIDENCE_THRESHOLD = 0.90
    COMPLEXITY_CEILING = 1.0

    def __init__(self, tools: ToolRegistry, react_agent: ReActAgent, classify: ClassifyFn) -> None:
        self.tools = tools
        self.react_agent = react_agent
        self.classify = classify

    async def route_and_execute(self, query: str, thread_context: dict[str, Any]) -> AgentReply:
        try:
            intent = await self.classify(query, thread_context)
        except Exception:
            return await self.react_agent.run(query, thread_context)

        simple = (
            intent.confidence >= self.INTENT_CONFIDENCE_THRESHOLD
            and intent.complexity < self.COMPLEXITY_CEILING
        )
        if simple and intent.intent == "search_commitments":
            results = await self.tools.search_commitments(query=query)
            return AgentReply(text=format_commitment_results(results, query))
        if simple and intent.intent == "query_contact":
            results = await self.tools.query_relationship_graph(contact_name=query)
            return AgentReply(text=format_contact_results(results, query))
        if simple and intent.intent == "meeting_context":
            results = await self.tools.get_meeting_context(contact_name=query)
            return AgentReply(text=format_meeting_results(results, query))
        if simple and intent.intent == "chitchat":
            return AgentReply(
                text="Hey! How can I help you manage your contacts, commitments, or schedule today?"
            )
        if intent.is_outbound >= 0.75 or not simple:
            return await self.react_agent.run(query, thread_context)
        return await self.react_agent.run(query, thread_context)
