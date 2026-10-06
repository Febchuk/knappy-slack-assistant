"""The relevance pass and the digest (Spec 18 §3-§4): one light call per flushed conversation buffer.

Messages go in; typed observations about the user come out. Observations below the threshold, and every message
that yields none, are discarded. Only observations are stored, with a permalink back to the message.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from knappy.agent.session import Turn
from knappy.agent.tools import current_owner
from knappy.awareness.store import ATTENTION_KINDS, AwarenessStore
from knappy.db.repository import format_ts
from knappy.ingestion.embed import generate_embedding
from knappy.llm.types import Model
from knappy.memory.engine import MemoryEngine
from knappy.memory.store import parse_ts, redact

logger = logging.getLogger("knappy")

MENTION = re.compile(r"<@([A-Z0-9_]+)(?:\|[^>]*)?>")

ObservationKind = Literal[
    "asks_user", "assigns_user", "waiting_on_user", "user_committed", "commitment_moved", "workstream_update", "fyi"
]

# The ledger has no observation kinds; the observation kind rides in event metadata.
EVENT_KIND: dict[str, str] = {
    "asks_user": "learned",
    "assigns_user": "learned",
    "waiting_on_user": "learned",
    "user_committed": "commitment_made",
    "workstream_update": "changed",
    "fyi": "learned",
}


class Observation(BaseModel):
    ts: str = Field(..., description="The source message's ts, exactly as given in parentheses")
    kind: ObservationKind
    summary: str = Field(
        ..., description="One line, third person, naming people. For user_committed: what the user will do, as an imperative"
    )
    who: str | None = Field(default=None, description="The other party's name")
    due: str | None = Field(default=None, description="ISO 8601 with offset, if a date is stated or implied")
    commitment_id: str | None = Field(default=None, description="For commitment_moved: an id from open_commitments")
    completed: bool = Field(default=False, description="For commitment_moved: true when it is done or the expected thing arrived")
    urgency: Literal["low", "today", "now"] = "low"
    relevance: float = Field(..., ge=0, le=1, description="How much the user would want to know this, 0-1")


class Relevance(BaseModel):
    observations: list[Observation] = Field(default_factory=list)


RELEVANCE_PROMPT = """You read a batch of Slack messages from one conversation the user is in, for the user's personal assistant. Most workspace traffic is not about the user. Return observations only for messages that pertain to the user. An empty list is the usual answer.

Kinds:
- asks_user: a question or request addressed to the user: they are @-mentioned or named, it is their DM, or it is a reply to them.
- assigns_user: someone assigns work to the user.
- waiting_on_user: someone says they are blocked on, or waiting for, the user.
- user_committed: the user (marked "(the user)") promises something to someone: "I'll send it Friday".
- commitment_moved: a message that moves one of open_commitments forward, such as the thing the user was waiting for arriving. Give its id in commitment_id; completed=true when it is done or arrived.
- workstream_update: a decision, a deadline change, or a blocker in work the user is part of (see workstreams), or a change that affects something the user owes.
- fyi: something the user would clearly want to know, with no action for them.

Rules:
- Nothing for chit-chat, greetings, jokes, thanks, lunch plans, reactions, or work that does not involve the user. Requests to someone else or to the whole channel are not about the user unless the user is named.
- The user's own messages matter only for user_committed. The user answering a request is not an observation.
- One observation per message at most. ts is the number in parentheses after the message.
- summary: one line, third person, with names ("Sam asked the user to review the launch deck by Thursday"). For user_committed, an imperative of what the user will do ("Send Sam the budget").
- who: the other person's name. due: ISO 8601 with offset, resolved against "now", only when a time is stated or implied.
- urgency: now only when it needs the user within the next few hours; today when it needs them today; otherwise low.
- relevance: 0.8-1.0 for direct asks, assignments, and deadline changes in the user's work; 0.5-0.7 for useful context; below 0.5 for things the user would not care about."""


@dataclass(frozen=True)
class SlackMessage:
    """One message, held in memory between arrival and the relevance pass. Never written to the database."""

    channel: str
    ts: str
    user: str
    text: str
    thread_ts: str | None = None

    @property
    def at(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), timezone.utc)

    @property
    def reply(self) -> bool:
        return bool(self.thread_ts) and self.thread_ts != self.ts


@dataclass(frozen=True)
class Conversation:
    id: str
    name: str | None
    direct: bool  # a DM or group DM: everything said there is to its members


Names = Callable[[str], Awaitable[str]]
Permalink = Callable[[str, str], Awaitable[str | None]]


