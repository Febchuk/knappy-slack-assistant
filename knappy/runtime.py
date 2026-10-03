"""Wires ingestion, the agent loop, approval, and proactive delivery."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from knappy.agent.loop import AgentLoop, AgentReply, InboundMessage
from knappy.agent.prompt import OPEN_LOOP_LIMIT, MemoryProvider, NoMemory, build_system_prompt
from knappy.agent.session import (
    ConversationLocks,
    ConversationLog,
    InMemoryConversationLog,
    Turn,
    as_messages,
    conversation_key,
)
from knappy.agent.tools import SlackThread, ToolRegistry, current_owner, current_thread
from knappy.db.repository import SqliteRepository
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager, model_triage
from knappy.hitl.gateway import ActionExecutor, ApprovalGateway
from knappy.ingestion.extract import SlmExtractor
from knappy.ingestion.gate import CompositeSystemOneGate, JevSystemOneAdapter, RegexFallbackAdapter
from knappy.ingestion.pipeline import IngestionPipeline, acknowledgement
from knappy.llm.client import OnUsage
from knappy.llm.types import Model, Tier, Usage
from knappy.slack.egress import Reply, SlackEgress, open_reply
from knappy.slack.users import UserDirectory

logger = logging.getLogger("knappy")

BUDGET_TEXT = "I've hit today's usage limit, so I can't think this through right now. I'll be back tomorrow."

_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>")


def strip_mentions(text: str) -> str:
    return re.sub(r"\s+", " ", _MENTION.sub(" ", text)).strip()


def failure_text(ref: str) -> str:
    return f"Sorry, something went wrong on my side and I couldn't finish that. Reference: `{ref}`"


class KnappyRuntime:
    def __init__(
        self,
        repo: SqliteRepository,
        *,
        workspace_id: str,
        model: Model,
        daily_budget_usd: float | None = None,
        executor: ActionExecutor | None = None,
        say: SlackEgress | None = None,
        sender: SlackEgress | None = None,
        slack: Any | None = None,
        memory: MemoryProvider | None = None,
        conversations: ConversationLog | None = None,
    ) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.say = say
        self.daily_budget_usd = daily_budget_usd
        self.memory = memory or NoMemory()
        self.conversations = conversations or InMemoryConversationLog()
        self.locks = ConversationLocks()
        self.users = UserDirectory(slack)
        self.tools = ToolRegistry(repo, workspace_id, history=slack)
        self.loop = AgentLoop(self.tools, model)
        gate = CompositeSystemOneGate(JevSystemOneAdapter(), RegexFallbackAdapter())
        self.pipeline = IngestionPipeline(repo, gate, SlmExtractor(model), workspace_id)
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

    async def handle_event(self, event: dict[str, Any]) -> AgentReply:
        """Answer one Slack message. Never raises: failures become an apology with a log reference."""
        event = {**event, "text": strip_mentions(str(event.get("text") or ""))}
        owner = str(event.get("user") or "")
        key = conversation_key(event)
        reply = open_reply(self.say, event)
        owner_token = current_owner.set(owner)
        thread_token = current_thread.set(SlackThread(str(event.get("channel") or ""), event.get("thread_ts")))
        # The placeholder goes up now; the lock is queued before any await so arrival order holds.
        acknowledged = asyncio.ensure_future(reply.start())
        try:
            async with self.locks.hold(key):
                await acknowledged
                answer = await self._answer(event, owner, key, reply)
                await reply.finish(answer.text, answer.blocks)
                return answer
        except Exception:
            ref = uuid.uuid4().hex[:8]
            logger.exception("handle_event failed ref=%s channel=%s", ref, event.get("channel"))
            apology = AgentReply(text=failure_text(ref))
            try:
                await reply.finish(apology.text)
            except Exception:
                logger.exception("failure reply failed ref=%s", ref)
            return apology
        finally:
            current_thread.reset(thread_token)
            current_owner.reset(owner_token)

    async def _answer(self, event: dict[str, Any], owner: str, key: str, reply: Reply) -> AgentReply:
        text = event["text"]
        if await self._over_budget(owner):
            return AgentReply(text=BUDGET_TEXT)
        if text.lower().startswith("note:"):
            extracted = await self.pipeline.commit({**event, "workspace_id": self.workspace_id})
            answer = AgentReply(text=acknowledgement(extracted))
        else:
            history, system = await asyncio.gather(
                self.conversations.window(owner, key),
                self._system_prompt(owner, key),
            )
            answer = await self.loop.run(
                InboundMessage(text=text, system=system, history=as_messages(history)),
                on_status=reply.status,
            )
        await self.conversations.append(owner, key, Turn("user", text))
        await self.conversations.append(owner, key, Turn("assistant", answer.text))
        return answer

    async def _system_prompt(self, owner: str, key: str) -> str:
        zone, memory, open_loops = await asyncio.gather(
            self.users.timezone(owner),
            self.memory.load(owner, key),
            self.repo.search_commitments(
                self.workspace_id, query="", owner_user_id=owner, match_text=False, limit=OPEN_LOOP_LIMIT
            ),
        )
        return build_system_prompt(
            now=datetime.now(timezone.utc), timezone=zone, memory=memory, open_loops=open_loops
        )

    async def _over_budget(self, owner: str) -> bool:
        if self.daily_budget_usd is None:
            return False
        return await self.repo.spend_today(self.workspace_id, owner) >= self.daily_budget_usd


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
