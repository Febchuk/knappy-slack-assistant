"""Test doubles. The regex heuristics that once ran in production live here only."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from knappy.heartbeat.brief import ContactMessage, ProactiveDraft
from knappy.heartbeat.triage import TriageJudgment
from knappy.ingestion.extract import ExtractedInteraction
from knappy.llm.fake import FakeModel
from knappy.llm.types import (
    Message,
    ModelTurn,
    Recency,
    SchemaT,
    Tier,
    ToolCall,
    ToolResult,
    ToolSpec,
    UserMessage,
    WebSearchResult,
)
from knappy.files.service import DocumentDigest
from knappy.memory.types import EpisodeDraft, RecapDraft, ReconcileResult, RepassDraft

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _next_weekday(now: datetime, weekday: int) -> datetime:
    days = (weekday - now.weekday()) % 7
    if days == 0:
        days = 7
    return (now + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)


def resolve_due(text: str, now: datetime) -> str | None:
    lower = text.lower()
    hour = 17
    minute = 0
    clock = re.search(r"at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower)
    if clock:
        hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridian = clock.group(3)
        if meridian == "pm" and hour < 12:
            hour += 12
        if meridian == "am" and hour == 12:
            hour = 0
    if "tomorrow" in lower:
        due = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        return due.strftime("%Y-%m-%dT%H:%M:%S")
    for name, index in WEEKDAYS.items():
        if name in lower:
            due = _next_weekday(now, index).replace(hour=hour, minute=minute)
            return due.strftime("%Y-%m-%dT%H:%M:%S")
    return None


def heuristic_extract(text: str, now: datetime | None = None) -> ExtractedInteraction:
    current = now or datetime.now()
    cleaned = re.sub(r"^note:\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    name_match = re.search(r"(?:with|sync with)\s+([A-Za-z][A-Za-z'-]*)", cleaned, re.IGNORECASE)
    contact_name = name_match.group(1) if name_match else "Unknown"
    company_match = re.search(r"from\s+([^,]+)", cleaned, re.IGNORECASE)
    company = company_match.group(1).strip() if company_match else None
    email_match = re.search(r"[\w.+-]+@[\w.-]+", cleaned)
    commitment = None
    promised = re.search(r"promised to\s+(.+)", cleaned, re.IGNORECASE)
    if promised:
        commitment = promised.group(1).rstrip(".").strip()
    return ExtractedInteraction(
        contact_name=contact_name,
        contact_email=email_match.group(0) if email_match else None,
        company=company,
        summary=cleaned,
        commitment=commitment,
        due_date=resolve_due(cleaned, current),
        importance="HIGH" if commitment else "MEDIUM",
    )


async def heuristic_triage(candidate: dict[str, Any]) -> dict[str, float | str]:
    check = candidate.get("hours_until_check")
    if isinstance(check, (int, float)) and check <= 0:
        return {"interrupt_probability": 0.9, "strategy": "immediate_dm", "strategy_confidence": 0.9, "consequence_score": 2.0}
    hours = candidate.get("hours_until_due")
    if isinstance(hours, (int, float)) and hours <= 4:
        return {"interrupt_probability": 0.9, "strategy": "immediate_dm", "strategy_confidence": 0.9, "consequence_score": 2.0}
    if isinstance(hours, (int, float)):
        return {"interrupt_probability": 0.55, "strategy": "batch_into_morning_digest", "strategy_confidence": 0.8, "consequence_score": 1.0}
    return {"interrupt_probability": 0.2, "strategy": "suppress_low_value", "strategy_confidence": 0.8, "consequence_score": 0.2}


def heuristic_turn(system: str, contents: list[Message]) -> ModelTurn:
    """Keyword stand-in for the model: picks a tool, then answers from what the tools returned."""
    last_user = max(index for index, item in enumerate(contents) if isinstance(item, UserMessage))
    query = contents[last_user].text
    results = [item for item in contents[last_user:] if isinstance(item, ToolResult)]
    if results:
        return ModelTurn(text=" | ".join(describe(result.result) for result in results))
    lower = query.lower()
    if "what do i have" in lower:
        return ModelTurn(text=system.split("Open commitments", 1)[1])
    if any(phrase in lower for phrase in ("said", "say about", "in slack", "slack message")):
        return tool_turn("search_slack_history", {"query": query})
    if "follow up" in lower:
        recipient = "them"
        for token in query.split():
            if token[:1].isupper() and token.lower() not in {"follow", "up", "with"}:
                recipient = token.strip(".,!?")
                break
        return tool_turn(
            "stage_outbound_action",
            {
                "action_type": "SEND_SLACK_DM",
                "recipient": recipient,
                "summary": f"Follow up with {recipient}",
                "staged_content": f"Hi {recipient}, following up as we discussed.",
                "recipient_identifier": recipient,
            },
        )
    if "promise" in lower or "commitment" in lower:
        return tool_turn("search_commitments", {"query": query})
    return ModelTurn(text=f"Model answer to: {query}")


def describe(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(describe(item) for item in value) or "nothing found"
    if isinstance(value, dict):
        if "error" in value:
            return f"error: {value['error']}"
        if "draft_id" in value:
            return str(value["status"])
        if value.get("commitment"):
            return f"{value.get('contact_name') or 'you'}: {value['commitment']}"
        return str(value.get("text") or value.get("name") or value)
    return str(value)


def tool_turn(name: str, args: dict[str, Any]) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(id=f"call_{name}", name=name, args=args)])


def tool_results(contents: list[Message]) -> list[ToolResult]:
    return [item for item in contents if isinstance(item, ToolResult)]


class HeuristicModel:
    """Deterministic stand-in for Gemini with the pre-model regex behavior."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now

    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        contents: list[Message],
        tools: list[ToolSpec] | None = None,
        timeout_s: float | None = None,
    ) -> ModelTurn:
        return heuristic_turn(system, contents)

    async def generate_structured(
        self, *, tier: Tier, system: str, text: str, schema: type[SchemaT], timeout_s: float | None = None
    ) -> SchemaT:
        result: BaseModel
        if schema is ExtractedInteraction:
            result = heuristic_extract(text, self.now)
        elif schema is TriageJudgment:
            result = TriageJudgment.model_validate(await heuristic_triage(json.loads(text)))
        else:
            raise AssertionError(f"HeuristicModel has no answer for {schema.__name__}")
        return schema.model_validate(result.model_dump())

    async def search(self, query: str, recency: Recency = "any") -> WebSearchResult:
        raise AssertionError("HeuristicModel cannot search the web")


