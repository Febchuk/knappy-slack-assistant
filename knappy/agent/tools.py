"""Tools the agent loop can call. Reads run freely; outbound messages are only staged."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field

from knappy.db.repository import SqliteRepository, format_ts
from knappy.hitl.blocks import approval_blocks
from knappy.ingestion.embed import generate_embedding
from knappy.llm.types import ToolSpec

current_owner: ContextVar[str | None] = ContextVar("knappy_owner", default=None)


@dataclass(frozen=True)
class SlackThread:
    channel_id: str
    thread_ts: str | None


# Where the current message came from. Tools read it instead of trusting model-supplied ids.
current_thread: ContextVar[SlackThread | None] = ContextVar("knappy_thread", default=None)

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
    channel_id: str | None = Field(default=None, description="Channel to search first; defaults to the current one")
    limit: int = Field(default=20, ge=1, le=50)


class AddCommitmentArgs(BaseModel):
    commitment: str = Field(..., description="What the user will do, as a short imperative: 'send Alex the deck'")
    person: str | None = Field(default=None, description="Who it is for or about, if anyone")
    due: AwareDatetime | None = Field(
        default=None, description="When it is due, ISO 8601 with the user's UTC offset, e.g. 2026-10-09T17:00:00-04:00"
    )


class CompleteCommitmentArgs(BaseModel):
    commitment_id: str = Field(..., description="Id from the open commitments list or search_commitments")
    status: Literal["FULFILLED", "CANCELLED"] = Field(
        default="FULFILLED", description="FULFILLED when done, CANCELLED when no longer needed"
    )


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
        ToolSpec("add_commitment", "Record something the user will do, or asked to be reminded about.", AddCommitmentArgs),
        ToolSpec("complete_commitment", "Mark one of the user's commitments done or cancelled.", CompleteCommitmentArgs),
    )
}

TOOL_STATUS: dict[str, str] = {
    "search_commitments": "checking your commitments",
    "query_relationship_graph": "looking up contacts",
    "get_meeting_context": "reading your notes",
    "stage_outbound_action": "drafting a message",
    "search_slack_history": "searching Slack",
    "add_commitment": "saving that",
    "complete_commitment": "updating your commitments",
}


@dataclass(frozen=True)
class StagedDraft:
    """A draft awaiting approval. The card goes to Slack; the model only sees the summary."""

    draft_id: str
    recipient: str
    blocks: list[dict[str, Any]]

    def for_model(self) -> dict[str, str]:
        return {"draft_id": self.draft_id, "status": f"Drafted for {self.recipient}. Waiting for the user's approval; not sent."}


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
    ) -> StagedDraft:
        thread = _thread()
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
            user_id=self._owner() or "",
            channel_id=thread.channel_id,
            thread_ts=thread.thread_ts,
            action_type=action_type,
            payload=staged,
        )
        return StagedDraft(draft_id, recipient, approval_blocks(draft_id, recipient, staged["staged_content"]))

    async def add_commitment(
        self,
        commitment: str,
        person: str | None = None,
        due: datetime | None = None,
    ) -> dict[str, Any]:
        thread = _thread()
        owner = self._owner() or ""
        fields = {
            "workspace_id": self.workspace_id,
            "source_type": "DIRECT_DM" if thread.channel_id.startswith("D") else "APP_MENTION",
            "channel_id": thread.channel_id,
            "thread_ts": thread.thread_ts,
            "raw_text": commitment,
            "summary": commitment,
            "commitment": commitment,
            "due_date": format_ts(due) if due else None,
            "embedding": generate_embedding(commitment),
            "owner_user_id": owner,
        }
        if person:
            _contact_id, interaction_id = await self.repo.record_interaction(contact_name=person, **fields)
        else:
            interaction_id = await self.repo.insert_interaction(contact_id=None, **fields)
        return {"id": interaction_id, "commitment": commitment, "person": person, "due_utc": fields["due_date"]}

    async def complete_commitment(self, commitment_id: str, status: str = "FULFILLED") -> dict[str, Any]:
        row = await self.repo.get_interaction(commitment_id)
        if (
            row is None
            or row["workspace_id"] != self.workspace_id
            or row["owner_user_id"] != (self._owner() or "")
            or not row.get("commitment")
        ):
            return {"error": f"No commitment with id {commitment_id}"}
        await self.repo.update_interaction_status(commitment_id, status)
        return {"id": commitment_id, "commitment": row["commitment"], "status": status}

    async def search_slack_history(
        self,
        query: str,
        channel_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        client = self.history
        if client is None:
            return []
        thread = current_thread.get()
        first = channel_id or (thread.channel_id if thread else None)
        channels: list[str] = [first] if first else []
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


def _thread() -> SlackThread:
    thread = current_thread.get()
    if thread is None:
        raise RuntimeError("No Slack thread in scope for this tool call")
    return thread
