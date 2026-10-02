"""Execute an approved Slack DM. Other action types stay unconnected."""

from __future__ import annotations

from typing import Any


class SlackActionExecutor:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def execute(self, draft: dict[str, Any]) -> None:
        if draft.get("action_type") != "SEND_SLACK_DM":
            raise RuntimeError(f"No provider for {draft.get('action_type')}")
        payload = draft["payload"]
        channel = payload.get("recipient_identifier")
        if not channel:
            raise RuntimeError("Missing recipient")
        await self.client.chat_postMessage(
            channel=channel,
            text=payload.get("staged_content") or payload.get("preview_summary") or " ",
        )
