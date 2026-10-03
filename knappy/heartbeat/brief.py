"""What a proactive message is about, and the writer that turns it into words (Spec 16 §2-§3).

One agent-tier call per message writes the text for the user and, separately, any message to another
person in the user's voice. When the call fails or the budget is spent, a plain list goes out instead.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from knappy.agent.tools import current_owner
from knappy.awareness.store import AttentionItem, Update
from knappy.llm.types import Model
from knappy.memory.store import MemoryStore
from knappy.slack.users import Recipient

logger = logging.getLogger("knappy")

Kind = Literal["brief", "nudge"]

_VOICE = """
Items with a "recipient" also get a message to that person: written as the user, in the first person, to the
recipient, ready to send as-is. It acts on the item (a heads-up, an update, a polite chase). It never mentions
Knappy, reminders, or what the user promised. Match the user's style from their profile. Skip items without a recipient.
""".strip()

BRIEF_PROMPT = f"""
You are Knappy, the user's assistant in Slack, writing their morning brief. Lead with what matters today.
Use these sections, each only when it has content:
*Today:* what is due and what is overdue, most important first.
*Follow-ups:* people to get back to, and why.
*Carryover:* at most 2 open loops from yesterday's recap, only ones not already listed.
*Needs you:* the ATTENTION items: people asking the user for something or waiting on them, in the order given.
*Worth knowing:* the UPDATE items: changes in the user's work since the last brief.
Give every ATTENTION and UPDATE item its link as <link|label>.
Under 200 words. Slack mrkdwn: *bold*, bullets with •. No greeting, no sign-off, no filler.
Times are in the user's timezone. Never invent items that are not in the input.

{_VOICE}
""".strip()

NUDGE_PROMPT = f"""
You are Knappy, the user's assistant in Slack, sending one short unprompted DM about the item below.
Say why it matters now (due in a few hours, no reply since a check date) and what to do, in one or two sentences.
Slack mrkdwn. No greeting, no filler. Times are in the user's timezone.

{_VOICE}
""".strip()


@dataclass
class Item:
    """One thing worth surfacing: a commitment that is due or saw no progress, a contact gone quiet, or, from
    workspace awareness (Spec 18 §6), something waiting on the user (ATTENTION) or a change in their work (UPDATE).
    """

    kind: Literal["COMMITMENT", "CADENCE", "ATTENTION", "UPDATE"]
    owner: str
    interaction_id: str | None = None
    contact_id: str | None = None
    contact_name: str | None = None
    commitment: str | None = None
    due: datetime | None = None
    check_due: bool = False
    next_check: datetime | None = None
    on_no_progress: str | None = None
    waiting_on: str | None = None
    days_quiet: int | None = None
    briefing_id: str | None = None
    recipient: Recipient | None = None
    attention_id: str | None = None
    event_id: str | None = None
    summary: str | None = None
    permalink: str | None = None
    urgency: str | None = None

    @property
    def key(self) -> str | None:
        """Briefing dedupe: one item per commitment; a cadence item per contact."""
        return self.interaction_id or self.contact_id or self.attention_id or self.event_id

    @classmethod
    def attention(cls, owner: str, item: AttentionItem) -> Item:
        return cls(
            kind="ATTENTION", owner=owner, attention_id=item.id, summary=item.summary, permalink=item.permalink,
            urgency=item.urgency, due=_ts(item.due_at),
        )

    @classmethod
    def update(cls, owner: str, update: Update) -> Item:
        return cls(kind="UPDATE", owner=owner, event_id=update.event_id, summary=update.summary, permalink=update.permalink)

    @property
    def person(self) -> str | None:
        return self.contact_name or self.waiting_on

    @classmethod
    def from_row(cls, row: dict[str, Any], now: datetime) -> Item:
        last = _ts(row.get("last_interaction_ts"))
        next_check = _ts(row.get("next_check_at"))
        kind = row.get("kind") or ("COMMITMENT" if row.get("interaction_id") else "CADENCE")
        # Scan rows say whether the check-in is due; a queued item is re-derived (its progress was checked when queued).
        check_due = bool(row["check_due"]) if "check_due" in row else bool(next_check and next_check <= now and row.get("on_no_progress"))
        return cls(
            kind=kind,
            owner=row.get("owner_user_id") or "",
            interaction_id=_str(row.get("interaction_id")),
            contact_id=_str(row.get("contact_id")),
            contact_name=row.get("contact_name"),
            commitment=row.get("commitment"),
            due=_ts(row.get("due_date")),
            check_due=check_due,
            next_check=next_check,
            on_no_progress=row.get("on_no_progress"),
            waiting_on=row.get("waiting_on"),
            days_quiet=(now - last).days if kind == "CADENCE" and last else None,
            briefing_id=_str(row.get("briefing_id")),
        )

    def triage_view(self, now: datetime) -> dict[str, Any]:
        """What the triage gate sees (knappy.heartbeat.triage)."""
        if self.kind in ("ATTENTION", "UPDATE"):
            view = {"kind": self.kind, "summary": self.summary, "urgency": self.urgency}
            if self.due:
                view["due_date"] = self.due.strftime("%Y-%m-%d %H:%M:%S")
                view["hours_until_due"] = (self.due - now).total_seconds() / 3600
            return view
        view: dict[str, Any] = {
            "kind": self.kind, "contact_name": self.contact_name, "commitment": self.commitment,
            "waiting_on": self.waiting_on, "on_no_progress": self.on_no_progress,
            "days_since_last_contact": self.days_quiet,
        }
        if self.due:
            view["due_date"] = self.due.strftime("%Y-%m-%d %H:%M:%S")
            view["hours_until_due"] = (self.due - now).total_seconds() / 3600
        if self.check_due and self.next_check:
            view["hours_until_check"] = (self.next_check - now).total_seconds() / 3600
        return view

    def line(self, now: datetime, zone: ZoneInfo) -> str:
        """The item in one plain sentence: the fallback text, and its card's label."""
        if self.kind == "CADENCE":
            return f"You haven't been in touch with {self.contact_name} for {self.days_quiet} days."
        if self.kind in ("ATTENTION", "UPDATE"):
            when = f", due {self.due.astimezone(zone).strftime('%a %H:%M')}" if self.due else ""
            return f"{self.summary}{when}"
        if self.check_due and self.on_no_progress:
            waiting = f" (waiting on {self.waiting_on})" if self.waiting_on else ""
            return f"No progress yet on {self.commitment}{waiting}. You asked me to: {self.on_no_progress}"
        who = f" for {self.contact_name}" if self.contact_name else ""
        if self.due is None:
            return f"{self.commitment}{who}"
        when = self.due.astimezone(zone).strftime("%a %H:%M")
        state = "overdue since" if self.due <= now else "due"
        return f"{self.commitment}{who}, {state} {when}"

    def for_writer(self, number: int, now: datetime, zone: ZoneInfo) -> dict[str, Any]:
        view: dict[str, Any] = {"item": number, "kind": self.kind, "summary": self.line(now, zone)}
        if self.commitment:
            view["commitment"] = self.commitment
        if self.person:
            view["person"] = self.person
        if self.due:
            view["due_local"] = self.due.astimezone(zone).strftime("%A %Y-%m-%d %H:%M")
            view["hours_until_due"] = round((self.due - now).total_seconds() / 3600, 1)
        for name in ("on_no_progress", "waiting_on", "days_quiet"):
            if getattr(self, name) is not None:
                view[name] = getattr(self, name)
        if self.recipient and self.recipient.user_id:
            view["recipient"] = self.recipient.name
        if self.permalink:
            view["link"] = self.permalink
        return view


