"""Fast intent router. High-confidence simple lookups skip the ReAct loop."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from knappy.agent.react import AgentReply, ReActAgent
from knappy.agent.tools import (
    ToolRegistry,
    format_commitment_results,
    format_contact_results,
    format_history_results,
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
logger = logging.getLogger("knappy")


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
            logger.info("route intent=error path=react")
            return await self.react_agent.run(query, thread_context)

        simple = (
            intent.confidence >= self.INTENT_CONFIDENCE_THRESHOLD
            and intent.complexity < self.COMPLEXITY_CEILING
        )
        path = _route_path(intent, simple)
        logger.info("route intent=%s path=%s", intent.intent, path)
        if simple and intent.intent == "identity":
            return AgentReply(
                text=(
                    "I'm Knappy, your work assistant. I keep your notes and commitments, "
                    "answer questions about them, and remind you before something is due. "
                    "Save one with `note:`, or ask what you have to do."
                )
            )
        if simple and intent.intent == "list_commitments":
            results = await self.tools.search_commitments(query=query, match_text=False)
            if not results:
                return AgentReply(text="You don't have any open commitments.")
            return AgentReply(text=format_commitment_results(results, "your open commitments"))
        if simple and intent.intent == "search_commitments":
            results = await self.tools.search_commitments(query=query)
            return AgentReply(text=format_commitment_results(results, query))
        if simple and intent.intent == "search_history":
            results = await self.tools.search_slack_history(
                query=query,
                channel_id=thread_context.get("channel_id") or None,
            )
            return AgentReply(text=format_history_results(results, query))
        if simple and intent.intent == "query_contact":
            results = await self.tools.query_relationship_graph(contact_name=query)
            return AgentReply(text=format_contact_results(results, query))
        if simple and intent.intent == "meeting_context":
            results = await self.tools.get_meeting_context(contact_name=query)
            return AgentReply(text=format_meeting_results(results, query))
        if simple and intent.intent == "compose":
            return AgentReply(text=await _compose_reply(query, self.tools))
        if simple and intent.intent == "show_last":
            return AgentReply(text=_show_last(thread_context))
        if simple and intent.intent == "chitchat":
            if _is_general_question(query):
                return AgentReply(
                    text="I can't answer general questions. I can tell you what you promised, draft a note, or remind you when it's due."
                )
            return AgentReply(
                text="Hey! How can I help you manage your contacts, commitments, or schedule today?"
            )
        return await self.react_agent.run(query, thread_context)


async def _compose_reply(query: str, tools: ToolRegistry) -> str:
    results = await tools.search_commitments(query=query)
    name = _person_name(query) or (results[0].get("contact_name") if results else "") or "them"
    commitment = (results[0].get("commitment") if results else "") or ""
    if "budget" in query.lower():
        tied = f"\nTied to your note: {commitment}." if commitment else ""
        return (
            f"Draft budget for {name}\n\n"
            "| Item | Amount | Notes |\n"
            "| --- | --- | --- |\n"
            "| Labor |  |  |\n"
            "| Materials |  |  |\n"
            "| Travel |  |  |\n"
            "| Contingency |  |  |\n"
            f"| Total |  |  |{tied}"
        )
    return f"Draft for {name}: {query.strip()}"


def _show_last(thread_context: dict[str, Any]) -> str:
    history = thread_context.get("recent_messages") or []
    for item in reversed(history):
        text = item.get("text") or ""
        if item.get("role") == "assistant" and text and "How can I help" not in text:
            return text
    return "I don't have a draft to show yet. Ask me to draft one."


def _person_name(query: str) -> str:
    match = re.search(r"\bfor ([A-Z][a-zA-Z]+)", query)
    return match.group(1) if match else ""


def _is_general_question(query: str) -> bool:
    text = query.strip().lower()
    return text.endswith("?") or text.startswith(("how many", "how much", "what is", "what's"))


def _route_path(intent: IntentClassification, simple: bool) -> str:
    if not simple or intent.is_outbound >= 0.75:
        return "react"
    return intent.intent
