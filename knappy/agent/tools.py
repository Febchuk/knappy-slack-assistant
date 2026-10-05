"""Tools the agent loop can call. Reads run freely; outbound messages are only staged."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from jsonschema import SchemaError, validators
from pydantic import AwareDatetime, BaseModel, Field
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, specification_with

from knappy.awareness.store import AwarenessStore
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.hitl.blocks import app_action_blocks, approval_blocks
from knappy.ingestion.embed import generate_embedding
from knappy.llm.types import Model, Recency, ToolSpec
from knappy.mcp.hub import McpHub, McpTool, NotConnected
from knappy.memory.types import RecordType, SavableType
from knappy.slack.users import RecipientResolver, UserDirectory
from knappy.web import WebFetcher

if TYPE_CHECKING:
    from knappy.awareness.ingest import Awareness
    from knappy.files.service import DocumentFormat, FileService
    from knappy.memory.engine import MemoryEngine

logger = logging.getLogger("knappy")

current_owner: ContextVar[str | None] = ContextVar("knappy_owner", default=None)
# The logged user turn being answered. Memory written by tools cites it as provenance.
current_turn: ContextVar[str | None] = ContextVar("knappy_turn", default=None)


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
    action_type: Literal["SEND_SLACK_DM", "SHARE_FILE", "POST_THREAD_REPLY"] = Field(
        ...,
        description=(
            "SEND_SLACK_DM for a message; SHARE_FILE to send one of the user's documents; "
            "POST_THREAD_REPLY to answer an attention item in its own conversation, posted as the user"
        ),
    )
    recipient: str = Field(..., description="Name of the person to message, or their <@U...> mention")
    summary: str = Field(..., description="One line describing the action, shown on the approval card")
    staged_content: str = Field(..., description="The exact message text to send after approval; for SHARE_FILE, the note with the file")
    recipient_identifier: str | None = Field(
        default=None, description="Their Slack user id (from a <@U...> mention) or email, if known; never guess one"
    )
    document_id: str | None = Field(default=None, description="Required for SHARE_FILE: the document to send")
    attention_id: str | None = Field(default=None, description="Required for POST_THREAD_REPLY: the attention item answered")


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


class RescheduleCommitmentArgs(BaseModel):
    commitment_id: str = Field(..., description="Id from the open commitments list or search_commitments")
    due: AwareDatetime = Field(
        ..., description="The new deadline, ISO 8601 with the user's UTC offset, e.g. 2026-10-12T09:00:00-04:00"
    )


class RememberArgs(BaseModel):
    text: str = Field(..., description="The fact, preference, or correction in the user's terms: 'Vegetarian; no fish either'")
    type: SavableType = Field(default="fact", description="preference for likes and dislikes, person for someone they know")
    about: str | None = Field(
        default=None, description="Short subject ('diet', 'manager'), or an existing record id to correct that record"
    )


class ForgetArgs(BaseModel):
    query_or_id: str = Field(..., description="A record id from memory_search, or a short description of what to forget")


class MemorySearchArgs(BaseModel):
    query: str = Field(..., description="Keywords; empty returns the most recently updated records")
    types: list[RecordType] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=25)


class MemoryReadArgs(BaseModel):
    id: str
    history: bool = Field(default=False, description="Include earlier versions of the record")


class SearchConversationsArgs(BaseModel):
    query: str
    since: date | None = Field(default=None, description="Only conversations on or after this date")


class WebSearchArgs(BaseModel):
    query: str = Field(..., description="A search engine query")
    recency: Recency = Field(default="any", description="week or month to limit results to recent pages")


class FetchUrlArgs(BaseModel):
    url: str = Field(..., description="The http(s) URL to read")
    question: str | None = Field(default=None, description="What you need from the page, to focus long pages")


class ReadFileArgs(BaseModel):
    document_id: str = Field(..., description="Id from list_files, memory_search, or a [Shared file] note")
    query: str | None = Field(default=None, description="What you need from the document; returns the best-matching parts")
    max_chars: int = Field(default=20_000, ge=1_500, le=100_000)


class ListFilesArgs(BaseModel):
    query: str | None = Field(default=None, description="Words from the file's name or subject; empty lists the most recent")
    limit: int = Field(default=10, ge=1, le=25)


class ListAttentionArgs(BaseModel):
    status: Literal["OPEN", "ANSWERED", "DONE", "DISMISSED", "SNOOZED"] = "OPEN"


class ResolveAttentionArgs(BaseModel):
    id: str = Field(..., description="Attention id from the list in the prompt or list_attention")
    status: Literal["DONE", "DISMISSED", "SNOOZED", "ANSWERED"] = Field(
        default="DONE", description="DONE when handled, DISMISSED when it doesn't need them, SNOOZED for 24 hours"
    )


class StopWatchingArgs(BaseModel):
    conversation: str = Field(..., description="The channel as the user named it, e.g. #random, or a <#C...> link")


class CreateDocumentArgs(BaseModel):
    title: str = Field(..., description="Short title, also used for the file name")
    content_markdown: str = Field(..., description="The full document")
    format: Literal["md", "txt", "csv"] = "md"


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
        ToolSpec(
            "reschedule_commitment",
            "Move an existing commitment's deadline. Use it instead of add_commitment when a date changes.",
            RescheduleCommitmentArgs,
        ),
        ToolSpec(
            "remember",
            "Save a lasting fact, preference, or correction about the user now. To correct a record, pass its id as about.",
            RememberArgs,
        ),
        ToolSpec(
            "forget",
            "Forget memory records and everything derived from them. Returns what was forgotten; tell the user.",
            ForgetArgs,
        ),
        ToolSpec(
            "memory_search",
            "Search what you know about the user: people, organizations, facts, preferences, decisions, workstreams, past days.",
            MemorySearchArgs,
        ),
        ToolSpec(
            "memory_read",
            "Read one memory record in full, with the sources it came from and when. Use it to answer 'why do you think that?'.",
            MemoryReadArgs,
        ),
        ToolSpec("search_conversations", "Search earlier conversations with the user by keywords.", SearchConversationsArgs),
        ToolSpec(
            "web_search",
            "Search the web. Returns a grounded answer, its sources, and the queries run. "
            "Use it for anything current or checkable: news, prices, dates, releases, weather, places, opening hours.",
            WebSearchArgs,
        ),
        ToolSpec("fetch_url", "Read one web page or PDF by URL, such as a link the user pasted.", FetchUrlArgs),
        ToolSpec(
            "read_file",
            "Read a document the user shared or you created earlier: the full text, or with query the best-matching parts.",
            ReadFileArgs,
        ),
        ToolSpec("list_files", "List the user's documents, newest first, with id, name, date, and summary.", ListFilesArgs),
        ToolSpec(
            "create_document",
            "Deliver a long piece of writing as a file in the user's own DM. No approval needed. "
            "Use it for answers over about 3,000 characters or when they ask for a doc, plan, or file.",
            CreateDocumentArgs,
        ),
        ToolSpec(
            "list_attention",
            "What is waiting on the user from their Slack conversations (requests, assignments, people blocked on them), "
            "and recent updates in their work, each with a link.",
            ListAttentionArgs,
        ),
        ToolSpec("resolve_attention", "Mark an attention item done, dismissed, answered, or snoozed.", ResolveAttentionArgs),
        ToolSpec(
            "stop_watching",
            "Stop reading one Slack conversation for the user, and forget what was learned from it.",
            StopWatchingArgs,
        ),
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
    "reschedule_commitment": "updating your commitments",
    "remember": "remembering that",
    "forget": "forgetting that",
    "memory_search": "checking what I know",
    "memory_read": "checking what I know",
    "search_conversations": "searching past conversations",
    "web_search": "searching the web",
    "fetch_url": "reading the page",
    "read_file": "reading the file",
    "list_files": "looking through your files",
    "create_document": "writing the document",
    "list_attention": "checking what's waiting on you",
    "resolve_attention": "updating that",
    "stop_watching": "updating what I watch",
    "list_apps": "checking your apps",
    "connect_app": "getting a connect link",
}


# Spec 20 §2. Gemini's function-name rule; an app tool whose `<server>__<tool>` breaks it is skipped, not renamed.
APP_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,63}$")
APP_TOOLS_TIMEOUT_S = 10.0
APP_RESULT_CHARS = 20_000
WRITE_NOTE = " Changes data: creates a draft the user must approve."
PERSONAL_LINK = "Connection links are personal: ask the user to DM Knappy to connect {app}."
NO_LINK = {
    "connected": "Already connected.",
    "unavailable": "Not set up on this Knappy yet: an admin must add its credentials.",
}


def app_specs(servers: dict[str, str]) -> list[ToolSpec]:
    """`list_apps` and `connect_app` over the configured servers, given as {name: title}."""
    choices = ", ".join(f"{name} ({title})" for name, title in servers.items())
    return [
        ToolSpec(
            "list_apps",
            "List the apps the user can connect (Lorikeet, Gmail, and so on) and whether each is connected.",
            {"type": "object", "properties": {}},
        ),
        ToolSpec(
            "connect_app",
            "Get the link the user opens to connect one of their apps to Knappy. Give them the link.",
            {
                "type": "object",
                "properties": {"server": {"type": "string", "enum": list(servers), "description": f"One of: {choices}"}},
                "required": ["server"],
            },
        ),
    ]


def _refs(node: Any) -> Any:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            elif key not in ("enum", "const", "default", "examples"):
                yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def app_tool_problem(name: str, schema: dict[str, Any]) -> Literal["name", "schema"] | None:
    """Why Gemini would refuse this declaration, or None.

    Measured 2026-10-04 (Spec 21 §4.2): the only schema Gemini rejected was a `$ref` that does not resolve inside the
    schema, and it failed the whole request, every other tool with it.
    """
    if not APP_TOOL_NAME.match(name):
        return "name"
    try:
        validators.validator_for(schema).check_schema(schema)
    except SchemaError:
        return "schema"
    resource = specification_with(str(schema.get("$schema", "")), default=DRAFT202012).create_resource(schema)
    resolver = Registry().resolver_with_root(resource)
    for ref in _refs(schema):
        try:
            resolver.lookup(ref)
        except Unresolvable:
            return "schema"
    return None


def app_tool_spec(tool: McpTool, app: str) -> ToolSpec | None:
    """One connected tool as `<server>__<tool>` with the server's schema verbatim (Spec 20 §2.2). None when unusable."""
    name = f"{tool.server}__{tool.name}"
    problem = app_tool_problem(name, tool.input_schema)
    if problem is not None:
        logger.warning("mcp tool skipped name=%s reason=%s", name, problem)
        return None
    description = f"[{app}] {tool.title or tool.name}. {tool.description}".strip()
    return ToolSpec(name, description + (WRITE_NOTE if tool.access == "write" else ""), tool.input_schema)


