"""Wires ingestion, the agent, approval, and proactive delivery."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from knappy.agent.memory import ThreadMemory
from knappy.agent.react import AgentReply, ModelTurn, ReActAgent
from knappy.agent.router import IntentClassification, SystemOneRouter
from knappy.agent.tools import ToolRegistry
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.hitl.gateway import ActionExecutor, ApprovalGateway
from knappy.ingestion.extract import SlmExtractor
from knappy.ingestion.filter import LocalStructuralFilter
from knappy.ingestion.gate import CompositeSystemOneGate, JevSystemOneAdapter, RegexFallbackAdapter
from knappy.ingestion.pipeline import IngestionPipeline, acknowledgement

Say = Callable[..., Awaitable[None]]


async def heuristic_intent(query: str, thread_context: dict[str, Any]) -> IntentClassification:
    text = query.lower()
    if any(phrase in text for phrase in ("promise", "promised", "commitment", "what did i")):
        return IntentClassification("search_commitments", 0.95, 0.2, 0.05)
    if re.search(r"\bfollow up\b|\bemail\b|\binvite\b|\bdraft\b", text):
        return IntentClassification("stage_action", 0.95, 1.5, 0.92)
    if text.startswith("who") or "contact" in text:
        return IntentClassification("query_contact", 0.95, 0.2, 0.0)
    if "meeting" in text or "last note" in text:
        return IntentClassification("meeting_context", 0.95, 0.3, 0.0)
    return IntentClassification("chitchat", 0.95, 0.1, 0.0)


async def heuristic_complete(messages: list[dict[str, Any]]) -> ModelTurn:
    if any(item.get("role") == "tool" for item in messages):
        return ModelTurn(text="Done.")
    user = next(item["content"] for item in messages if item["role"] == "user")
    query = user.split("User:", 1)[-1].strip()
    lower = query.lower()
    if "follow up" in lower:
        recipient = "them"
        for token in query.split():
            if token[:1].isupper() and token.lower() not in {"follow", "up", "with"}:
                recipient = token.strip(".,!?")
                break
        return ModelTurn(
            tool_name="stage_outbound_action",
            tool_args={
                "action_type": "SEND_SLACK_DM",
                "recipient": recipient,
                "summary": f"Follow up with {recipient}",
                "payload": {
                    "staged_content": f"Hi {recipient}, following up as we discussed.",
                    "recipient_identifier": recipient,
                },
            },
        )
    return ModelTurn(tool_name="search_commitments", tool_args={"query": user})


async def heuristic_triage(candidate: dict[str, Any]) -> dict[str, float | str]:
    hours = candidate.get("hours_until_due")
    if isinstance(hours, (int, float)) and hours <= 4:
        return {
            "interrupt_probability": 0.9,
            "strategy": "immediate_dm",
            "strategy_confidence": 0.9,
            "consequence_score": 2.0,
        }
    if isinstance(hours, (int, float)):
        return {
            "interrupt_probability": 0.55,
            "strategy": "batch_into_morning_digest",
            "strategy_confidence": 0.8,
            "consequence_score": 1.0,
        }
    return {
        "interrupt_probability": 0.2,
        "strategy": "suppress_low_value",
        "strategy_confidence": 0.8,
        "consequence_score": 0.2,
    }


class KnappyRuntime:
    def __init__(
        self,
        repo: SqliteRepository,
        *,
        workspace_id: str,
        classify=heuristic_intent,
        complete=heuristic_complete,
        triage_classify=heuristic_triage,
        executor: ActionExecutor | None = None,
        say: Say | None = None,
        sender: Say | None = None,
    ) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.say = say
        self.memory = ThreadMemory()
        self.tools = ToolRegistry(repo, workspace_id)
        self.agent = ReActAgent(self.tools, complete, self.memory)
        self.router = SystemOneRouter(self.tools, self.agent, classify)
        self.gate = CompositeSystemOneGate(JevSystemOneAdapter(), RegexFallbackAdapter())
        self.pipeline = IngestionPipeline(repo, self.gate, SlmExtractor(), workspace_id)
        self.gateway = ApprovalGateway(repo, executor or _RefusingExecutor())
        self.heartbeat = HeartbeatEngine(
            repo,
            ProactiveAlertTriager(triage_classify),
            workspace_id=workspace_id,
            user_id="user",
            sender=sender,
        )

    def bind_user(self, user_id: str) -> None:
        self.heartbeat.user_id = user_id

    async def handle_event(self, event: dict[str, Any]) -> AgentReply | None:
        text = event.get("text", "")
        thread_ts = str(event.get("thread_ts") or event.get("ts") or event.get("channel") or "dm")
        self.memory.append(thread_ts, "user", text)
        if text.strip().lower().startswith("note:"):
            extracted = await self.pipeline.run({**event, "workspace_id": self.workspace_id})
            if extracted is None:
                return None
            reply = AgentReply(text=acknowledgement(extracted))
            self.memory.append(thread_ts, "assistant", reply.text)
            if self.say is not None:
                await self.say(text=reply.text, channel=event.get("channel"), thread_ts=thread_ts)
            return reply
        if LocalStructuralFilter.should_evaluate(event) and not _is_user_query(text):
            passes, _decision = await self.gate.should_ingest(event)
            if passes:
                await self.pipeline.commit({**event, "workspace_id": self.workspace_id})
        context = {
            "thread_ts": thread_ts,
            "user_id": event.get("user") or self.heartbeat.user_id,
            "channel_id": event.get("channel") or "",
            "recent_messages": self.memory.history(thread_ts)[-2:],
        }
        reply = await self.router.route_and_execute(text, context)
        self.memory.append(thread_ts, "assistant", reply.text)
        if self.say is not None:
            await self.say(
                text=reply.text,
                blocks=reply.blocks,
                channel=event.get("channel"),
                thread_ts=thread_ts,
            )
        return reply


def _is_user_query(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped.endswith("?") or stripped.startswith(("what ", "who ", "when ", "where ", "why ", "how ", "follow up"))


class _RefusingExecutor:
    async def execute(self, draft: dict[str, Any]) -> None:
        raise RuntimeError("No executor configured")
