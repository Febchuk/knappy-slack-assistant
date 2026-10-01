"""Alert triage. Code owns the interrupt decision."""

from __future__ import annotations

from typing import Any, Awaitable, Callable


ClassifyFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