@dataclass(frozen=True)
class StagedDraft:
    """A draft awaiting approval. The card goes to Slack; the model only sees the summary.

    Without a draft_id the recipient could not be found, so the card shows the text with no send button.
    """

    draft_id: str | None
    recipient: str
    blocks: list[dict[str, Any]]
    problem: str | None = None

    def for_model(self) -> dict[str, str | None]:
        if self.draft_id is None:
            return {
                "draft_id": None,
                "status": (
                    f"Not staged: {self.problem}. The user sees the text but cannot send it. "
                    f"Ask them to @-mention {self.recipient} so you can draft it again."
                ),
            }
        return {"draft_id": self.draft_id, "status": f"Drafted for {self.recipient}. Waiting for the user's approval; not sent."}


class ToolRegistry:
    def __init__(
        self,
        repo: SqliteRepository,
        workspace_id: str,
        history: Any | None = None,
        memory: MemoryEngine | None = None,
        searcher: Model | None = None,
        fetcher: WebFetcher | None = None,
        files: FileService | None = None,
        recipients: RecipientResolver | None = None,
        attention: AwarenessStore | None = None,
        awareness: Awareness | None = None,
        clock: Callable[[], datetime] = utc_now,
        mcp: McpHub | None = None,
    ) -> None:
        self.mcp = mcp
        self.repo = repo
        self.clock = clock
        self.attention = attention or AwarenessStore(repo, workspace_id)
        self.awareness = awareness
        self.files = files
        self.recipients = recipients or RecipientResolver(repo, workspace_id, UserDirectory(history))
        self.workspace_id = workspace_id
        self.history = history
        self.memory = memory
        self.searcher = searcher
        self.fetcher = fetcher or WebFetcher()

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
        embedding = generate_embedding(topic) if topic and not contacts else None
        if embedding is not None:
            return await self.repo.nearest_interactions(
                embedding,
                workspace_id=self.workspace_id,
                owner_user_id=self._owner(),
            )
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
        document_id: str | None = None,
        attention_id: str | None = None,
    ) -> StagedDraft | dict[str, str]:
        thread = _thread()
        owner = self._owner() or ""
        if action_type == "POST_THREAD_REPLY":
            return await self._stage_reply(owner, thread, attention_id, summary, staged_content or summary)
        metadata: dict[str, str] = {}
        file_name = None
        if action_type == "SHARE_FILE":
            document = await self.files.documents.get(owner, document_id) if self.files and document_id else None
            if document is None:
                return {"error": f"No document with id {document_id}. Find it with list_files."}
            metadata = {"document_id": document.id, "file_name": document.name}
            file_name = document.name
        content = staged_content or summary
        who = await self.recipients.resolve(owner, recipient, slack_id=recipient_identifier)
        if who.user_id is None:
            blocks = approval_blocks(None, who.name, content, file_name, problem=who.problem)
            return StagedDraft(None, who.name, blocks, who.problem)
        staged = {
            "action_type": action_type,
            "recipient_identifier": who.user_id,
            "recipient_name": who.name,
            "preview_summary": summary,
            "staged_content": content,
            "metadata": metadata,
        }
        draft_id = await self.repo.create_draft(
            workspace_id=self.workspace_id,
            user_id=owner,
            channel_id=thread.channel_id,
            thread_ts=thread.thread_ts,
            action_type=action_type,
            payload=staged,
        )
        blocks = approval_blocks(draft_id, who.name, content, file_name, recipient_id=who.user_id)
        return StagedDraft(draft_id, who.name, blocks)

    async def _stage_reply(
        self, owner: str, thread: SlackThread, attention_id: str | None, summary: str, content: str
    ) -> StagedDraft | dict[str, str]:
        """Spec 18 §5: a reply in the attention item's own conversation, posted as the user once approved."""
        item = await self.attention.get(owner, attention_id or "")
        if item is None:
            return {"error": f"No attention item with id {attention_id}. Find it with list_attention."}
        reply_in = item.thread_ts or (None if item.channel_id.startswith("D") else item.source_ts)
        staged = {
            "action_type": "POST_THREAD_REPLY",
            "recipient_identifier": item.channel_id,
            "recipient_name": item.where,
            "preview_summary": summary,
            "staged_content": content,
            "metadata": {"attention_id": item.id, "reply_thread_ts": reply_in or ""},
        }
        draft_id = await self.repo.create_draft(
            workspace_id=self.workspace_id, user_id=owner, channel_id=thread.channel_id, thread_ts=thread.thread_ts,
            action_type="POST_THREAD_REPLY", payload=staged,
        )
        blocks = approval_blocks(draft_id, item.where, content, post_as_user=True)
        return StagedDraft(draft_id, item.where, blocks)

    async def list_attention(self, status: str = "OPEN") -> dict[str, Any]:
        owner = self._owner() or ""
        now = self.clock()
        items = await self.attention.items(owner, now, status)  # type: ignore[arg-type]
        updates = await self.attention.updates(owner, now - timedelta(days=3))
        return {
            "items": [item.for_model() for item in items],
            "recent_updates": [update.for_model() for update in updates],
            **({} if self.awareness else {"note": "Workspace awareness is off: no SLACK_USER_TOKEN is configured."}),
        }

    async def resolve_attention(self, id: str, status: str = "DONE") -> dict[str, Any]:
        owner = self._owner() or ""
        if not await self.attention.resolve(owner, id, status, self.clock()):  # type: ignore[arg-type]
            return {"error": f"No attention item with id {id}"}
        return {"id": id, "status": status}

    async def stop_watching(self, conversation: str) -> dict[str, Any]:
        if self.awareness is None:
            return {"error": "Workspace awareness is off, so no conversations are being read."}
        return await self.awareness.stop_watching(self._owner() or "", conversation)

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

    async def reschedule_commitment(self, commitment_id: str, due: datetime) -> dict[str, Any]:
        row = await self.repo.get_interaction(commitment_id)
        if (
            row is None
            or row["workspace_id"] != self.workspace_id
            or row["owner_user_id"] != (self._owner() or "")
            or not row.get("commitment")
            or row["status"] != "PENDING"
        ):
            return {"error": f"No open commitment with id {commitment_id}"}
        await self.repo.reschedule_commitment(commitment_id, due)
        return {"id": commitment_id, "commitment": row["commitment"], "due_utc": format_ts(due)}

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

    async def remember(self, text: str, type: str = "fact", about: str | None = None) -> dict[str, Any]:
        if self.memory is None:
            return _NO_MEMORY
        return await self.memory.remember(self._owner() or "", text, type, about, current_turn.get())

    async def forget(self, query_or_id: str) -> dict[str, Any]:
        if self.memory is None:
            return _NO_MEMORY
        return await self.memory.forget(self._owner() or "", query_or_id, current_turn.get())

    async def memory_search(self, query: str, types: list[str] | None = None, limit: int = 8) -> Any:
        if self.memory is None:
            return _NO_MEMORY
        return await self.memory.store.search(self._owner() or "", query, types=types or [], limit=limit)

    async def memory_read(self, id: str, history: bool = False) -> dict[str, Any]:
        if self.memory is None:
            return _NO_MEMORY
        return await self.memory.read(self._owner() or "", id, history)

    async def search_conversations(self, query: str, since: date | None = None) -> Any:
        if self.memory is None:
            return _NO_MEMORY
        start = datetime.combine(since, time(0), timezone.utc) if since else None
        # The message being answered is already logged; finding it again reads as a lead worth chasing.
        return await self.memory.store.search_conversations(self._owner() or "", query, start, exclude=current_turn.get())

    async def web_search(self, query: str, recency: Recency = "any") -> dict[str, Any]:
        if self.searcher is None:
            return {"error": "Web search is not available in this context."}
        return (await self.searcher.search(query, recency)).model_dump()

    async def fetch_url(self, url: str, question: str | None = None) -> dict[str, Any]:
        return await self.fetcher.fetch(url, question)

    async def read_file(self, document_id: str, query: str | None = None, max_chars: int = 20_000) -> dict[str, Any]:
        if self.files is None:
            return _NO_FILES
        owner = self._owner() or ""
        document = await self.files.documents.get(owner, document_id)
        if document is None:
            return {"error": f"No document with id {document_id}"}
        head = {"document_id": document.id, "name": document.name, "summary": document.summary}
        if query:
            parts = await self.files.documents.best_chunks(owner, document.id, query, max_chars)
            if parts:
                return {**head, "matches": [text for _seq, text in parts]}
        text = document.text[:max_chars]
        return {**head, "text": text, "truncated": len(text) < len(document.text)}

    async def list_files(self, query: str | None = None, limit: int = 10) -> Any:
        if self.files is None:
            return _NO_FILES
        return [document.listing() for document in await self.files.documents.recent(self._owner() or "", query, limit)]

    async def create_document(self, title: str, content_markdown: str, format: DocumentFormat = "md") -> dict[str, Any]:
        if self.files is None:
            return _NO_FILES
        return await self.files.create_document(self._owner() or "", _thread(), title, content_markdown, format)

    async def specs(self) -> list[ToolSpec]:
        """The tools for this owner's turn: the static ones, plus their connected apps' tools when MCP is on."""
        if self.mcp is None:
            return list(TOOL_SPECS.values())
        titles = {name: server.title for name, server in self.mcp.servers.items()}
        connected = [app_tool_spec(tool, titles[tool.server]) for tool in await self._app_tools()]
        return [*TOOL_SPECS.values(), *app_specs(titles), *(spec for spec in connected if spec is not None)]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        # Before getattr: an app tool name is never an attribute, and `__`-names like `__init__` must not become one.
        if "__" in name:
            return await self._app_tool(name, arguments)
        method = getattr(self, name)
        return await method(**arguments)

    async def _app_tools(self) -> list[McpTool]:
        assert self.mcp is not None
        try:
            return await asyncio.wait_for(self.mcp.tools(self._owner() or ""), APP_TOOLS_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("mcp tools timed out owner=%s", self._owner())
            return []

    async def list_apps(self) -> dict[str, Any]:
        if self.mcp is None:
            return _NO_APPS
        status = await self.mcp.status(self._owner() or "")
        return {
            "apps": [{"server": name, "app": server.title, "status": status[name]} for name, server in self.mcp.servers.items()]
        }

    async def connect_app(self, server: str) -> dict[str, Any]:
        if self.mcp is None:
            return _NO_APPS
        config = self.mcp.servers.get(server)
        if config is None:
            return {"error": f"No app named {server}. Call list_apps."}
        owner = self._owner() or ""
        status = (await self.mcp.status(owner))[server]
        url = await self.mcp.connect_url(owner, server)
        if url is None:
            return {"app": config.title, "status": status, "connect_url": None, "note": NO_LINK.get(status, "It has no sign-in link.")}
        return _personal({"app": config.title, "status": status, "connect_url": url})

    async def _app_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Spec 20 §4: a read runs now with the owner's credential; a write only stages a draft."""
        if self.mcp is None:
            return _NO_APPS
        owner = self._owner() or ""
        server, _, tool_name = name.partition("__")
        tool = next((tool for tool in await self._app_tools() if (tool.server, tool.name) == (server, tool_name)), None)
        if tool is None:
            return await self._unavailable(owner, server, name)
        if tool.access == "write":
            return await self._stage_app_action(owner, tool, arguments)
        result = await self.mcp.call(owner, server, tool_name, arguments)
        if isinstance(result, NotConnected):
            return _personal(result.for_model())
        content: Any = result.text[:APP_RESULT_CHARS] if result.text else result.structured
        return {"app": self.mcp.servers[server].title, "tool": tool.title or tool.name, "content": content, "is_error": result.is_error}

    async def _unavailable(self, owner: str, server: str, name: str) -> dict[str, Any]:
        """A tool missing from the owner's own list is never called: its classification is unknown."""
        assert self.mcp is not None
        config = self.mcp.servers.get(server)
        if config is not None:
            status = (await self.mcp.status(owner))[server]
            if status != "connected":
                missing = NotConnected(server, config.title, status, await self.mcp.connect_url(owner, server))
                return _personal(missing.for_model())
        return {"error": f"No app tool {name}. Call list_apps to see what is connected."}

    async def _stage_app_action(self, owner: str, tool: McpTool, arguments: dict[str, Any]) -> StagedDraft:
        """An APP_ACTION draft (Spec 20 §4.1). The executor calls the tool once the owner approves; nothing else does."""
        assert self.mcp is not None
        thread = _thread()
        server = self.mcp.servers[tool.server]
        app, title = server.title, tool.title or tool.name
        field = server.body_field_for(tool.name)
        body_field = field if field and isinstance(arguments.get(field), str) else None
        content = arguments[body_field] if body_field else json.dumps(arguments, indent=2, ensure_ascii=False)
        staged = {
            "action_type": "APP_ACTION",
            "recipient_identifier": tool.server,
            "recipient_name": app,
            "preview_summary": f"{app}: {title}",
            "staged_content": content,
            "metadata": {
                "server": tool.server, "tool": tool.name, "tool_title": title, "arguments": arguments, "body_field": body_field,
            },
        }
        draft_id = await self.repo.create_draft(
            workspace_id=self.workspace_id, user_id=owner, channel_id=thread.channel_id, thread_ts=thread.thread_ts,
            action_type="APP_ACTION", payload=staged,
        )
        return StagedDraft(draft_id, app, app_action_blocks(draft_id, app, title, arguments, body_field, content))


_NO_MEMORY = {"error": "Memory is not available in this context."}
_NO_FILES = {"error": "Files are not available in this context."}
_NO_APPS = {"error": "No apps are configured for Knappy."}


def _personal(view: dict[str, Any]) -> dict[str, Any]:
    """A connect link binds consent to the asking user, so it is shown only in their DM (Spec 20 §3)."""
    thread = current_thread.get()
    if not view.get("connect_url") or (thread is not None and thread.channel_id.startswith("D")):
        return view
    hidden = {key: value for key, value in view.items() if key != "connect_url"}
    return {**hidden, "note": PERSONAL_LINK.format(app=view["app"])}


def _thread() -> SlackThread:
    thread = current_thread.get()
    if thread is None:
        raise RuntimeError("No Slack thread in scope for this tool call")
    return thread
