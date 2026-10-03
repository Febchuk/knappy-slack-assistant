"""Deterministic tools available to the fast path and the ReAct loop."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel, Field

from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import approval_blocks
from knappy.ingestion.embed import generate_embedding
from knappy.llm.types import ToolSpec

current_owner: ContextVar[str | None] = ContextVar("knappy_owner", default=None)

_HISTORY_STOP = frozenset(
    {"what", "did", "we", "you", "say", "said", "about", "the", "slack", "channel", "message", "messages"}
)


class SearchCommitmentsArgs(BaseModel):
    query: str = Field(..., description="What the commitment is about, or a person's name")
    status: Literal["PENDING", "FULFILLED", "CANCELLED", "EXPIRED"] | None = "PENDING"
    due_before: str | None = Field(default=None, description="ISO 8601 timestamp")


class QueryRelationshipGraphArgs(BaseModel):
    contact_name: str | None = None
    company: str | None = None
    topic: str | None = Field(default=None, description="Subject to match against past interactions")


class GetMeetingContextArgs(BaseModel):
    contact_name: str
    limit: int = Field(default=5, ge=1, le=20)


class StageOutboundActionArgs(BaseModel):
    action_type: Literal["SEND_SLACK_DM"]
    recipient: str = Field(..., description="Display name of the person to message")
    summary: str = Field(..., description="One line describing the action, shown on the approval card")
    staged_content: str = Field(..., description="The exact message text to send after approval")
    recipient_identifier: str | None = Field(default=None, description="Slack user id, if known")


class SearchSlackHistoryArgs(BaseModel):
    query: str
    channel_id: str | None = None
    limit: int = Field(default=20, ge=1, le=50)


TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("search_commitments", "Find the user's commitments and promises, by topic or person.", SearchCommitmentsArgs),
        ToolSpec("query_relationship_graph", "Look up the user's contacts by name, company, or topic.", QueryRelationshipGraphArgs),
        ToolSpec("get_meeting_context", "Recent notes and interactions with one contact.", GetMeetingContextArgs),
        ToolSpec(
            "stage_outbound_action",
            "Draft a message to another person. It is only sent after the user approves the card. Never claim it was sent.",
            StageOutboundActionArgs,
        ),
        ToolSpec("search_slack_history", "Search recent messages in Slack channels Knappy was invited to.", SearchSlackHistoryArgs),
    )
}


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
        staged_content: str,
        recipient_identifier: str | None = None,
        *,
        user_id: str,
        channel_id: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        staged = {
            "action_type": action_type,
            "recipient_identifier": recipient_identifier or recipient,
            "recipient_name": recipient,
            "preview_summary": summary,
            "staged_content": staged_content or summary,
            "metadata": {},
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

    def specs(self) -> list[ToolSpec]:
        return list(TOOL_SPECS.values())

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
