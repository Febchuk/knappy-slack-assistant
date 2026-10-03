"""Execute an approved draft: a Slack DM, or a document shared to someone's DM. Other action types stay unconnected."""

from __future__ import annotations

from typing import Any

from knappy.files.service import open_dm, share_filename
from knappy.files.store import DocumentStore


class SlackActionExecutor:
    def __init__(self, client: Any, documents: DocumentStore | None = None) -> None:
        self.client = client
        self.documents = documents

    async def execute(self, draft: dict[str, Any]) -> None:
        payload = draft["payload"]
        recipient = payload.get("recipient_identifier")
        if not recipient:
            raise RuntimeError("Missing recipient")
        action = draft.get("action_type")
        if action == "SEND_SLACK_DM":
            await self.client.chat_postMessage(
                channel=recipient,
                text=payload.get("staged_content") or payload.get("preview_summary") or " ",
            )
        elif action == "SHARE_FILE":
            await self._share(draft["user_id"], recipient, payload)
        else:
            raise RuntimeError(f"No provider for {action}")

    async def _share(self, owner: str, recipient: str, payload: dict[str, Any]) -> None:
        document_id = (payload.get("metadata") or {}).get("document_id")
        document = await self.documents.get(owner, document_id) if self.documents and document_id else None
        if document is None:
            raise RuntimeError(f"No document {document_id} for {owner}")
        channel = await open_dm(self.client, recipient) if recipient[:1] in ("U", "W") else recipient
        await self.client.files_upload_v2(
            channel=channel,
            content=document.text,
            filename=share_filename(document),
            title=document.name,
            initial_comment=payload.get("staged_content") or None,
        )
