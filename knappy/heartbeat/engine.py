"""The proactive heartbeat (Specs 06 and 16): a zero-LLM sweep, triage, the silence rules, then a brief or a nudge.

Every tick, per owner and in the owner's timezone:
- 08:00-12:00 local, once a day: the morning brief, if there is anything to say.
- Otherwise each due item is triaged. Immediate items become a DM unless it is quiet hours or the owner
  already had two today; those, and digest items, wait for the next brief.
No model is called unless an item qualifies. Every tick logs its outcome per owner: sent, queued, or silent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from knappy.agent.session import Turn
from knappy.awareness.store import AwarenessStore
from knappy.db.repository import format_ts
from knappy.heartbeat.brief import Item, Kind, ProactiveWriter
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.hitl.blocks import ProactiveCard, proactive_blocks
from knappy.memory.store import MemoryStore
from knappy.slack.users import RecipientResolver

logger = logging.getLogger("knappy")

BRIEF_HOUR = 8
BRIEF_UNTIL_HOUR = 12
QUIET_FROM_HOUR = 21
NUDGES_PER_DAY = 2
URGENT_CONSEQUENCE = 2.0
BRIEF_ITEMS = 10
DORMANT_PER_BRIEF = 3
NEEDS_YOU = 5
WORTH_KNOWING = 3
UPDATES_WITHIN = timedelta(hours=36)

Route = Literal["send", "queue", "drop"]


class ProactiveChannel(Protocol):
    """Where proactive messages go: the owner's DM."""

    async def open(self, owner: str) -> str: ...

    async def post(self, channel: str, text: str, blocks: list[dict[str, Any]]) -> str | None: ...


@dataclass
class OwnerTick:
    owner: str
    candidates: int = 0
    sent: int = 0
    queued: int = 0
    failed: bool = False

    @property
    def outcome(self) -> str:
        if self.failed:
            return "failed"
        return "sent" if self.sent else "queued" if self.queued else "silent"


def quiet_hours(local: datetime) -> bool:
    return local.hour >= QUIET_FROM_HOUR or local.hour < BRIEF_HOUR


def next_brief(local: datetime) -> datetime:
    day = local.date() if local.hour < BRIEF_HOUR else local.date() + timedelta(days=1)
    return datetime.combine(day, time(BRIEF_HOUR), local.tzinfo)


def route(decision: dict[str, Any], item: Item, local: datetime, nudges_today: int) -> Route:
    """Spec 16 §3.1. Immediate DMs are rare, and at night only for serious items due before the morning brief."""
    action = decision["action"]
    if action == "SUPPRESS_NOISE":
        return "drop"
    if action != "DISPATCH_IMMEDIATE_DM":
        return "queue"
    if quiet_hours(local):
        urgent = decision["consequence_score"] >= URGENT_CONSEQUENCE
        # Already overdue can wait for the brief; only a deadline that lands before it is worth waking for.
        if not (urgent and item.due is not None and local < item.due < next_brief(local)):
            return "queue"
    if nudges_today >= NUDGES_PER_DAY:
        return "queue"
    return "send"