class FakeSdk:
    """Stands in for google-genai's client: returns or raises the scripted outcomes in order."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls: list[dict] = []
        self.aio = self
        self.models = self

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSlack:
    """Slack Web API double. Posts return a ts so placeholders can be updated."""

    def __init__(
        self,
        *,
        messages: list[dict] | None = None,
        tz: str = "America/New_York",
        ephemeral_error: BaseException | None = None,
        members: list[dict] | None = None,
        user_id: str = "UBOT",
    ) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.ephemerals: list[dict] = []
        self.reactions: list[tuple[str, dict]] = []
        self.messages = messages or []
        self.tz = tz
        self.ephemeral_error = ephemeral_error
        self.users_info_calls = 0
        self.modals: list[dict] = []
        self.uploads: list[dict] = []
        # The workspace directory: users.list members, each optionally with profile.email.
        self.members = members or []
        self.directory_calls: list[str] = []
        # Who this token is (auth.test). The bot by default; the owner for a user-token client.
        self.user_id = user_id
        # Channels this token can open with conversations.info. For the bot: only its own DMs.
        self.visible: set[str] = set()
        # The workspace as this token sees it: users.conversations, and each conversation's messages.
        self.conversations: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        self.api_calls: list[str] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"100.{len(self.posts)}"}

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        if self.ephemeral_error is not None:
            raise self.ephemeral_error
        self.ephemerals.append(kwargs)

    async def reactions_add(self, **kwargs):
        self.reactions.append(("add", kwargs))

    async def reactions_remove(self, **kwargs):
        self.reactions.append(("remove", kwargs))

    async def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)
        file_id = f"FUP{len(self.uploads)}"
        return {"ok": True, "files": [{"id": file_id, "permalink": f"https://slack.test/files/{file_id}"}]}

    async def conversations_open(self, *, users):
        return {"ok": True, "channel": {"id": f"D{users}"}}

    async def views_open(self, **kwargs):
        self.modals.append(kwargs)

    async def users_info(self, *, user):
        self.users_info_calls += 1
        known = next((member for member in self.members if member["id"] == user), {})
        return {"ok": True, "user": {**known, "id": user, "tz": self.tz}}

    async def users_list(self, *, limit=200, cursor=None):
        self.directory_calls.append("users.list")
        return {"ok": True, "members": self.members, "response_metadata": {"next_cursor": ""}}

    async def users_lookupByEmail(self, *, email):
        self.directory_calls.append(f"users.lookupByEmail {email}")
        for member in self.members:
            if (member.get("profile") or {}).get("email") == email:
                return {"ok": True, "user": member}
        raise RuntimeError("users_not_found")

    async def auth_test(self):
        return {"ok": True, "user_id": self.user_id, "team_id": "T_TEST"}

    async def conversations_history(
        self, *, channel, limit=20, oldest=None, latest=None, inclusive=False, cursor=None,
    ):
        self.api_calls.append(f"conversations.history {channel}")
        if channel not in self.history:
            return {"messages": self.messages}
        top = []
        for message in self.history[channel]:
            if message.get("thread_ts") not in (None, message["ts"]):
                continue
            ts = float(message["ts"])
            if oldest is not None:
                after = float(oldest)
                if ts < after or (not inclusive and ts == after):
                    continue
            elif ts <= 0:
                continue
            if latest is not None:
                before = float(latest)
                if ts > before or (not inclusive and ts == before):
                    continue
            top.append(message)
        top.sort(key=lambda message: -float(message["ts"]))
        start = int(cursor) if cursor else 0
        page = top[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(top) else ""
        return {"messages": page, "response_metadata": {"next_cursor": next_cursor}}

    async def conversations_replies(self, *, channel, ts, oldest=None, limit=200):
        self.api_calls.append(f"conversations.replies {channel} {ts}")
        thread = [m for m in self.history.get(channel, []) if m.get("thread_ts") == ts or m["ts"] == ts]
        return {"messages": sorted(thread, key=lambda m: float(m["ts"]))}

    async def conversations_info(self, *, channel):
        self.api_calls.append(f"conversations.info {channel}")
        known = next((c for c in self.conversations if c["id"] == channel), None)
        if channel not in self.visible and known is None:
            raise RuntimeError("channel_not_found")
        return {"ok": True, "channel": known or {"id": channel, "is_im": channel.startswith("D")}}

    async def users_conversations(self, **kwargs):
        self.api_calls.append("users.conversations")
        return {"channels": list(self.conversations)}

    async def chat_getPermalink(self, *, channel, message_ts):
        return {"ok": True, "permalink": f"https://slack.test/archives/{channel}/p{message_ts.replace('.', '')}"}

    def shown(self, ts: str) -> dict:
        """The message at ts as the user now sees it: the post, overlaid by its latest update."""
        post = next(post for index, post in enumerate(self.posts, 1) if f"100.{index}" == ts)
        latest = [update for update in self.updates if update["ts"] == ts]
        return {**post, **latest[-1]} if latest else post


def pdf_with(*pages: str) -> bytes:
    """A minimal PDF with one page per argument. An empty string makes a page with no text layer, like a scan."""
    count = len(pages)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        kids.append(len(objects) + 1)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            % len(objects)
        )
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % kid for kid in kids), count)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def dm(text: str, ts: str, *, user: str = "U1", channel: str = "D1", thread_ts: str | None = None) -> dict:
    event = {"text": text, "channel": channel, "channel_type": "im", "user": user, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def mention(text: str, ts: str, *, user: str = "U1", channel: str = "C1") -> dict:
    return {"type": "app_mention", "text": f"<@UBOT> {text}", "channel": channel, "user": user, "ts": ts}


def only(runtime):
    """register_actions' lookup for a test that serves one workspace."""

    async def runtime_for(body):
        return runtime

    return runtime_for


