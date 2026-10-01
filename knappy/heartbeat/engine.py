"""Deterministic scanner plus triage, then optional synthesis and DM."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from knappy.db.repository import SqliteRepository, format_ts
from knappy.heartbeat.triage import ProactiveAlertTriager
from knappy.hitl.blocks import proactive_blocks

PROACTIVE_SYSTEM_PROMPT = """
You are an executive relationship assistant reaching out proactively to the user via Slack.
Be direct, helpful, and concise. Never use fluff or robotic pleasantries.

Instructions:
1. Explain clearly why you are surfacing this now (e.g. deadline approaching in 4 hours, haven't spoken in 30 days).
2. Propose a concrete action draft that the user can execute in one click.
""".strip()

Sender = Callable[..., Awaitable[None]]
Synthesizer = Callable[[dict[str, Any]], Awaitable[str]]


def hours_until(due_date: str) -> float:
    due = datetime.strptime(due_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (due - now).total_seconds() / 3600


def default_synthesis(candidate: dict[str, Any]) -> str:
    name = candidate.get("contact_name") or "someone"
    if candidate.get("commitment"):
        return f"Deadline approaching for {name}: {candidate['commitment']}"
    days = candidate.get("days_since_last_contact")
    return f"You have not spoken with {name} in {days} days."


class HeartbeatEngine:
    def __init__(
        self,
        repo: SqliteRepository,
        triager: ProactiveAlertTriager,
        *,
        workspace_id: str,
        user_id: str,
        sender: Sender | None = None,
        synthesize: Synthesizer | None = None,
    ) -> None:
        self.repo = repo
        self.triager = triager
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.sender = sender
        self.synthesize = synthesize or _async_default
        self.classifier_calls = 0

    async def run_tick(self, *, include_cadence: bool = False, deliver_digest: bool = False) -> dict[str, int]:
        counts = {"scanned": 0, "immediate": 0, "queued": 0, "suppressed": 0, "digests": 0}
        commitments = await self.repo.scan_due_commitments(self.workspace_id)
        candidates = [self._commitment_candidate(row) for row in commitments]
        if include_cadence:
            for row in await self.repo.scan_dormant_contacts(self.workspace_id):
                candidates.append(self._cadence_candidate(row))
        counts["scanned"] = len(candidates)
        if not candidates and not deliver_digest:
            return counts
        queued_contacts = {
            item["contact_id"]
            for item in await self.repo.list_queued_briefings(self.workspace_id)
        }
        for candidate in candidates:
            self.classifier_calls += 1
            decision = await self.triager.triage_candidate(candidate)
            action = decision["action"]
            if action == "DISPATCH_IMMEDIATE_DM":
                await self._dispatch_immediate(candidate)
                counts["immediate"] += 1
            elif action == "QUEUE_MORNING_DIGEST":
                if candidate.get("contact_id") not in queued_contacts:
                    await self.repo.enqueue_briefing(
                        workspace_id=self.workspace_id,
                        user_id=self.user_id,
                        kind=candidate["kind"],
                        summary=candidate.get("commitment") or candidate.get("summary") or candidate["contact_name"],
                        interaction_id=candidate.get("interaction_id"),
                        contact_id=candidate.get("contact_id"),
                    )
                    queued_contacts.add(candidate.get("contact_id"))
                counts["queued"] += 1
            else:
                counts["suppressed"] += 1
            if candidate.get("interaction_id"):
                await self.repo.mark_alerted(candidate["interaction_id"])
        if deliver_digest:
            counts["digests"] = await self.deliver_digest()
        return counts

    async def deliver_digest(self) -> int:
        items = await self.repo.list_queued_briefings(self.workspace_id)
        if not items or self.sender is None:
            for item in items:
                await self.repo.mark_briefing_delivered(item["id"])
            return len(items)
        lines = [f"• {item['summary']}" for item in items]
        await self.sender(
            channel=self.user_id,
            text="Morning briefing",
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Morning briefing*\n" + "\n".join(lines)},
                }
            ],
        )
        for item in items:
            await self.repo.mark_briefing_delivered(item["id"])
        return len(items)

    async def mark_done(self, interaction_id: str) -> None:
        await self.repo.update_interaction_status(interaction_id, "FULFILLED")

    async def snooze(self, interaction_id: str) -> str | None:
        return await self.repo.snooze_interaction(interaction_id, hours=24)

    async def _dispatch_immediate(self, candidate: dict[str, Any]) -> None:
        summary = await self.synthesize(candidate)
        draft_id = await self.repo.create_draft(
            workspace_id=self.workspace_id,
            user_id=self.user_id,
            channel_id=self.user_id,
            action_type="SEND_SLACK_DM",
            payload={
                "action_type": "SEND_SLACK_DM",
                "recipient_identifier": candidate.get("slack_user_id") or candidate["contact_name"],
                "recipient_name": candidate["contact_name"],
                "preview_summary": summary,
                "staged_content": summary,
                "metadata": {},
            },
        )
        blocks = proactive_blocks(
            draft_id,
            candidate.get("interaction_id") or "",
            candidate["contact_name"],
            candidate.get("commitment") or summary,
        )
        if self.sender is not None:
            await self.sender(channel=self.user_id, text=summary, blocks=blocks)

    def _commitment_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "kind": "COMMITMENT",
            "hours_until_due": hours_until(row["due_date"]),
            "summary": row.get("commitment"),
        }

    def _cadence_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        last = datetime.strptime(row["last_interaction_ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - last).days
        return {
            **row,
            "kind": "CADENCE",
            "days_since_last_contact": days,
            "summary": f"No contact with {row['contact_name']} in {days} days",
        }


async def _async_default(candidate: dict[str, Any]) -> str:
    return default_synthesis(candidate)


def digest_timestamp() -> str:
    return format_ts()