class HeartbeatEngine:
    def __init__(
        self,
        store: MemoryStore,
        triager: ProactiveAlertTriager,
        *,
        writer: ProactiveWriter,
        recipients: RecipientResolver,
        timezone: Callable[[str], Awaitable[str]],
        channel: ProactiveChannel | None = None,
        attention: AwarenessStore | None = None,
    ) -> None:
        self.store = store
        self.attention = attention
        self.repo = store.repo
        self.workspace_id = store.workspace_id
        self.clock = store.clock
        self.triager = triager
        self.writer = writer
        self.recipients = recipients
        self.timezone = timezone
        self.channel = channel

    async def run_tick(self) -> list[OwnerTick]:
        now = self.clock()
        due: dict[str, list[Item]] = {}
        for row in await self.repo.scan_due_commitments(self.workspace_id, now):
            if not row.get("owner_user_id"):
                logger.warning("heartbeat skipped commitment=%s: it has no owner", row["interaction_id"])
                continue
            due.setdefault(row["owner_user_id"], []).append(Item.from_row(row, now))
        ticks = []
        for owner in sorted(set(due) | set(await self.repo.proactive_owners(self.workspace_id))):
            tick = OwnerTick(owner, len(due.get(owner, [])))
            try:
                await self._tick_owner(tick, due.get(owner, []), now)
            except Exception:
                tick.failed = True
                logger.exception("heartbeat failed owner=%s", owner)
            logger.info(
                "heartbeat tick owner=%s candidates=%d sent=%d queued=%d outcome=%s",
                owner, tick.candidates, tick.sent, tick.queued, tick.outcome,
            )
            ticks.append(tick)
        return ticks

    async def mark_done(self, interaction_id: str) -> None:
        await self.repo.update_interaction_status(interaction_id, "FULFILLED")

    async def snooze(self, interaction_id: str) -> str | None:
        return await self.repo.snooze_interaction(interaction_id, self.clock(), hours=24)

    async def _tick_owner(self, tick: OwnerTick, items: list[Item], now: datetime) -> None:
        owner = tick.owner
        zone = await self._zone(owner)
        local = now.astimezone(zone)
        profile = await self.store.profile(owner) or {}
        today = local.date().isoformat()
        if BRIEF_HOUR <= local.hour < BRIEF_UNTIL_HOUR and profile.get("brief_on") != today:
            await self._brief(tick, now, zone)
            return
        # Spec 18 §6: only urgency-now items that need the user may interrupt; each is decided once.
        urgent = [Item.attention(owner, row) for row in await self.attention.urgent_undecided(owner)] if self.attention else []
        items = items + urgent
        tick.candidates = len(items)
        if not items:
            return
        nudges = int(profile.get("nudges_sent") or 0) if profile.get("nudges_on") == today else 0
        queued = {str(row["interaction_id"] or row["contact_id"]) for row in await self.repo.queued_items(self.workspace_id, owner)}
        for item in items:
            decision = await self.triager.triage_candidate(item.triage_view(now))
            choice = route(decision, item, local, nudges)
            if choice == "send":
                await self._deliver(owner, "nudge", [item], now, zone)
                await self.store.count_nudge(owner, today)
                nudges += 1
                tick.sent += 1
            if item.attention_id:
                # Open attention items are in every brief anyway; nothing to enqueue.
                await self.attention.mark_surfaced([item.attention_id], now)
                tick.queued += choice == "queue"
                continue
            if choice == "queue":
                if item.key not in queued:
                    await self.repo.enqueue_briefing(
                        workspace_id=self.workspace_id, user_id=owner, kind=item.kind, summary=item.line(now, zone),
                        interaction_id=item.interaction_id, contact_id=item.contact_id, owner_user_id=owner,
                    )
                    queued.add(item.key)
                tick.queued += 1
            if item.interaction_id:
                await self.repo.mark_alerted(item.interaction_id, now)

    async def _brief(self, tick: OwnerTick, now: datetime, zone: ZoneInfo) -> None:
        """Spec 16 §3: queued items, what is due today or overdue, check-ins, and quiet contacts. Nothing on an empty day."""
        owner = tick.owner
        local = now.astimezone(zone)
        items: list[Item] = []
        for row in await self.repo.queued_items(self.workspace_id, owner):
            if row["interaction_id"] and row["interaction_status"] != "PENDING":
                await self.repo.dismiss_briefing(row["briefing_id"])
                continue
            if row["snoozed_until"] and row["snoozed_until"] > format_ts(now):
                continue
            items.append(Item.from_row(row, now))
        listed = {item.key for item in items}
        end_of_day = datetime.combine(local.date() + timedelta(days=1), time(0), zone)
        for row in await self.repo.scan_due_commitments(self.workspace_id, now, until=end_of_day, owner_user_id=owner):
            if str(row["interaction_id"]) not in listed:
                items.append(Item.from_row(row, now))
        for row in await self.repo.scan_dormant_contacts(self.workspace_id, now, owner, limit=DORMANT_PER_BRIEF):
            contact = Item.from_row(row, now)
            await self.repo.mark_contact_alerted(contact.contact_id or "", now)
            if contact.key in listed:
                continue
            decision = await self.triager.triage_candidate(contact.triage_view(now))
            if decision["action"] != "SUPPRESS_NOISE":
                items.append(contact)
        # The rest stay queued or unsurfaced for tomorrow; a brief is short.
        items = items[:BRIEF_ITEMS]
        if self.attention is not None:
            items += [Item.attention(owner, row) for row in await self.attention.items(owner, now, limit=NEEDS_YOU)]
            updates = await self.attention.updates(owner, now - UPDATES_WITHIN, unbriefed=True, limit=WORTH_KNOWING)
            items += [Item.update(owner, update) for update in updates]
        tick.candidates = len(items)
        if items:
            await self._deliver(owner, "brief", items, now, zone)
            for item in items:
                if item.briefing_id:
                    await self.repo.mark_briefing_delivered(item.briefing_id)
                if item.interaction_id:
                    await self.repo.mark_alerted(item.interaction_id, now)
            if self.attention is not None:
                await self.attention.mark_surfaced([item.attention_id for item in items if item.attention_id], now)
                await self.attention.mark_briefed([item.event_id for item in items if item.event_id])
            tick.sent = 1
        await self.store.set_brief_on(owner, local.date().isoformat())

    async def _deliver(self, owner: str, kind: Kind, items: list[Item], now: datetime, zone: ZoneInfo) -> None:
        """Write, stage any drafts to other people behind approval, post to the owner's DM, and log it as a turn."""
        for item in items:
            if item.person:
                item.recipient = await self.recipients.resolve(owner, item.person)
        written = await self.writer.write(owner, kind, items, now, zone)
        if self.channel is None:
            logger.info("heartbeat has no Slack channel; %s for owner=%s not posted", kind, owner)
            return
        dm = await self.channel.open(owner)
        cards = [await self._card(owner, dm, item, written.messages.get(index), now, zone) for index, item in enumerate(items)]
        cards = [card for card in cards if card is not None]
        ts = await self.channel.post(dm, written.text, proactive_blocks(written.text, cards))
        if ts:
            await self.store.append(owner, f"thread:{dm}:{ts}", Turn("assistant", _transcript(written.text, cards)), slack_ts=ts)

    async def _card(
        self, owner: str, dm: str, item: Item, message: str | None, now: datetime, zone: ZoneInfo
    ) -> ProactiveCard | None:
        if item.kind in ("ATTENTION", "UPDATE"):
            return ProactiveCard(label=item.line(now, zone), attention_id=item.attention_id, permalink=item.permalink)
        recipient = item.recipient
        if not item.interaction_id and not recipient:
            return None
        draft_id = None
        if recipient and recipient.user_id and message:
            draft_id = await self.repo.create_draft(
                workspace_id=self.workspace_id,
                user_id=owner,
                channel_id=dm,
                action_type="SEND_SLACK_DM",
                payload={
                    "action_type": "SEND_SLACK_DM",
                    "recipient_identifier": recipient.user_id,
                    "recipient_name": recipient.name,
                    "preview_summary": item.line(now, zone),
                    "staged_content": message,
                    "metadata": {"interaction_id": item.interaction_id or ""},
                },
            )
        return ProactiveCard(
            label=item.line(now, zone),
            interaction_id=item.interaction_id,
            recipient=recipient.name if recipient else None,
            recipient_id=recipient.user_id if recipient else None,
            draft_id=draft_id,
            message=message if draft_id else None,
            problem=recipient.problem if recipient else None,
        )

    async def _zone(self, owner: str) -> ZoneInfo:
        profile = await self.store.profile(owner)
        name = (profile or {}).get("timezone")
        if not name:
            name = await self.timezone(owner)
            await self.store.set_timezone(owner, name)
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")


def _transcript(text: str, cards: list[ProactiveCard]) -> str:
    """What the thread's agent sees as this message: the text, each item's commitment id, and any staged draft."""
    lines = [text]
    for card in cards:
        ref = f" (commitment id {card.interaction_id})" if card.interaction_id else ""
        ref = f" (attention id {card.attention_id})" if card.attention_id else ref
        lines.append(f"- {card.label}{ref}")
        if card.draft_id:
            lines.append(f"  Draft to {card.recipient}, waiting for approval: {card.message}")
        elif card.recipient:
            lines.append(f"  Nothing drafted to {card.recipient}{': ' + card.problem if card.problem else ''}.")
    return "\n".join(lines)
