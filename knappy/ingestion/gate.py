"""Tier 1 ingestion gate: Jev when available, regex otherwise."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class IngestionGateDecision(BaseModel):
    should_ingest: bool = Field(...)
    contains_commitment: bool = Field(...)
    contains_relationship_note: bool = Field(...)
    category: str = Field(default="CASUAL_CHAT")
    urgency_score: float = Field(default=0.0)
    confidence: float = Field(..., ge=0.0, le=1.0)


@runtime_checkable
class SystemOneGate(Protocol):
    async def evaluate(self, event: dict) -> IngestionGateDecision: ...


class JevSystemOneAdapter:
    """Maps a TypeSafe System One response onto an ingestion decision."""

    def __init__(self, client: Any | None = None, timeout_s: float = 0.35) -> None:
        self.client = client
        self.timeout_s = timeout_s

    async def evaluate(self, event: dict) -> IngestionGateDecision:
        text = event.get("text", "").strip()
        state = {
            "message": {
                "text": text,
                "user": event.get("user", "unknown"),
                "channel_type": event.get("channel_type", "im"),
            }
        }
        questions = {
            "has_actionable_intel": "Does the message contain relationship intelligence, a meeting note, or a commitment?",
            "has_commitment": "Does the message describe an explicit promise, deliverable, or agreed task?",
            "category": ["commitment", "meeting_note", "casual_chat", "system_noise"],
            "urgency": ["low", "soon", "urgent"],
        }
        response = await self._ask(state, questions)
        intel_prob = response.nouls["has_actionable_intel"].noul
        commitment_prob = response.nouls["has_commitment"].noul
        category_choice = response.choices["category"].choice
        category_conf = response.choices["category"].confidence
        urgency_score = response.scores["urgency"].score
        should_ingest = intel_prob >= 0.65 or (
            category_choice in ("commitment", "meeting_note") and category_conf >= 0.60
        )
        category_map = {
            "commitment": "COMMITMENT",
            "meeting_note": "MEETING_NOTE",
            "casual_chat": "CASUAL_CHAT",
            "system_noise": "SYSTEM_NOISE",
        }
        return IngestionGateDecision(
            should_ingest=should_ingest,
            contains_commitment=commitment_prob >= 0.60,
            contains_relationship_note=category_choice == "meeting_note" or "note:" in text.lower(),
            category=category_map.get(category_choice, "CASUAL_CHAT"),
            urgency_score=urgency_score,
            confidence=max(intel_prob, category_conf),
        )

    async def _ask(self, state: dict, questions: dict) -> Any:
        if self.client is not None:
            return await asyncio.wait_for(
                self.client.system_one(state=state, questions=questions),
                timeout=self.timeout_s,
            )
        from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

        typed_questions = {
            "has_actionable_intel": Noul(
                instructions="Does `message.text` contain relationship intelligence, a meeting note, or a commitment worth tracking for an executive?"
            ),
            "has_commitment": Noul(
                instructions="Does `message.text` describe an explicit promise, deliverable, or agreed task?"
            ),
            "category": Choice(
                instructions="What is the primary classification of `message.text`?",
                criteria={
                    "commitment": "A promise, deliverable, or agreed next step",
                    "meeting_note": "A note or summary of a discussion, sync, or contact interaction",
                    "casual_chat": "Routine chatter, greeting, acknowledgment, or banter",
                    "system_noise": "Automated alerts, status check, or trivial ping",
                },
            ),
            "urgency": Score(
                instructions="How urgent or time-sensitive is the commitment discussed in `message.text`?",
                criteria=[
                    "No explicit deadline or low priority",
                    "Actionable within the next few days",
                    "Urgent deadline due today, tomorrow, or strictly time-critical",
                ],
            ),
        }
        async with AsyncTypeSafeClient() as client:
            return await asyncio.wait_for(
                client.system_one(state=state, questions=typed_questions),
                timeout=self.timeout_s,
            )


class RegexFallbackAdapter:
    COMMITMENT_TRIGGERS = [
        r"\b(i will|i'll|let's meet|sending you|send you|follow up|by tomorrow|by friday|deadline|promise)\b",
        r"\b(met with|spoke with|sync with|1-on-1 with|note:)\b",
    ]

    async def evaluate(self, event: dict) -> IngestionGateDecision:
        text = event.get("text", "").strip()
        lower_text = text.lower()
        if lower_text.startswith("note:") or "met with" in lower_text:
            return IngestionGateDecision(
                should_ingest=True,
                contains_commitment="promise" in lower_text or "by " in lower_text,
                contains_relationship_note=True,
                category="MEETING_NOTE",
                urgency_score=1.0,
                confidence=0.95,
            )
        for pattern in self.COMMITMENT_TRIGGERS:
            if re.search(pattern, text, re.IGNORECASE):
                return IngestionGateDecision(
                    should_ingest=True,
                    contains_commitment=True,
                    contains_relationship_note=False,
                    category="COMMITMENT",
                    urgency_score=1.0,
                    confidence=0.75,
                )
        return IngestionGateDecision(
            should_ingest=False,
            contains_commitment=False,
            contains_relationship_note=False,
            category="CASUAL_CHAT",
            urgency_score=0.0,
            confidence=0.90,
        )


class CompositeSystemOneGate:
    def __init__(
        self,
        primary: SystemOneGate,
        fallback: SystemOneGate,
        acceptance_threshold: float = 0.65,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.acceptance_threshold = acceptance_threshold

    async def should_ingest(self, event: dict) -> tuple[bool, IngestionGateDecision | None]:
        from knappy.ingestion.filter import LocalStructuralFilter

        if not LocalStructuralFilter.should_evaluate(event):
            return False, None
        try:
            decision = await self.primary.evaluate(event)
        except Exception:
            decision = await self.fallback.evaluate(event)
        passes = decision.should_ingest and decision.confidence >= self.acceptance_threshold
        return passes, decision
