"""Typed model contracts and prompts for the memory compiler (Spec 13 §3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RecordType = Literal[
    "person", "org", "fact", "preference", "decision", "workstream", "episode_daily", "episode_weekly", "document"
]
SavableType = Literal["person", "org", "fact", "preference", "decision", "workstream"]
EventKind = Literal[
    "learned", "changed", "commitment_made", "commitment_progress", "commitment_done", "decision", "document_added",
    "forgotten",
]
OpKind = Literal[
    "create", "update", "supersede", "expire", "link", "commitment_add", "commitment_progress", "commitment_done"
]


class LedgerEvent(BaseModel):
    kind: EventKind
    summary: str = Field(..., description="One line, e.g. 'Moved from Google to Stripe'")
    occurred_at: str = Field(..., description="ISO 8601: when it happened, not now")
    source_turn_ids: list[str] = Field(..., description="Ids of the input turns where this was said. At least one.")
    admission_score: float = Field(..., ge=0, le=1, description="Expected value for future decisions, 0-1")


class MemoryOp(BaseModel):
    op: OpKind
    record_id: str | None = Field(default=None, description="Existing record or commitment id to target")
    type: SavableType | None = None
    title: str | None = None
    aliases: list[str] = Field(default_factory=list)
    body: str | None = Field(default=None, description="Full new markdown body for create, update, supersede")
    expires_at: str | None = Field(default=None, description="ISO 8601, for time-bound facts")
    due: str | None = Field(default=None, description="ISO 8601 with offset, when a commitment is due")
    person: str | None = Field(default=None, description="Who a commitment is for or about")
    next_check_at: str | None = Field(
        default=None, description="ISO 8601 with offset. Required for conditional follow-through ('if X hasn't happened by Thursday')"
    )
    on_no_progress: str | None = Field(
        default=None, description="Required with next_check_at: what to do for the user if nothing moved by then"
    )
    waiting_on: str | None = Field(default=None, description="Who or what the commitment is blocked on")
    from_events: list[int] = Field(..., description="Indexes into events this op was compiled from. Required.")
    from_records: list[str] = Field(
        default_factory=list, description="Ids of existing records this op's content depends on, e.g. a plan that follows a preference"
    )
    admission_score: float = Field(..., ge=0, le=1)
    reason: str = Field(..., description="One line, logged")


class ReconcileResult(BaseModel):
    events: list[LedgerEvent] = Field(default_factory=list)
    ops: list[MemoryOp] = Field(default_factory=list)
    discarded: list[str] = Field(default_factory=list, description="One line per thing judged not worth keeping")


class EpisodeDraft(BaseModel):
    body: str = Field(..., description="Markdown, at most 300 words")


class RecapDraft(BaseModel):
    body: str = Field(..., description="Markdown, at most 250 words")


class RepassDraft(BaseModel):
    body: str = Field(..., description="The record body with the forgotten content removed. Empty if nothing is left.")


RECONCILE_PROMPT = """You compile a personal assistant's long-term memory about one user from recent conversation turns. Memory is a compiler, not an attic: admit only what will change a future decision.

Answer three questions for the batch:
1. What here changes our understanding of the user: facts about them, preferences, people, organizations, decisions, ongoing workstreams?
2. What active task or commitment was made, moved forward, or finished?
3. What can be safely discarded?

Return:
- events: the ledger of what happened, one line each. source_turn_ids lists the ids of the input turns where you saw it (at least one). occurred_at is when it happened, not now. admission_score (0-1) is the expected value of remembering it for future decisions.
- ops: changes to memory records and commitments. Every op lists from_events, indexes into your events list. An op without events is rejected. When an op's content also relies on existing records (a meal plan that follows a diet preference), list their ids in from_records.
- discarded: one short line per thing you judged not worth keeping.

Rules:
- Admit what will change a future decision, not what was merely said. Small talk, greetings, thanks, acknowledgements, and one-off logistics get no events; list them in discarded.
- Score honestly: 0.8-1.0 for durable facts, preferences, decisions, and commitments; 0.4-0.7 for useful context; below 0.4 for trivia.
- Generalize repeated examples into preferences.
- Prefer changing an existing record (an id from records) to creating a near-duplicate. supersede when a fact changed and the old version is no longer true: body replaces the old body entirely. update to add to a record: body is the full new body, including what is still true.
- create needs type, title, a markdown body with facts as "- " bullets, and aliases a person would plausibly search for (synonyms, names, companies, places).
- Types: person (someone the user knows), org (a company or group), fact (stable facts about the user: job, home, family, health, dates), preference (likes, dislikes, habits, how they want things done), decision (a choice the user made), workstream (an ongoing project or goal the user is working on). The user's employer is a fact, not a workstream.
- Time-bound facts ("exam on the 12th") get expires_at.
- Never store secrets: passwords, API keys, tokens, card numbers. Write a generic note instead, such as "has an API key for the billing service".
- commitment_add when the user will do something or asked to be reminded: title is an imperative ("Send Priya the deck"), person is who it is for, due (ISO 8601 with offset, resolved from today and user_timezone) when known. Skip commitments already in open_commitments. commitment_progress and commitment_done target an id from open_commitments.
- Conditional follow-through is a commitment with a scratchpad. "If Dana hasn't confirmed the venue by Friday, remind me to call her" becomes commitment_add with title "Call Dana about the venue", person "Dana", waiting_on "Dana", next_check_at Friday at 17:00 in the user's timezone, and on_no_progress "Remind the user to call Dana and offer to draft a message asking her to confirm the venue". A commitment_add that depends on someone else or on a deadline passing must set next_check_at and on_no_progress, never leave them null: "if he hasn't replied by Thursday", "unless she confirms by Friday", "check back next week". Set waiting_on whenever it is blocked on someone.
- Preferences about how the user wants you to write get the alias "communication-style". Preferences about what you may do without asking get the alias "autonomy".
- Assistant turns are context. Never record something only the assistant said.
- Turns in conversations starting with "awareness:" are not the user speaking. They are third-person observations from the user's Slack workspace (a decision, a deadline change, a blocker, useful context). Use them to update workstream, person, org, and decision records. Never make commitments from them; those are handled already.
- A turn with already_saved was saved explicitly into those records. Do not create them again; only admit anything else the turn says."""

DAILY_EPISODE_PROMPT = """Write the user's daily episode for a personal assistant's memory from the ledger events below: what happened, decisions made, and open loops. Use short "- " bullets, at most 300 words. Do not invent anything that is not in the input."""

WEEKLY_EPISODE_PROMPT = """Write the user's weekly episode for a personal assistant's memory from the daily episodes below: the themes of the week, decisions, and what is still open. Use short "- " bullets, at most 300 words. Do not invent anything that is not in the input."""

RECAP_PROMPT = """Summarize the earlier part of a conversation between a user and their assistant so the assistant can continue it. Keep anchors the conversation depends on: open loops, names, identifiers, numbers, dates, and decisions. Fold in the previous recap if there is one. At most 250 words, "- " bullets."""

REPASS_PROMPT = """The user asked their assistant to forget some information. Rewrite the memory record below so it no longer contains, implies, or depends on anything in "forget". Keep everything else unchanged. Return an empty body if nothing meaningful remains."""