@dataclass(frozen=True)
class Written:
    text: str
    # Item index -> the message to that item's recipient, in the user's voice.
    messages: dict[int, str]


class ContactMessage(BaseModel):
    item: int = Field(..., description="The item number this message is for")
    text: str = Field(..., description="The message to the recipient, written as the user")


class ProactiveDraft(BaseModel):
    text: str = Field(..., description="What Knappy tells the user, in Slack mrkdwn")
    messages: list[ContactMessage] = Field(default_factory=list, description="One per item that has a recipient")


OverBudget = Callable[[str], Awaitable[bool]]


class ProactiveWriter:
    def __init__(self, model: Model | None, store: MemoryStore, over_budget: OverBudget | None = None) -> None:
        self.model = model
        self.store = store
        self.over_budget = over_budget

    async def write(self, owner: str, kind: Kind, items: list[Item], now: datetime, zone: ZoneInfo) -> Written:
        fallback = Written(fallback_text(kind, items, now, zone), {})
        if self.model is None or (self.over_budget is not None and await self.over_budget(owner)):
            return fallback
        payload = await self._payload(owner, kind, items, now, zone)
        token = current_owner.set(owner)
        try:
            draft = await self.model.generate_structured(
                tier="agent",
                system=BRIEF_PROMPT if kind == "brief" else NUDGE_PROMPT,
                text=json.dumps(payload, default=str),
                schema=ProactiveDraft,
            )
        except Exception:
            logger.exception("proactive writer failed owner=%s kind=%s; sending the plain list", owner, kind)
            return fallback
        finally:
            current_owner.reset(token)
        messages = {
            message.item - 1: message.text.strip()
            for message in draft.messages
            if 1 <= message.item <= len(items) and _reachable(items[message.item - 1]) and message.text.strip()
        }
        return Written(draft.text.strip() or fallback.text, messages)

    async def _payload(self, owner: str, kind: Kind, items: list[Item], now: datetime, zone: ZoneInfo) -> dict[str, Any]:
        local = now.astimezone(zone)
        profile = await self.store.profile(owner)
        payload: dict[str, Any] = {
            "now_local": local.strftime("%A %Y-%m-%d %H:%M"),
            "timezone": str(zone),
            "profile": (profile or {}).get("body") or "",
            "items": [item.for_writer(index + 1, now, zone) for index, item in enumerate(items)],
        }
        if kind == "brief":
            yesterday = await self.store.get_record(owner, f"episode_daily:{(local.date() - timedelta(days=1)).isoformat()}")
            if yesterday is not None and yesterday["status"] == "ACTIVE":
                payload["yesterday"] = yesterday["body"]
        return payload


def fallback_text(kind: Kind, items: list[Item], now: datetime, zone: ZoneInfo) -> str:
    def line(item: Item) -> str:
        text = item.line(now, zone)
        return f"{text} <{item.permalink}|link>" if item.permalink else text

    if kind == "nudge":
        return line(items[0])
    sections = [
        ("", [item for item in items if item.kind in ("COMMITMENT", "CADENCE")]),
        ("*Needs you:*\n", [item for item in items if item.kind == "ATTENTION"]),
        ("*Worth knowing:*\n", [item for item in items if item.kind == "UPDATE"]),
    ]
    body = "\n".join(title + "\n".join(f"• {line(item)}" for item in chosen) for title, chosen in sections if chosen)
    return "*Your morning brief*\n" + body


def _reachable(item: Item) -> bool:
    return item.recipient is not None and item.recipient.user_id is not None


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:19]).replace(tzinfo=timezone.utc)


def _str(value: Any) -> str | None:
    return None if value is None else str(value)