class FakeApp:
    """Bolt app double: collects the action and view handlers register_actions installs."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., Awaitable[None]]] = {}

    def action(self, name: str):
        def decorate(fn):
            self.handlers[name] = fn
            return fn

        return decorate

    view = action


class FakeClock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


ToolPlan = tuple[str, dict[str, Any] | Callable[[Any], dict[str, Any]]]


def agent(script: dict[str, ToolPlan | list[ToolPlan]] | None = None):
    """Agent double for FakeModel: calls the scripted tools for a message prefix, then reports the tool results as JSON.

    A plan's args may be a function of the request, to read ids out of the system prompt as the model would.
    """

    async def respond(request):
        if request.tier == "light":
            attached = [item for message in request.contents for item in getattr(message, "attachments", ())]
            return ModelTurn(text="Read: " + ", ".join(item.mime_type for item in attached))
        results = tool_results(request.contents)
        if results:
            return ModelTurn(text=json.dumps([result.result for result in results], default=str))
        text = request.contents[-1].text.lower()
        for prefix, plan in (script or {}).items():
            if text.startswith(prefix):
                plans = plan if isinstance(plan, list) else [plan]
                return ModelTurn(tool_calls=[
                    ToolCall(id=f"call_{index}_{name}", name=name, args=args(request) if callable(args) else args)
                    for index, (name, args) in enumerate(plans)
                ])
        return ModelTurn(text="ok")

    return respond


Reconcile = Callable[[dict[str, Any]], "ReconcileResult | dict[str, Any] | Awaitable[ReconcileResult | dict[str, Any]]"]


Observe = Callable[[str, str, str, dict[str, Any]], "dict[str, Any] | None"]
_LINE = re.compile(r"^(?P<sender>.+?)(?P<owner> \(the user\))?(?: \[reply in thread [\d.]+\])?: (?P<text>.*) \((?P<ts>[\d.]+)\)$")


def observer(rules: dict[str, dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]]) -> Observe:
    """A relevance pass by keyword: the first rule whose key appears in a message's text gives its observation.

    A rule may be a function of the pass's context (open_commitments, workspaces, the user's names).
    """

    def observe(sender: str, text: str, ts: str, context: dict[str, Any]) -> dict[str, Any] | None:
        for key, fields in rules.items():
            if key in text.lower():
                return {"ts": ts, "relevance": 0.9, "urgency": "low", **(fields(context) if callable(fields) else fields)}
        return None

    return observe


def relevance_reply(observe: Observe | None, text: str) -> dict[str, Any]:
    from knappy.awareness.relevance import Relevance

    found = []
    head, lines = text.split("\n\nMessages:\n", 1)
    context = json.loads(head)
    for line in lines.splitlines():
        match = _LINE.match(line)
        assert match, f"unparsed message line {line!r}"
        result = observe(match["sender"], match["text"], match["ts"], context) if observe else None
        if result is not None:
            found.append(result)
    return Relevance(observations=found).model_dump()


def memory_structured(reconcile: Reconcile | None = None, observe: Observe | None = None):
    """Structured responder for FakeModel: a scripted reconciler plus deterministic memory drafts.

    `reconcile` receives the reconciler's JSON payload (records, open_commitments, turns).
    Episodes list their inputs, recaps join the turns, and re-passes drop lines that mention a forgotten word.
    """

    async def respond(schema: type[BaseModel], system: str, text: str) -> Any:
        if schema.__name__ == "Relevance":
            return relevance_reply(observe, text)
        if schema is ReconcileResult:
            if reconcile is None:
                return ReconcileResult()
            result = reconcile(json.loads(text))
            return await result if inspect.isawaitable(result) else result
        if schema is EpisodeDraft:
            if text.startswith("{"):
                return EpisodeDraft(body="\n".join(f"- {event['summary']}" for event in json.loads(text)["events"]))
            return EpisodeDraft(body="\n".join(line for line in text.splitlines() if line.startswith("- ")))
        if schema is RecapDraft:
            return RecapDraft(body="\n".join(f"- {line}" for line in text.splitlines() if line.startswith(("user:", "assistant:"))))
        if schema is RepassDraft:
            data = json.loads(text)
            words = {word for line in data["forget"] for word in re.findall(r"[a-z]{5,}", line.lower())}
            kept = [line for line in data["record"]["body"].splitlines() if not words & set(re.findall(r"[a-z]{5,}", line.lower()))]
            return RepassDraft(body="\n".join(kept))
        if schema is TriageJudgment:
            return TriageJudgment.model_validate(await heuristic_triage(json.loads(text)))
        if schema is ProactiveDraft:
            return proactive_draft(system, json.loads(text))
        if schema is DocumentDigest:
            name, _, body = text.partition("\n\n")
            return DocumentDigest(summary=" ".join(body.split())[:300], key_terms=[name.removeprefix("File name: ")])
        raise AssertionError(f"memory_structured has no answer for {schema.__name__}")

    return respond


def proactive_draft(system: str, payload: dict[str, Any]) -> ProactiveDraft:
    """The brief or nudge lists each item's summary; each recipient gets a short note that reads as the user."""
    items = payload["items"]
    lead = "*Brief*\n" if "morning brief" in system else ""
    return ProactiveDraft(
        text=lead + "\n".join(f"• {item['summary']}" for item in items),
        messages=[
            ContactMessage(item=item["item"], text=f"Hi {item['recipient']}, quick update on {item.get('commitment') or 'things'}.")
            for item in items if item.get("recipient")
        ],
    )


