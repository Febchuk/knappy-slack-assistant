"""Deterministic tools available to the fast path and the ReAct loop."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import approval_blocks
from knappy.ingestion.embed import generate_embedding

current_owner: ContextVar[str | None] = ContextVar("knappy_owner", default=None)

_HISTORY_STOP = frozenset(
    {"what", "did", "we", "you", "say", "said", "about", "the", "slack", "channel", "message", "messages"}
)


class ToolRegistry:
    def __init__(self, repo: SqliteRepository, workspace_id: str, history: Any | None = None) -> None:
        self.repo = repo
        self.workspace_id = workspace_id
        self.history = history

    def _owner(self) -> str | None:
        return current_owner.get()

    async def search_commitments(
        self,
        query: str,
        status: str | None = "PENDING",
        due_before: str | None = None,
        match_text: bool = True,
    ) -> list[dict[str, Any]]:
        return await self.repo.search_commitments(
            self.workspace_id,
            query=query,
            query_embedding=generate_embedding(query) if match_text else None,
            status=status,
            due_before=due_before,
            owner_user_id=self._owner(),
            match_text=match_text,
        )

    async def query_relationship_graph(
        self,
        contact_name: str | None = None,
        company: str | None = None,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        contacts = await self.repo.find_contacts(
            self.workspace_id,
            name=contact_name,
            company=company,
            owner_user_id=self._owner(),
        )
        if topic and not contacts:
            matches = await self.repo.nearest_interactions(
                generate_embedding(topic),
                workspace_id=self.workspace_id,
                owner_user_id=self._owner(),
            )
            return matches
        return contacts

    async def get_meeting_context(self, contact_name: str, limit: int = 5) -> list[dict[str, Any]]:
        contacts = await self.repo.find_contacts(
            self.workspace_id,
            name=contact_name,
            owner_user_id=self._owner(),
        )
        if not contacts:
            return []
        return await self.repo.recent_interactions(contacts[0]["id"], limit=limit)

    async def stage_outbound_action(
        self,
        action_type: str,
        recipient: str,
        summary: str,
        payload: dict[str, Any],
        *,
        user_id: str,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        staged = {
            "action_type": action_type,
            "recipient_identifier": payload.get("recipient_identifier", recipient),
            "recipient_name": recipient,
            "preview_summary": summary,
            "staged_content": payload.get("staged_content") or summary,
            "metadata": payload.get("metadata") or {},
        }
        draft_id = await self.repo.create_draft(
            workspace_id=self.workspace_id,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            action_type=action_type,
            payload=staged,
        )
        return {"draft_id": draft_id, "blocks": approval_blocks(draft_id, recipient, staged["staged_content"])}

    async def search_slack_history(
        self,
        query: str,
        channel_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        client = self.history
        if client is None:
            return []
        channels: list[str] = []
        if channel_id:
            channels.append(channel_id)
        try:
            listed = await client.users_conversations(
                types="public_channel,private_channel,im",
                exclude_archived=True,
                limit=100,
            )
            for channel in listed.get("channels") or []:
                cid = channel.get("id")
                if cid and cid not in channels:
                    channels.append(cid)
        except Exception:
            pass
        tokens = []
        for raw in query.lower().split():
            token = raw.strip(".,!?:;\"'")
            if len(token) > 2 and token not in _HISTORY_STOP:
                tokens.append(token)
        hits: list[dict[str, Any]] = []
        for cid in channels[:15]:
            try:
                history = await client.conversations_history(channel=cid, limit=limit)
            except Exception:
                continue
            for message in history.get("messages") or []:
                text = message.get("text") or ""
                if tokens and not any(token in text.lower() for token in tokens):
                    continue
                hits.append(
                    {
                        "channel": cid,
                        "user": message.get("user"),
                        "ts": message.get("ts"),
                        "text": text,
                    }
                )
                if len(hits) >= limit:
                    return hits
        return hits

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        method = getattr(self, name)
        return await method(**arguments)


def format_commitment_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"I couldn't find any commitments regarding {query}."
    parts: list[str] = []
    for row in results:
        name = row.get("contact_name") or "them"
        commitment = (row.get("commitment") or "").rstrip(".")
        if commitment.lower().startswith("send "):
            parts.append(f"You promised to send {name} {commitment[5:]}.")
        else:
            parts.append(f"{name} has a pending commitment: {commitment}.")
    return " ".join(parts)


def format_contact_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"I couldn't find any contacts regarding {query}."
    parts = []
    for row in results:
        company = f" at {row['company']}" if row.get("company") else ""
        parts.append(f"{row.get('name') or row.get('contact_name')}{company}")
    return "I found " + "; ".join(parts) + "."


def format_history_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"I couldn't find recent Slack messages regarding {query}."
    lines = [row["text"] for row in results if row.get("text")]
    return "Recent Slack messages: " + " | ".join(lines)


def format_meeting_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"I couldn't find any meeting notes regarding {query}."
    summaries = [row.get("summary") or "" for row in results if row.get("summary")]
    return "Recent notes: " + " | ".join(summaries)
