"""Deterministic scanner plus triage, then optional synthesis and DM."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from knappy.db.repository import SqliteRepository, format_ts, utc_now
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


def hours_until(due_date: str, now: datetime | None = None) -> float:
    due = datetime.strptime(due_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return (due - (now or datetime.now(timezone.utc))).total_seconds() / 3600


def default_synthesis(candidate: dict[str, Any]) -> str:
    name = candidate.get("contact_name") or "someone"
    if candidate.get("check_due") and candidate.get("on_no_progress"):
        waiting = f" (waiting on {candidate['waiting_on']})" if candidate.get("waiting_on") else ""
        return f"No progress on {candidate['commitment']}{waiting}. Suggested next step: {candidate['on_no_progress']}"
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
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repo = repo
        self.triager = triager
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.sender = sender
        self.synthesize = synthesize or _async_default
        self.clock = clock
        self.classifier_calls = 0

    async def run_tick(self, *, include_cadence: bool = False, deliver_digest: bool = False) -> dict[str, int]:
        counts = {"scanned": 0, "immediate": 0, "queued": 0, "suppressed": 0, "digests": 0}
        commitments = await self.repo.scan_due_commitments(self.workspace_id, now=self.clock())
        candidates = [self._commitment_candidate(row) for row in commitments]
        if include_cadence:
            for row in await self.repo.scan_dormant_contacts(self.workspace_id):
                candidates.append(self._cadence_candidate(row))
        counts["scanned"] = len(candidates)
        if not candidates and not deliver_digest:
            return counts
        queued_keys = {
            _briefing_key(item)
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
                if _briefing_key(candidate) not in queued_keys:
                    owner = candidate.get("owner_user_id") or self.user_id
                    await self.repo.enqueue_briefing(
                        workspace_id=self.workspace_id,
                        user_id=owner,
                        kind=candidate["kind"],
                        summary=candidate.get("commitment") or candidate.get("summary") or candidate["contact_name"],
                        interaction_id=candidate.get("interaction_id"),
                        contact_id=candidate.get("contact_id"),
                        owner_user_id=owner,
                    )
                    queued_keys.add(_briefing_key(candidate))
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
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            owner = item.get("owner_user_id") or item.get("user_id") or self.user_id
            groups.setdefault(owner, []).append(item)
        for owner, group in groups.items():
            lines = [f"• {item['summary']}" for item in group]
            await self.sender(
                channel=owner,
                text="Morning briefing",
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "*Morning briefing*\n" + "\n".join(lines)},
                    }
                ],
            )
            for item in group:
                await self.repo.mark_briefing_delivered(item["id"])
        return len(items)

    async def mark_done(self, interaction_id: str) -> None:
        await self.repo.update_interaction_status(interaction_id, "FULFILLED")

    async def snooze(self, interaction_id: str) -> str | None:
        return await self.repo.snooze_interaction(interaction_id, hours=24)

    async def _dispatch_immediate(self, candidate: dict[str, Any]) -> None:
        summary = await self.synthesize(candidate)
        owner = candidate.get("owner_user_id") or self.user_id
        contact_name = candidate.get("contact_name")
        draft_id = None
        if contact_name:
            draft_id = await self.repo.create_draft(
                workspace_id=self.workspace_id,
                user_id=owner,
                channel_id=owner,
                action_type="SEND_SLACK_DM",
                payload={
                    "action_type": "SEND_SLACK_DM",
                    "recipient_identifier": candidate.get("slack_user_id") or contact_name,
                    "recipient_name": contact_name,
                    "preview_summary": summary,
                    "staged_content": summary,
                    "metadata": {},
                },
            )
        blocks = proactive_blocks(
            draft_id,
            candidate.get("interaction_id") or "",
            contact_name,
            candidate.get("commitment") or summary,
        )
        if self.sender is not None:
            await self.sender(channel=owner, text=summary, blocks=blocks)

    def _commitment_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        candidate = {**row, "kind": "COMMITMENT", "summary": row.get("commitment"), "check_due": bool(row.get("check_due"))}
        if row.get("due_date"):
            candidate["hours_until_due"] = hours_until(row["due_date"], now)
        if candidate["check_due"]:
            candidate["hours_until_check"] = hours_until(row["next_check_at"], now)
        return candidate

    def _cadence_candidate(self, row: dict[str, Any]) -> dict[str, Any]:
        last = datetime.strptime(row["last_interaction_ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - last).days
        return {
            **row,
            "kind": "CADENCE",
            "days_since_last_contact": days,
            "summary": f"No contact with {row['contact_name']} in {days} days",
        }


def _briefing_key(item: dict[str, Any]) -> str | None:
    """One briefing per contact; commitments without a contact are each their own item."""
    return item.get("contact_id") or item.get("interaction_id")


async def _async_default(candidate: dict[str, Any]) -> str:
    return default_synthesis(candidate)


def digest_timestamp() -> str:
    return format_ts()
