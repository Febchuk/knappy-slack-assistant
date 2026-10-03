"""Wires ingestion, the agent, approval, and proactive delivery."""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from knappy.agent.memory import ThreadMemory
from knappy.agent.react import AgentReply, ReActAgent
from knappy.agent.router import IntentClassification, SystemOneRouter
from knappy.agent.tools import ToolRegistry, current_owner
from knappy.slack.egress import delivery_kwargs
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager, model_triage
from knappy.hitl.gateway import ActionExecutor, ApprovalGateway
from knappy.ingestion.extract import SlmExtractor
from knappy.ingestion.filter import LocalStructuralFilter
from knappy.ingestion.gate import CompositeSystemOneGate, JevSystemOneAdapter, RegexFallbackAdapter
from knappy.ingestion.pipeline import IngestionPipeline, acknowledgement
from knappy.llm.client import OnUsage
from knappy.llm.types import Model, Tier, Usage

logger = logging.getLogger("knappy")
Say = Callable[..., Awaitable[None]]

BUDGET_TEXT = "I've hit today's usage limit, so I can't think this through right now. I'll be back tomorrow."

_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>")


def strip_mentions(text: str) -> str:
    return re.sub(r"\s+", " ", _MENTION.sub(" ", text)).strip()


async def heuristic_intent(query: str, thread_context: dict[str, Any]) -> IntentClassification:
    text = strip_mentions(query).lower()
    if re.search(r"\bwho are you\b|\bwhat are you\b|\bwhat can you do\b", text):
        return IntentClassification("identity", 0.95, 0.1, 0.0)
    if re.search(
        r"\bwhat do i have\b|\bwhat should i\b|\bwhat do i need\b|\bon my plate\b|\bmy tasks\b|\bto-?do\b",
        text,
    ):
        return IntentClassification("list_commitments", 0.95, 0.2, 0.0)
    if any(phrase in text for phrase in ("promise", "promised", "commitment", "what did i")):
        return IntentClassification("search_commitments", 0.95, 0.2, 0.05)
    if any(phrase in text for phrase in ("said", "say about", "in slack", "in the channel", "slack message")):
        return IntentClassification("search_history", 0.95, 0.2, 0.0)
    if re.search(r"\b(show me|show that|show it|show the draft)\b", text):
        return IntentClassification("show_last", 0.95, 0.1, 0.0)
    if re.search(r"\b(draft|template|write me|write a)\b", text) and not re.search(
        r"\b(send|email|follow up|invite)\b", text
    ):
        return IntentClassification("compose", 0.95, 0.3, 0.0)
    if re.search(r"\bfollow up\b|\bemail\b|\binvite\b", text):
        return IntentClassification("stage_action", 0.95, 1.5, 0.92)
    if text.startswith("who") or "contact" in text:
        return IntentClassification("query_contact", 0.95, 0.2, 0.0)
    if "meeting" in text or "last note" in text:
        return IntentClassification("meeting_context", 0.95, 0.3, 0.0)
    return IntentClassification("chitchat", 0.95, 0.1, 0.0)


class KnappyRuntime:
    def __init__(
        self,
        repo: SqliteRepository,
        *,
        workspace_id: str,
        model: Model,
        classify=heuristic_intent,
        daily_budget_usd: float | None = None,
        executor: ActionExecutor | None = None,
        say: Say | None = None,
        sender: Say | None = None,
        history: Any | None = None,
    ) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.say = say
        self.daily_budget_usd = daily_budget_usd
        self.memory = ThreadMemory()
        self.tools = ToolRegistry(repo, workspace_id, history=history)
        self.agent = ReActAgent(self.tools, model, self.memory)
        self.router = SystemOneRouter(self.tools, self.agent, classify)
        self.gate = CompositeSystemOneGate(JevSystemOneAdapter(), RegexFallbackAdapter())
        self.pipeline = IngestionPipeline(repo, self.gate, SlmExtractor(model), workspace_id)
        self.gateway = ApprovalGateway(repo, executor or _RefusingExecutor())
        self.heartbeat = HeartbeatEngine(
            repo,
            ProactiveAlertTriager(model_triage(model)),
            workspace_id=workspace_id,
            user_id="user",
            sender=sender,
        )

    def bind_user(self, user_id: str) -> None:
        self.heartbeat.user_id = user_id

    async def handle_event(self, event: dict[str, Any]) -> AgentReply | None:
        owner = str(event.get("user") or "")
        token = current_owner.set(owner)
        try:
            return await self._handle_event(event)
        finally:
            current_owner.reset(token)

    async def _handle_event(self, event: dict[str, Any]) -> AgentReply | None:
        text = strip_mentions(event.get("text", ""))
        event = {**event, "text": text}
        if await self._over_budget(str(event.get("user") or "")):
            reply = AgentReply(text=BUDGET_TEXT)
            await self._post(event, reply)
            return reply
        thread_ts = _conversation_key(event)
        self.memory.append(thread_ts, "user", text)
        if text.strip().lower().startswith("note:"):
            extracted = await self.pipeline.run({**event, "workspace_id": self.workspace_id})
            if extracted is None:
                return None
            reply = AgentReply(text=acknowledgement(extracted))
            self.memory.append(thread_ts, "assistant", reply.text)
            await self._post(event, reply)
            return reply
        if LocalStructuralFilter.should_evaluate(event) and not _is_user_query(text):
            passes, _decision = await self.gate.should_ingest(event)
            if passes:
                await self.pipeline.commit({**event, "workspace_id": self.workspace_id})
        context = {
            "thread_ts": thread_ts,
            "user_id": event.get("user") or self.heartbeat.user_id,
            "channel_id": event.get("channel") or "",
            "recent_messages": self.memory.history(thread_ts)[-6:],
        }
        reply = await self.router.route_and_execute(text, context)
        self.memory.append(thread_ts, "assistant", reply.text)
        await self._post(event, reply)
        return reply

    async def _over_budget(self, owner: str) -> bool:
        if self.daily_budget_usd is None:
            return False
        return await self.repo.spend_today(self.workspace_id, owner) >= self.daily_budget_usd

    async def _post(self, event: dict[str, Any], reply: AgentReply) -> None:
        if self.say is None:
            return
        await self.say(**delivery_kwargs(event, reply.text, reply.blocks))


def _conversation_key(event: dict[str, Any]) -> str:
    if event.get("thread_ts"):
        return str(event["thread_ts"])
    if event.get("channel"):
        return str(event["channel"])
    return str(event.get("ts") or "dm")


def _is_user_query(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.endswith("?") or stripped.startswith(("what ", "who ", "when ", "where ", "why ", "how ", "follow up"))


class _RefusingExecutor:
    async def execute(self, draft: dict[str, Any]) -> None:
        raise RuntimeError("No executor configured")


def usage_recorder(repo: SqliteRepository, workspace_id: str) -> OnUsage:
    async def record(tier: Tier, model: str, usage: Usage) -> None:
        owner = current_owner.get() or ""
        logger.info("usage owner=%s tier=%s model=%s cost=%.6f", owner, tier, model, usage.cost_usd)
        await repo.add_model_usage(
            workspace_id,
            owner,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
        )

    return record