def event(kind: str, summary: str, turns: list[str], score: float = 0.9, occurred_at: str = "2026-10-03T12:00:00Z") -> dict:
    return {"kind": kind, "summary": summary, "occurred_at": occurred_at, "source_turn_ids": turns, "admission_score": score}


def op(kind: str, *, events: list[int], score: float = 0.9, reason: str = "test", **fields: Any) -> dict:
    return {"op": kind, "from_events": events, "admission_score": score, "reason": reason, **fields}


def user_turns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [turn for turn in payload["turns"] if turn["role"] == "user"]


def member(user_id: str, real_name: str, *, display_name: str = "", email: str | None = None) -> dict:
    """A users.list member as Slack returns it."""
    profile = {"real_name": real_name, "display_name": display_name, **({"email": email} if email else {})}
    return {"id": user_id, "name": real_name.lower().replace(" ", "."), "real_name": real_name, "profile": profile}


def knappy_runtime(repo, slack: FakeSlack, clock: FakeClock, model=None, **kwargs):
    """A runtime wired to fake Slack the way open_runtime wires the real one: replies, proactive DMs, and approvals."""
    from knappy.runtime import KnappyRuntime
    from knappy.slack.egress import build_say
    from knappy.slack.executor import SlackActionExecutor

    say = build_say(slack)
    return KnappyRuntime(
        repo, workspace_id="T_TEST", model=model or FakeModel(agent(), structured=memory_structured()), say=say,
        sender=say, executor=SlackActionExecutor(slack), slack=slack, clock=clock, **kwargs,
    )


async def seed_commitment(
    repo,
    text: str,
    *,
    due: datetime | None,
    owner: str = "U1",
    contact: str | None = "Alex",
    slack_user_id: str | None = "UALEX",
    email: str | None = None,
) -> str:
    """A pending commitment, as add_commitment or the reconciler would store it."""
    contact_id = None
    if contact:
        contact_id = await repo.upsert_contact(
            "T_TEST", contact, slack_user_id=slack_user_id, email=email, owner_user_id=owner,
            last_interaction_ts=(due or datetime(2026, 10, 1)).strftime("%Y-%m-%d %H:%M:%S"),
        )
    return await repo.insert_interaction(
        workspace_id="T_TEST", contact_id=contact_id, source_type="DIRECT_DM", channel_id=f"D{owner}",
        raw_text=text, summary=text, commitment=text,
        due_date=due.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if due else None, owner_user_id=owner,
    )
