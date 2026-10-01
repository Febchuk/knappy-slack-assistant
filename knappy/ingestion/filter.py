"""Tier 0 structural filter. No network calls."""

from __future__ import annotations


class LocalStructuralFilter:
    BOT_SUBTYPES = ["bot_message", "channel_join", "channel_leave", "pinned_info"]

    @classmethod
    def should_evaluate(cls, event: dict) -> bool:
        if event.get("bot_id") or event.get("subtype") in cls.BOT_SUBTYPES:
            return False
        text = event.get("text", "").strip()
        tokens = text.split()
        if len(tokens) < 4:
            return False
        if text.startswith("http://") or text.startswith("https://"):
            return False
        return True
