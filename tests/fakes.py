"""Test doubles. The regex heuristics that once ran in production live here only."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel

from knappy.heartbeat.triage import TriageJudgment
from knappy.ingestion.extract import ExtractedInteraction
from knappy.llm.types import Message, ModelTurn, SchemaT, Tier, ToolCall, ToolResult, ToolSpec, UserMessage

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _next_weekday(now: datetime, weekday: int) -> datetime:
    days = (weekday - now.weekday()) % 7
    if days == 0:
        days = 7
    return (now + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)


def resolve_due(text: str, now: datetime) -> str | None:
    lower = text.lower()
    hour = 17
    minute = 0
    clock = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridian = clock.group(3)
        if meridian == "pm" and hour < 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
    if "tomorrow" in lower:
        due = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return due.strftime("%Y-%m-%dT%H:%M:%S")
    for name, index in WEEKDAYS.items():
        if name in lower:
            due = _next_weekday(now, index).replace(hour=hour, minute=minute)
            return due.strftime("%Y-%m-%dT%H:%M:%S")
    return None


def heuristic_extract(text: str, now: datetime | None = None) -> ExtractedInteraction:
    current = now or datetime.now()
    cleaned = re.sub(r"^note:\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    name_match = re.search(r"(?:with|sync with)\s+([A-Za-z][A-Za-z'-]*)", cleaned, re.IGNORECASE)
    contact_name = name_match.group(1) if name_match else "Unknown"
    company_match = re.search(r"from\s+([^,]+)", cleaned, re.IGNORECASE)
    company = company_match.group(1).strip() if company_match else None
    email_match = re.search(r"[\w.+-]+@[\w.-]+", cleaned)
    commitment = None
    promised = re.search(r"promised to\s+(.+)", cleaned, re.IGNORECASE)
    if promised:
        commitment = promised.group(1).rstrip(".").strip()
    return ExtractedInteraction(
        contact_name=contact_name,
        contact_email=email_match.group(0) if email_match else None,
        company=company,
        summary=cleaned,
        commitment=commitment,
        due_date=resolve_due(cleaned, current),
        importance="HIGH" if commitment else "MEDIUM",
    )


async def heuristic_triage(candidate: dict[str, Any]) -> dict[str, float | str]:
    hours = candidate.get("hours_until_due")
    if isinstance(hours, (int, float)) and hours <= 4:
        return {"interrupt_probability": 0.9, "strategy": "immediate_dm", "strategy_confidence": 0.9, "consequence_score": 2.0}
    if isinstance(hours, (int, float)):
        return {"interrupt_probability": 0.55, "strategy": "batch_into_morning_digest", "strategy_confidence": 0.8, "consequence_score": 1.0}
    return {"interrupt_probability": 0.2, "strategy": "suppress_low_value", "strategy_confidence": 0.8, "consequence_score": 0.2}


def heuristic_turn(system: str, contents: list[Message]) -> ModelTurn:
    """Keyword stand-in for the model: picks a tool, then answers from what the tools returned."""
    last_user = max(index for index, item in enumerate(contents) if isinstance(item, UserMessage))
    query = contents[last_user].text
    results = [item for item in contents[last_user:] if isinstance(item, ToolResult)]
    if results:
        return ModelTurn(text=" | ".join(describe(result.result) for result in results))
    lower = query.lower()
    if "what do i have" in lower:
        return ModelTurn(text=system.split("Open commitments", 1)[1])
    if any(phrase in lower for phrase in ("said", "say about", "in slack", "slack message")):
        return tool_turn("search_slack_history", {"query": query})
    if "follow up" in lower:
        recipient = "them"
        for token in query.split():
            if token[:1].isupper() and token.lower() not in {"follow", "up", "with"}:
                recipient = token.strip(".,!?")
                break
        return tool_turn(
            "stage_outbound_action",
            {
                "action_type": "SEND_SLACK_DM",
                "recipient": recipient,
                "summary": f"Follow up with {recipient}",
                "staged_content": f"Hi {recipient}, following up as we discussed.",
                "recipient_identifier": recipient,
            },
        )
    if "promise" in lower or "commitment" in lower:
        return tool_turn("search_commitments", {"query": query})
    return ModelTurn(text=f"Model answer to: {query}")


def describe(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(describe(item) for item in value) or "nothing found"
    if isinstance(value, dict):
        if "error" in value:
            return f"error: {value['error']}"
        if "draft_id" in value:
            return str(value["status"])
        if value.get("commitment"):
            return f"{value.get('contact_name') or 'you'}: {value['commitment']}"
        return str(value.get("text") or value.get("name") or value)
    return str(value)


def tool_turn(name: str, args: dict[str, Any]) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(id=f"call_{name}", name=name, args=args)])


def tool_results(contents: list[Message]) -> list[ToolResult]:
    return [item for item in contents if isinstance(item, ToolResult)]


class HeuristicModel:
    """Deterministic stand-in for Gemini with the pre-model regex behavior."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now

    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        contents: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> ModelTurn:
        return heuristic_turn(system, contents)

    async def generate_structured(self, *, tier: Tier, system: str, text: str, schema: type[SchemaT]) -> SchemaT:
        result: BaseModel
        if schema is ExtractedInteraction:
            result = heuristic_extract(text, self.now)
        elif schema is TriageJudgment:
            result = TriageJudgment.model_validate(await heuristic_triage(json.loads(text)))
        else:
            raise AssertionError(f"HeuristicModel has no answer for {schema.__name__}")
        return schema.model_validate(result.model_dump())


class FakeSlack:
    """Slack Web API double. Posts return a ts so placeholders can be updated."""

    def __init__(
        self,
        *,
        messages: list[dict] | None = None,
        tz: str = "America/New_York",
        ephemeral_error: BaseException | None = None,
    ) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []
        self.reactions: list[tuple[str, dict]] = []
        self.messages = messages or []
        self.tz = tz
        self.ephemeral_error = ephemeral_error
        self.users_info_calls = 0

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"100.{len(self.posts)}"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        if self.ephemeral_error is not None:
            raise self.ephemeral_error
        self.ephemerals.append(kwargs)

    async def reactions_add(self, **kwargs):
        self.reactions.append(("add", kwargs))

    async def reactions_remove(self, **kwargs):
        self.reactions.append(("remove", kwargs))

    async def users_info(self, *, user):
        self.users_info_calls += 1
        return {"ok": True, "user": {"id": user, "tz": self.tz}}

    async def conversations_history(self, *, channel, limit=20):
        return {"messages": self.messages}

    async def users_conversations(self, **kwargs):
        return {"channels": []}

    def shown(self, ts: str) -> dict:
        """The message at ts as the user now sees it: the post, overlaid by its latest update."""
        post = next(post for index, post in enumerate(self.posts, 1) if f"100.{index}" == ts)
        latest = [update for update in self.updates if update["ts"] == ts]
        return {**post, **latest[-1]} if latest else post


def dm(text: str, ts: str, *, user: str = "U1", channel: str = "D1", thread_ts: str | None = None) -> dict:
    event = {"text": text, "channel": channel, "channel_type": "im", "user": user, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def mention(text: str, ts: str, *, user: str = "U1", channel: str = "C1") -> dict:
    return {"type": "app_mention", "text": f"<@UBOT> {text}", "channel": channel, "user": user, "ts": ts}
