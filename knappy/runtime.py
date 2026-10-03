"""Wires ingestion, the agent loop, approval, and proactive delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from knappy.agent.loop import AgentLoop, AgentReply, InboundMessage
from knappy.agent.prompt import OPEN_LOOP_LIMIT, MemoryProvider, build_system_prompt
from knappy.agent.session import ConversationLocks, ConversationLog, Turn, as_messages, conversation_key
from knappy.agent.tools import SlackThread, ToolRegistry, current_owner, current_thread, current_turn
from knappy.db.repository import SqliteRepository, utc_now
from knappy.heartbeat.engine import HeartbeatEngine
from knappy.heartbeat.triage import ProactiveAlertTriager, model_triage
from knappy.hitl.gateway import ActionExecutor, ApprovalGateway
from knappy.ingestion.extract import SlmExtractor
from knappy.ingestion.gate import CompositeSystemOneGate, JevSystemOneAdapter, RegexFallbackAdapter
from knappy.ingestion.pipeline import IngestionPipeline, acknowledgement
from knappy.llm.client import OnUsage
from knappy.llm.types import Model, Tier, ToolResult, Usage
from knappy.memory import MemoryConfig, MemoryEngine, MemoryStore
from knappy.slack.egress import Reply, SlackEgress, open_reply
from knappy.slack.users import UserDirectory

logger = logging.getLogger("knappy")

BUDGET_TEXT = "I've hit today's usage limit, so I can't think this through right now. I'll be back tomorrow."

# An app_mention starts with the bot's own mention. Other mentions stay: they tell the model who someone is.
_ADDRESS = re.compile(r"^\s*<@[A-Z0-9]+(?:\|[^>]+)?>")


def strip_address(text: str) -> str:
    return re.sub(r"\s+", " ", _ADDRESS.sub(" ", text)).strip()


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
        memory_config: MemoryConfig | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.say = say
        self.daily_budget_usd = daily_budget_usd
        self.clock = clock
        self.store = MemoryStore(repo, workspace_id, clock)
        self.memory_engine = MemoryEngine(self.store, model, memory_config)
        self.memory: MemoryProvider = memory or self.memory_engine
        self.conversations: ConversationLog = self.store
        self.locks = ConversationLocks()
        self.users = UserDirectory(slack)
        self.tools = ToolRegistry(repo, workspace_id, history=slack, memory=self.memory_engine)
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
            clock=clock,
        )

    def bind_user(self, user_id: str) -> None:
        self.heartbeat.user_id = user_id

    async def handle_event(self, event: dict[str, Any]) -> AgentReply:
        """Answer one Slack message. Never raises: failures become an apology with a log reference."""
        event = {**event, "text": strip_address(str(event.get("text") or ""))}
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
        history = await self.conversations.window(owner, key)
        system = None if text.lower().startswith("note:") else await self._system_prompt(owner, key)
        turn_id = await self.conversations.append(owner, key, Turn("user", text), slack_ts=event.get("ts"))
        turn_token = current_turn.set(turn_id)
        try:
            if system is None:
                extracted = await self.pipeline.commit({**event, "workspace_id": self.workspace_id})
                answer = AgentReply(text=acknowledgement(extracted))
            else:
                answer = await self.loop.run(
                    InboundMessage(text=text, system=system, history=as_messages(history)),
                    on_status=reply.status,
                )
        finally:
            current_turn.reset(turn_token)
        for result in answer.tool_results:
            await self.conversations.append(owner, key, Turn("tool", _tool_turn(result, turn_id)))
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
        await self.store.set_timezone(owner, zone)
        return build_system_prompt(now=self.clock(), timezone=zone, memory=memory, open_loops=open_loops)

    async def _over_budget(self, owner: str) -> bool:
        if self.daily_budget_usd is None:
            return False
        return await self.repo.spend_today(self.workspace_id, owner) >= self.daily_budget_usd


def _tool_turn(result: ToolResult, turn_id: str) -> str:
    """What a tool turn stores: enough to replay remember and forget on rebuild (Spec 13 §3.5)."""
    output = json.dumps(result.result, default=str)
    return json.dumps(
        {"tool": result.call.name, "args": result.call.args, "turn": turn_id, "result": output[:2000]}, default=str
    )


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
