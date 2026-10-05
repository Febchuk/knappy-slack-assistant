"""Wires ingestion, the agent loop, approval, and proactive delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from knappy.agent.loop import AgentLoop, AgentReply, InboundMessage
from knappy.agent.prompt import ATTENTION_LIMIT, OPEN_LOOP_LIMIT, MemoryProvider, build_system_prompt
from knappy.agent.session import ConversationLocks, ConversationLog, Turn, as_messages, conversation_key
from knappy.agent.tools import SlackThread, ToolRegistry, current_owner, current_thread, current_turn
from knappy.awareness.ingest import Awareness, Pacing
from knappy.awareness.relevance import RelevancePass
from knappy.awareness.store import AwarenessStore
from knappy.db.repository import SqliteRepository, utc_now
from knappy.files.service import FileService, Shared, SlackDownloader, open_dm
from knappy.files.store import DocumentStore
from knappy.heartbeat.brief import ProactiveWriter
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
from knappy.slack.users import RecipientResolver, UserDirectory
from knappy.web import WebFetcher

if TYPE_CHECKING:
    from knappy.mcp.hub import McpHub

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
        fetcher: WebFetcher | None = None,
        downloader: SlackDownloader | None = None,
        user_client: Any | None = None,
        awareness_owner: str | None = None,
        bot_user_id: str | None = None,
        awareness_threshold: float = 0.5,
        awareness_pacing: Pacing | None = None,
        mcp: McpHub | None = None,
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
        self.recipients = RecipientResolver(repo, workspace_id, self.users)
        self.documents = DocumentStore(repo, workspace_id)
        self.files = FileService(self.documents, self.store, model, downloader, slack)
        self.attention = AwarenessStore(repo, workspace_id)
        self.user_client = user_client
        self.awareness: Awareness | None = None
        self.mcp = mcp
        if user_client is not None and awareness_owner:
            relevance = RelevancePass(
                model, self.memory_engine, self.attention, name=self.users.name, permalink=self._permalink,
                context=self._relevance_context, threshold=awareness_threshold,
            )
            self.awareness = Awareness(
                owner=awareness_owner, user_client=user_client, bot_client=slack, bot_user_id=bot_user_id,
                store=self.attention, relevance=relevance, memory=self.memory_engine, clock=clock,
                over_budget=self._over_budget, pacing=awareness_pacing,
            )
        self.tools = ToolRegistry(
            repo, workspace_id, history=slack, memory=self.memory_engine, searcher=model, fetcher=fetcher,
            files=self.files, recipients=self.recipients, attention=self.attention, awareness=self.awareness, clock=clock,
        )
        self.loop = AgentLoop(self.tools, model)
        gate = CompositeSystemOneGate(JevSystemOneAdapter(), RegexFallbackAdapter())
        self.pipeline = IngestionPipeline(repo, gate, SlmExtractor(model), workspace_id)
        self.gateway = ApprovalGateway(repo, executor or _RefusingExecutor())
        poster = sender or say
        self.heartbeat = HeartbeatEngine(
            self.store,
            ProactiveAlertTriager(model_triage(model)),
            writer=ProactiveWriter(model, self.store, over_budget=self._over_budget),
            recipients=self.recipients,
            timezone=self.users.timezone,
            channel=DirectMessages(slack, poster) if slack is not None and poster is not None else None,
            attention=self.attention,
        )

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
        files = event.get("files") or []
        shared = await self.files.receive(owner, key, files, reply.status) if files else Shared()
        history = await self.conversations.window(owner, key)
        system = None if text.lower().startswith("note:") else await self._system_prompt(owner, key)
        turn_id = await self.conversations.append(owner, key, Turn("user", shared.logged(text)), slack_ts=event.get("ts"))
        turn_token = current_turn.set(turn_id)
        try:
            if system is None:
                extracted = await self.pipeline.commit({**event, "workspace_id": self.workspace_id})
                answer = AgentReply(text=acknowledgement(extracted))
            else:
                answer = await self.loop.run(
                    InboundMessage(
                        text=shared.prompt(text), system=system, history=as_messages(history),
                        attachments=shared.attachments,
                    ),
                    on_status=reply.status,
                )
                if shared.notices:
                    answer.text = "\n".join([*shared.notices, "", answer.text])
        finally:
            current_turn.reset(turn_token)
        for result in answer.tool_results:
            await self.conversations.append(owner, key, Turn("tool", _tool_turn(result, turn_id)))
        await self.conversations.append(owner, key, Turn("assistant", answer.text))
        return answer

    async def _system_prompt(self, owner: str, key: str) -> str:
        zone, memory, open_loops, attention = await asyncio.gather(
            self.users.timezone(owner),
            self.memory.load(owner, key),
            self.repo.search_commitments(
                self.workspace_id, query="", owner_user_id=owner, match_text=False, limit=OPEN_LOOP_LIMIT
            ),
            self.attention.items(owner, self.clock(), limit=ATTENTION_LIMIT),
        )
        await self.store.set_timezone(owner, zone)
        return build_system_prompt(
            now=self.clock(), timezone=zone, memory=memory, open_loops=open_loops,
            attention=[item.for_model() for item in attention], awareness=self.awareness is not None,
        )

    async def _permalink(self, channel: str, ts: str) -> str | None:
        if self.user_client is None:
            return None
        try:
            response = await self.user_client.chat_getPermalink(channel=channel, message_ts=ts)
        except Exception as exc:
            logger.info("chat.getPermalink failed channel=%s error=%s", channel, type(exc).__name__)
            return None
        return response.get("permalink")

    async def _relevance_context(self, owner: str) -> dict[str, Any]:
        """Spec 18 §3: who the user is and what they are working on, compactly, for the relevance pass."""
        workstreams = await self.store.active_records(owner, ["workstream"])
        people = await self.store.active_records(owner, ["person"])
        return {
            "user": {"slack_id": owner, "names": await self.users.names(owner)},
            "workstreams": [record["title"] for record in workstreams[:15]],
            "people": [record["title"] for record in people[:30]],
            "open_commitments": [
                {key: row[key] for key in ("id", "commitment", "person", "waiting_on", "due_date") if row.get(key)}
                for row in await self.store.open_commitments(owner, 30)
            ],
        }

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


class DirectMessages:
    """Proactive messages go to the owner's DM, opened first so the thread key matches their replies."""

    def __init__(self, client: Any, egress: SlackEgress) -> None:
        self.client = client
        self.egress = egress

    async def open(self, owner: str) -> str:
        return await open_dm(self.client, owner)

    async def post(self, channel: str, text: str, blocks: list[dict[str, Any]]) -> str | None:
        return await self.egress(text=text, channel=channel, blocks=blocks)


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
