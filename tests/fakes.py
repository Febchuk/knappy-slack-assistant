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


def heuristic_turn(contents: list[Message]) -> ModelTurn:
    if any(isinstance(item, ToolResult) for item in contents):
        return ModelTurn(text="Done.")
    user = next(item.text for item in contents if isinstance(item, UserMessage))
    query = user.split("User:", 1)[-1].strip()
    lower = query.lower()
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
    return tool_turn("search_commitments", {"query": user})


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
        return heuristic_turn(contents)

    async def generate_structured(self, *, tier: Tier, system: str, text: str, schema: type[SchemaT]) -> SchemaT:
        result: BaseModel
        if schema is ExtractedInteraction:
            result = heuristic_extract(text, self.now)
        elif schema is TriageJudgment:
            result = TriageJudgment.model_validate(await heuristic_triage(json.loads(text)))
        else:
            raise AssertionError(f"HeuristicModel has no answer for {schema.__name__}")
        return schema.model_validate(result.model_dump())
