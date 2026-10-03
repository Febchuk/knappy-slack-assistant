"""Alert triage. Code owns the interrupt decision."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from knappy.llm.types import Model

ClassifyFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

TRIAGE_PROMPT = """
You decide whether a proactive reminder is worth interrupting a busy person for.
Interrupt only when acting soon matters: a deadline within hours, or a real cost to waiting.
Prefer the morning digest for anything that can wait until tomorrow.
Suppress items with little value. Silence is a good outcome.
A check-in (hours_until_check at or below zero) means the user asked to be told if nothing moved by then; on_no_progress says what they wanted done.
""".strip()


class TriageJudgment(BaseModel):
    interrupt_probability: float = Field(..., ge=0, le=1)
    strategy: Literal["immediate_dm", "batch_into_morning_digest", "suppress_low_value"]
    strategy_confidence: float = Field(..., ge=0, le=1)
    consequence_score: float = Field(..., ge=0, le=3, description="0 trivial, 3 severe cost of missing it")


def model_triage(model: Model) -> ClassifyFn:
    async def classify(candidate: dict[str, Any]) -> dict[str, Any]:
        judgment = await model.generate_structured(
            tier="light",
            system=TRIAGE_PROMPT,
            text=json.dumps(_triage_view(candidate), default=str),
            schema=TriageJudgment,
        )
        return judgment.model_dump()

    return classify


def _triage_view(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "kind", "contact_name", "commitment", "summary", "due_date", "hours_until_due", "days_since_last_contact",
        "hours_until_check", "waiting_on", "on_no_progress",
    )
    return {key: candidate[key] for key in keys if candidate.get(key) is not None}


class ProactiveAlertTriager:
    IMMEDIATE_INTERRUPT_THRESHOLD = 0.75
    STRATEGY_CONFIDENCE_THRESHOLD = 0.65

    def __init__(self, classify: ClassifyFn) -> None:
        self.classify = classify

    async def triage_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        response = await self.classify(candidate)
        interrupt_prob = response["interrupt_probability"]
        strategy = response["strategy"]
        strategy_conf = response["strategy_confidence"]
        consequence = response["consequence_score"]
        if (
            strategy == "immediate_dm"
            and interrupt_prob >= self.IMMEDIATE_INTERRUPT_THRESHOLD
            and strategy_conf >= self.STRATEGY_CONFIDENCE_THRESHOLD
        ):
            action = "DISPATCH_IMMEDIATE_DM"
        elif strategy == "batch_into_morning_digest" or interrupt_prob >= 0.40:
            action = "QUEUE_MORNING_DIGEST"
        else:
            action = "SUPPRESS_NOISE"
        return {
            "action": action,
            "interrupt_probability": interrupt_prob,
            "strategy": strategy,
            "strategy_confidence": strategy_conf,
            "consequence_score": consequence,
        }
