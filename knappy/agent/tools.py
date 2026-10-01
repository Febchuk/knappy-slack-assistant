"""Deterministic tools available to the fast path and the ReAct loop."""

from __future__ import annotations

from typing import Any

from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import approval_blocks
from knappy.ingestion.embed import generate_embedding


class ToolRegistry:
    def __init__(self, repo: SqliteRepository, workspace_id: str) -> None:
        self.repo = repo
        self.workspace_id = workspace_id

    async def search_commitments(
        self,
        query: str,
        status: str | None = "PENDING",
        due_before: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self.repo.search_commitments(
            self.workspace_id,
            query=query,
            query_embedding=generate_embedding(query),
            status=status,
            due_before=due_before,
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
        )
        if topic and not contacts:
            matches = await self.repo.nearest_interactions(
                generate_embedding(topic),
                workspace_id=self.workspace_id,
            )
            return matches
        return contacts

    async def get_meeting_context(self, contact_name: str, limit: int = 5) -> list[dict[str, Any]]:
        contacts = await self.repo.find_contacts(self.workspace_id, name=contact_name)
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


def format_meeting_results(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"I couldn't find any meeting notes regarding {query}."
    summaries = [row.get("summary") or "" for row in results if row.get("summary")]
    return "Recent notes: " + " | ".join(summaries)
