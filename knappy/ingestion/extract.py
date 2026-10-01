"""Structured extraction of contacts and commitments."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

SYSTEM_PROMPT = """
You are a specialized relationship intelligence extractor.
Extract counterparty names, conversation summaries, and actionable commitments.
Never invent names or promises. If no commitment is made, leave commitment and due_date null.
Resolve relative dates (e.g., 'tomorrow', 'next Monday') relative to the reference date: {current_iso_timestamp}.
""".strip()

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class ExtractedInteraction(BaseModel):
    contact_name: str = Field(...)
    contact_email: str | None = Field(default=None)
    company: str | None = Field(default=None)
    summary: str = Field(...)
    commitment: str | None = Field(default=None)
    due_date: str | None = Field(default=None)
    importance: str = Field(default="MEDIUM")


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


class SlmExtractor:
    """Uses an injected completion function, and the heuristic when none is configured."""

    def __init__(
        self,
        complete: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.complete = complete

    async def extract(self, text: str, now: datetime | None = None) -> ExtractedInteraction:
        current = now or datetime.now()
        if self.complete is None:
            return heuristic_extract(text, current)
        prompt = SYSTEM_PROMPT.format(current_iso_timestamp=current.isoformat())
        payload = await self.complete(prompt, text)
        return ExtractedInteraction.model_validate(payload)
