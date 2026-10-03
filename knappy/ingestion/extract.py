"""Structured extraction of contacts and commitments."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from knappy.llm.types import Model

SYSTEM_PROMPT = """
You are a specialized relationship intelligence extractor.
Extract counterparty names, conversation summaries, and actionable commitments.
Never invent names or promises. If no commitment is made, leave commitment and due_date null.
Resolve relative dates (e.g., 'tomorrow', 'next Monday') relative to the reference date: {current_iso_timestamp}.
Write due_date as an ISO 8601 local timestamp without timezone, e.g. 2026-10-02T17:00:00.
""".strip()


class ExtractedInteraction(BaseModel):
    contact_name: str = Field(..., description="Name of the person the note is about")
    contact_email: str | None = Field(default=None)
    company: str | None = Field(default=None)
    summary: str = Field(..., description="One or two sentence essence of the interaction")
    commitment: str | None = Field(default=None, description="The promised action, or null")
    due_date: str | None = Field(default=None, description="ISO 8601 deadline, or null")
    importance: str = Field(default="MEDIUM", description="LOW, MEDIUM, or HIGH")


class SlmExtractor:
    def __init__(self, model: Model) -> None:
        self.model = model

    async def extract(self, text: str, now: datetime | None = None) -> ExtractedInteraction:
        current = now or datetime.now()
        return await self.model.generate_structured(
            tier="light",
            system=SYSTEM_PROMPT.format(current_iso_timestamp=current.isoformat(timespec="seconds")),
            text=text,
            schema=ExtractedInteraction,
        )