class RelevancePass:
    def __init__(
        self,
        model: Model,
        memory: MemoryEngine,
        store: AwarenessStore,
        *,
        name: Names,
        permalink: Permalink,
        context: Callable[[str], Awaitable[dict]],
        threshold: float = 0.5,
    ) -> None:
        self.model = model
        self.memory = memory
        self.store = store
        self.name = name
        self.permalink = permalink
        self.context = context
        self.threshold = threshold

    async def process(self, owner: str, conversation: Conversation, messages: list[SlackMessage], now: datetime) -> int:
        """Run one batch. Returns how many observations were admitted."""
        observations = await self._observe(owner, conversation, messages, now)
        by_ts = {message.ts: message for message in messages}
        admitted = [obs for obs in observations if self._admissible(obs, by_ts)]
        open_ids = {row["id"] for row in await self.memory.store.open_commitments(owner, 500)}
        admitted = [obs for obs in admitted if obs.kind != "commitment_moved" or obs.commitment_id in open_ids]
        links = {obs.ts: await self.permalink(conversation.id, obs.ts) for obs in admitted}
        senders = {obs.ts: by_ts[obs.ts].user for obs in admitted}
        async with self.memory.store.repo.transaction():
            for obs in admitted:
                await self._digest(owner, conversation, obs, by_ts[obs.ts], senders[obs.ts], links[obs.ts], now)
            answered = 0
            for message in messages:
                if message.user == owner:
                    answered += await self.store.answered(
                        owner, conversation.id, thread_ts=message.thread_ts, ts=message.ts, direct=conversation.direct, now=now
                    )
            await self.store.advance_cursor(owner, conversation.id, max(by_ts, key=float))
        logger.info(
            "awareness flush owner=%s channel=%s messages=%d observations=%d admitted=%d answered=%d",
            owner, conversation.id, len(messages), len(observations), len(admitted), answered,
        )
        return len(admitted)

    async def _observe(self, owner: str, conversation: Conversation, messages: list[SlackMessage], now: datetime) -> list[Observation]:
        people = {user for message in messages for user in [message.user, *MENTION.findall(message.text)]}
        names = {user: await self.name(user) for user in people}
        lines = []
        for message in messages:
            sender = names[message.user]
            if message.user == owner:
                sender += " (the user)"
            where = f" [reply in thread {message.thread_ts}]" if message.reply else ""
            text = MENTION.sub(lambda match: "@" + names[match.group(1)], message.text)
            lines.append(f"{sender}{where}: {text} ({message.ts})")
        payload = {
            "now": now.isoformat(),
            "conversation": conversation.name or conversation.id,
            "is_direct_message": conversation.direct,
            **await self.context(owner),
        }
        text = json.dumps(payload, default=str) + "\n\nMessages:\n" + "\n".join(lines)
        token = current_owner.set(owner)
        try:
            result = await self.model.generate_structured(tier="light", system=RELEVANCE_PROMPT, text=text, schema=Relevance)
        finally:
            current_owner.reset(token)
        return result.observations

    def _admissible(self, obs: Observation, by_ts: dict[str, SlackMessage]) -> bool:
        if obs.ts not in by_ts:
            logger.info("awareness dropped observation for unknown ts=%s kind=%s", obs.ts, obs.kind)
            return False
        if obs.relevance < self.threshold:
            return False
        return bool(obs.summary.strip())

    async def _digest(
        self, owner: str, conversation: Conversation, obs: Observation, message: SlackMessage, sender: str,
        link: str | None, now: datetime,
    ) -> None:
        store = self.memory.store
        source = f"{conversation.id}:{obs.ts}"
        due = parse_ts(obs.due)
        if obs.kind in ATTENTION_KINDS:
            await self.store.upsert_item(
                owner, kind=obs.kind, summary=redact(obs.summary), who=obs.who,
                who_slack_id=sender if sender != owner else None, channel_id=conversation.id,
                channel_name=conversation.name, thread_ts=message.thread_ts, source_ts=obs.ts, permalink=link,
                due_at=due, urgency=obs.urgency, now=now,
            )
        if await self._seen(owner, source, obs.kind):
            return
        kind = EVENT_KIND.get(obs.kind) or ("commitment_done" if obs.completed else "commitment_progress")
        metadata = {"observation": obs.kind, "permalink": link, "where": conversation.name, "who": obs.who, "urgency": obs.urgency}
        event_id = await store.add_event(
            owner, kind=kind, summary=obs.summary, occurred_at=message.at, score=obs.relevance, now=now,
            sources=[("slack_message", source)], metadata={key: value for key, value in metadata.items() if value is not None},
        )
        if obs.kind == "user_committed":
            commitment_id = await self._commit(owner, conversation, obs, message, due)
            await store.link_events_to_commitment([event_id], commitment_id)
        elif obs.kind == "commitment_moved":
            await store.link_events_to_commitment([event_id], obs.commitment_id or "")
            if obs.completed:
                await store.finish_commitment(obs.commitment_id or "")
        elif obs.kind not in ATTENTION_KINDS:
            where = conversation.name or "Slack"
            await store.append(owner, f"awareness:{conversation.id}", Turn("user", f"[Seen in {where}] {obs.summary}"))

    async def _seen(self, owner: str, source: str, observation: str) -> bool:
        """The same message read twice (an edit, a catch-up after a crash) is digested once per kind."""
        cursor = await self.memory.store.repo.connection.execute(
            """
            SELECT e.metadata FROM memory_provenance p JOIN memory_events e ON e.id = p.target_id
            WHERE p.owner_user_id = ? AND p.target_type = 'event' AND p.source_type = 'slack_message' AND p.source_id = ?
            """,
            (owner, source),
        )
        return any(json.loads(row["metadata"] or "{}").get("observation") == observation for row in await cursor.fetchall())

    async def _commit(self, owner: str, conversation: Conversation, obs: Observation, message: SlackMessage, due: datetime | None) -> str:
        store = self.memory.store
        title = redact(obs.summary.strip())
        for row in await store.open_commitments(owner, 500):
            if row["commitment"].strip().lower() == title.lower():
                return row["id"]
        fields = {
            "workspace_id": store.workspace_id,
            "source_type": "DIRECT_DM" if conversation.id.startswith("D") else "APP_MENTION",
            "channel_id": conversation.id,
            "thread_ts": message.thread_ts or message.ts,
            "raw_text": title,
            "summary": title,
            "commitment": title,
            "due_date": format_ts(due) if due else None,
            "embedding": generate_embedding(title),
            "owner_user_id": owner,
        }
        repo = store.repo
        if obs.who:
            _contact, commitment_id = await repo.record_interaction(contact_name=obs.who, **fields)
        else:
            commitment_id = await repo.insert_interaction(contact_id=None, **fields)
        return str(commitment_id)
