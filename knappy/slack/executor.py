"""Execute an approved draft: a Slack DM, a document shared to someone's DM, a reply posted as the user, or an app
tool call through MCP (Spec 20 §6).

Other action types stay unconnected.
"""

from __future__ import annotations

import json
from typing import Any

from knappy.files.service import open_dm, share_filename
from knappy.files.store import DocumentStore
from knappy.mcp.hub import McpHub, NotConnected


class SlackActionExecutor:
    def __init__(
        self,
        client: Any,
        documents: DocumentStore | None = None,
        user_client: Any | None = None,
        user_id: str | None = None,
        mcp: McpHub | None = None,
    ) -> None:
        self.client = client
        self.mcp = mcp
        self.documents = documents
        # The owner's own token (Spec 18 §5). A reply in their conversations goes out under their name.
        self.user_client = user_client
        self.user_id = user_id

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
        elif action == "POST_THREAD_REPLY":
            if self.user_client is None or draft["user_id"] != self.user_id:
                raise RuntimeError("POST_THREAD_REPLY posts only for the owner of SLACK_USER_TOKEN")
            thread_ts = (payload.get("metadata") or {}).get("reply_thread_ts") or None
            await self.user_client.chat_postMessage(
                channel=recipient, text=payload.get("staged_content") or " ", **({"thread_ts": thread_ts} if thread_ts else {})
            )
        elif action == "APP_ACTION":
            await self._app_action(draft["user_id"], payload)
        else:
            raise RuntimeError(f"No provider for {action}")

    async def _app_action(self, owner: str, payload: dict[str, Any]) -> None:
        """Call the approved tool once with the owner's credential. Any failure raises, so the draft is FAILED."""
        if self.mcp is None:
            raise RuntimeError("APP_ACTION needs MCP, which is off")
        metadata = payload["metadata"]
        body_field = metadata.get("body_field")
        if body_field:
            arguments = {**metadata["arguments"], body_field: payload["staged_content"]}
        else:
            arguments = json.loads(payload["staged_content"])
            if not isinstance(arguments, dict):
                raise RuntimeError("Edited arguments must be a JSON object")
        result = await self.mcp.call(owner, metadata["server"], metadata["tool"], arguments)
        if isinstance(result, NotConnected):
            raise RuntimeError(f"{metadata['server']} is not connected for {owner}")
        if result.is_error:
            raise RuntimeError(f"{metadata['server']}.{metadata['tool']} failed: {result.text[:500]}")

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
