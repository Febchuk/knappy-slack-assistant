"""Model-backed memory work: the reconciler, nightly consolidation, forget, recaps, and rebuild (Spec 13 §3-4).

Model calls always happen outside a database transaction; each batch of writes is applied atomically.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from knappy.agent.prompt import MemoryContext
from knappy.db.repository import format_ts
from knappy.ingestion.embed import generate_embedding
from knappy.llm.types import Model
from knappy.memory.store import (
    LoggedTurn,
    MemoryStore,
    LINK,
    parse_ts,
    redact,
    slugify,
    summarize_body,
)
from knappy.memory.types import (
    DAILY_EPISODE_PROMPT,
    RECAP_PROMPT,
    RECONCILE_PROMPT,
    REPASS_PROMPT,
    WEEKLY_EPISODE_PROMPT,
    EpisodeDraft,
    LedgerEvent,
    MemoryOp,
    RecapDraft,
    ReconcileResult,
    RepassDraft,
    SavableType,
)

logger = logging.getLogger("knappy")

FORGETTABLE = [*get_args(SavableType), "document"]
# Reconcile batches and nightly episodes run off the reply path and carry large payloads.
BACKGROUND_TIMEOUT_S = 90.0
_RECORD_ID = re.compile(r"^[a-z_]+:[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class MemoryConfig:
    admission_threshold: float = 0.4
    raw_retention_days: int = 90
    idle: timedelta = timedelta(minutes=20)
    nightly_hour: int = 3
    batch_turns: int = 30


@dataclass
class ReconcileReport:
    turns: int = 0
    events: int = 0
    ops: int = 0
    dropped: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)

    def merge(self, other: ReconcileReport) -> None:
        self.turns += other.turns
        self.events += other.events
        self.ops += other.ops
        self.dropped += other.dropped
        self.rejected += other.rejected
        self.discarded += other.discarded


@dataclass(frozen=True)
class Admitted:
    events: dict[int, LedgerEvent]
    ops: list[tuple[MemoryOp, list[int]]]
    dropped: list[str]


class OpRejected(Exception):
    pass


def admit(result: ReconcileResult, turn_ids: set[str], threshold: float) -> Admitted:
    """The admission gate (Spec 13 §3.3 step 4). Provenance is required for every event and op."""
    events: dict[int, LedgerEvent] = {}
    dropped: list[str] = []
    for index, event in enumerate(result.events):
        sources = [turn_id for turn_id in event.source_turn_ids if turn_id in turn_ids]
        if event.admission_score < threshold:
            dropped.append(f"event {index} below threshold ({event.admission_score:.2f}): {event.summary}")
        elif not sources:
            dropped.append(f"event {index} has no source turn from this batch: {event.summary}")
        else:
            events[index] = event.model_copy(update={"source_turn_ids": sources})
    ops: list[tuple[MemoryOp, list[int]]] = []
    for op in result.ops:
        label = f"{op.op} {op.record_id or op.title or ''}".strip()
        cited = [index for index in dict.fromkeys(op.from_events) if index in events]
        if op.admission_score < threshold:
            dropped.append(f"op {label} below threshold ({op.admission_score:.2f}): {op.reason}")
        elif not op.from_events:
            dropped.append(f"op {label} has no provenance: {op.reason}")
        elif not cited:
            dropped.append(f"op {label} cites only dropped or unknown events: {op.reason}")
        else:
            ops.append((op, cited))
    return Admitted(events, ops, dropped)


class MemoryEngine:
    def __init__(self, store: MemoryStore, model: Model, config: MemoryConfig | None = None) -> None:
        self.store = store
        self.model = model
        self.config = config or MemoryConfig()
        # Owner -> (consecutive failures, earliest retry). Keeps a failing model from being called every tick.
        self._backoff: dict[str, tuple[int, datetime]] = {}

    @property
    def _tx(self) -> Any:
        return self.store.repo.transaction()

    async def load(self, owner: str, conversation_key: str) -> MemoryContext:
        view = await self.store.conversation_view(owner, conversation_key)
        recap = view.recap
        if view.needs_recap:
            try:
                recap = await self._write_recap(owner, conversation_key, recap, view.uncovered)
            except Exception:
                logger.exception("memory recap failed owner=%s key=%s", owner, conversation_key)
        profile = await self.store.profile(owner)
        workstreams = await self.store.active_records(owner, ["workstream"])
        return MemoryContext(
            profile=(profile or {}).get("body") or None,
            recap=recap,
            workstreams=tuple({"id": row["id"], "title": row["title"]} for row in workstreams[:15]),
        )

    async def _write_recap(self, owner: str, key: str, previous: str | None, turns: list[LoggedTurn]) -> str:
        lines = [f"{turn.role}: {turn.text[:1500]}" for turn in turns[-200:]]
        text = (f"Previous recap:\n{previous}\n\n" if previous else "") + "Turns:\n" + "\n".join(lines)
        draft = await self.model.generate_structured(tier="light", system=RECAP_PROMPT, text=text, schema=RecapDraft)
        await self.store.save_recap(owner, key, draft.body, turns[-1].id)
        return redact(draft.body)

    async def remember(
        self, owner: str, text: str, type: str = "fact", about: str | None = None, source_turn: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or self.store.clock()
        body = "\n".join(f"- {line.strip().lstrip('-•* ')}" for line in text.strip().splitlines() if line.strip())
        sources = [("turn", source_turn)] if source_turn else []
        title = about[:1].upper() + about[1:] if about and not _RECORD_ID.match(about) else _title(text)
        async with self._tx:
            target = None
            # An existing id in `about`, or the same subject remembered again, supersedes that record.
            for candidate in (about, f"{type}:{slugify(title)}"):
                record = await self.store.get_record(owner, candidate) if candidate else None
                if record is not None and record["status"] == "ACTIVE":
                    target = record
                    break
            kind = "changed" if target is not None else "learned"
            event_id = await self.store.add_event(
                owner, kind=kind, summary=_title(text, 200), occurred_at=now, score=1.0, now=now, sources=sources
            )
            if target is not None:
                record_id = await self.store.revise_record(
                    owner, target["id"], body=body, now=now, sources=[("event", event_id)], keep_sources=False
                )
            else:
                record_id = await self.store.create_record(
                    owner, record_id=await self.store.free_id(owner, type, title), type=type, title=title, body=body, aliases=[about] if about else [],
                    source="remember", now=now, sources=[("event", event_id)],
                )
            await self.store.rebuild_profile(owner, now)
        record = await self.store.get_record(owner, record_id)
        return {"id": record_id, "type": record["type"], "title": record["title"], "saved": True}

    async def forget(
        self, owner: str, query_or_id: str, source_turn: str | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or self.store.clock()
        targets = await self._forget_targets(owner, query_or_id)
        if not targets:
            return {"forgotten": [], "note": f"Nothing in memory matched {query_or_id!r}."}
        titles = {
            record_id: (await self.store.get_record(owner, record_id))["title"] + (" (earlier version)" if "@" in record_id else "")
            for record_id in targets
        }
        async with self._tx:
            cascade = await self.store.cascade_forget(owner, targets, now)
            await self.store.rebuild_profile(owner, now)
            await self.store.drop_recaps(owner)
        drafts: dict[str, str] = {}
        for record_id, removed in cascade.repass.items():
            record = await self.store.get_record(owner, record_id)
            try:
                draft = await self.model.generate_structured(
                    tier="light",
                    system=REPASS_PROMPT,
                    text=json.dumps({"record": {"title": record["title"], "body": record["body"]}, "forget": removed}),
                    schema=RepassDraft,
                )
                drafts[record_id] = draft.body.strip()
            except Exception:
                logger.exception("memory re-pass failed owner=%s record=%s; forgetting it instead", owner, record_id)
                drafts[record_id] = ""
        derived_forgotten = [record_id for record_id in cascade.forgotten if record_id not in targets]
        updated: list[str] = []
        async with self._tx:
            for record_id, body in drafts.items():
                if body:
                    await self.store.revise_record(
                        owner, record_id, body=body, now=now, sources=cascade.surviving[record_id],
                        keep_sources=False, old_status="FORGOTTEN",
                    )
                    updated.append(record_id)
                else:
                    versions = [version["id"] for version in await self.store.versions(owner, record_id)]
                    await self.store.set_status(owner, versions, "FORGOTTEN", now)
                    derived_forgotten.append(record_id)
            count = len(targets) + len(derived_forgotten)
            await self.store.add_event(
                owner, kind="forgotten", summary=f"Forgot {count} record(s) at the user's request", occurred_at=now,
                score=1.0, now=now, sources=[("turn", source_turn)] if source_turn else [],
            )
            await self.store.rebuild_profile(owner, now)
        logger.info(
            "memory forget owner=%s records=%d derived_forgotten=%d repassed=%d retracted=%d",
            owner, len(targets), len(derived_forgotten), len(updated), len(cascade.retracted),
        )
        return {
            "forgotten": [titles[record_id] for record_id in targets],
            "also_forgotten": derived_forgotten,
            "also_updated": updated,
        }

    async def _forget_targets(self, owner: str, query_or_id: str) -> list[str]:
        """Matching active records, plus superseded versions that still say it ("forget that I used to eat meat")."""
        record = await self.store.get_record(owner, query_or_id.strip())
        if record is not None and record["status"] in ("ACTIVE", "SUPERSEDED"):
            return [record["id"]]
        hits = await self.store.search(owner, query_or_id, types=FORGETTABLE, limit=5)
        active: list[str] = []
        if hits and "score" in hits[0]:
            top = hits[0]["score"]
            active = [hit["id"] for hit in hits if hit["score"] >= 0.6 * top][:3]
        past = await self.store.superseded_matches(owner, query_or_id, types=FORGETTABLE)
        return active + [version for version in past if version.split("@", 1)[0] not in active]

    async def read(self, owner: str, record_id: str, history: bool = False) -> dict[str, Any]:
        record = await self.store.get_record(owner, record_id)
        if record is None:
            return {"error": f"No memory record with id {record_id}"}
        links = []
        for link in json.loads(record["links"] or "[]"):
            linked = await self.store.get_record(owner, link)
            if linked is not None:
                links.append({"id": link, "title": linked["title"], "status": linked["status"]})
        result = {
            key: record[key] for key in ("id", "type", "title", "aliases", "body", "status", "updated_at", "expires_at")
        }
        result["links"] = links
        result["sources"] = await self._sources(owner, record_id)
        if history:
            result["history"] = [
                {
                    "id": version["id"], "title": version["title"], "body": version["body"],
                    "valid_from": version["valid_from"], "status": version["status"],
                    "sources": await self._sources(owner, version["id"]),
                }
                for version in (await self.store.versions(owner, record_id))[1:]
                if version["status"] != "FORGOTTEN"
            ]
        return result

    async def _sources(self, owner: str, record_id: str) -> list[dict[str, Any]]:
        events = [
            {"said": event["summary"], "kind": event["kind"], "when": str(event["occurred_at"])[:16] + " UTC"}
            for event in await self.store.source_events(owner, record_id)
            if event["status"] == "ACTIVE"
        ]
        others = [
            {"from": kind, "id": source_id}
            for kind, source_id in await self.store.sources(owner, "record", record_id)
            if kind in ("migration", "document", "record")
        ]
        return events + others

    async def reconcile(self, owner: str, now: datetime | None = None, *, everything: bool = False) -> ReconcileReport:
        now = now or self.store.clock()
        report = ReconcileReport()
        failures, retry_at = self._backoff.get(owner, (0, now))
        if now < retry_at:
            return report
        if everything:
            pending = await self.store.turns(owner, upto=now, unreconciled=True)
        else:
            keys = set(await self.store.idle_keys(owner, now, self.config.idle))
            if not keys:
                return report
            pending = [turn for turn in await self.store.turns(owner, upto=now, unreconciled=True) if turn.key in keys]
        size = self.config.batch_turns
        for start in range(0, len(pending), size):
            try:
                report.merge(await self._reconcile_batch(owner, pending[start : start + size], now))
            except Exception:
                delay = timedelta(minutes=min(2 ** (failures + 1), 60))
                self._backoff[owner] = (failures + 1, now + delay)
                logger.exception("memory reconcile failed owner=%s; turns stay unreconciled, retry in %s", owner, delay)
                return report
        self._backoff.pop(owner, None)
        return report

    async def _reconcile_batch(self, owner: str, batch: list[LoggedTurn], now: datetime) -> ReconcileReport:
        retracted = await self.store.retracted_turn_ids(owner, with_forget_requests=True)
        visible = [turn for turn in batch if turn.role in ("user", "assistant") and turn.id not in retracted]
        report = ReconcileReport(turns=len(batch))
        if not any(turn.role == "user" for turn in visible):
            async with self._tx:
                await self.store.mark_reconciled([turn.id for turn in batch], now)
            return report
        zone = _zone(await self.store.profile(owner))
        saved = await self.store.remembered(owner, [turn.id for turn in visible])
        payload = {
            "now": now.isoformat(),
            "today": now.astimezone(zone).strftime("%A %Y-%m-%d"),
            "user_timezone": str(zone),
            "records": await self.store.record_index(owner),
            "open_commitments": await self.store.open_commitments(owner),
            "turns": [
                {
                    "id": turn.id, "at": turn.at.isoformat(), "role": turn.role, "conversation": turn.key,
                    "text": turn.text[:2000], **({"already_saved": saved[turn.id]} if turn.id in saved else {}),
                }
                for turn in visible
            ],
        }
        result = await self.model.generate_structured(
            tier="light", system=RECONCILE_PROMPT, text=json.dumps(payload, default=str), schema=ReconcileResult,
            timeout_s=BACKGROUND_TIMEOUT_S,
        )
        gated = admit(result, {turn.id for turn in visible}, self.config.admission_threshold)
        report.dropped, report.discarded = gated.dropped, list(result.discarded)
        turns = {turn.id: turn for turn in visible}
        async with self._tx:
            event_ids: dict[int, str] = {}
            for index, event in gated.events.items():
                occurred = parse_ts(event.occurred_at) or max(turns[turn_id].at for turn_id in event.source_turn_ids)
                event_ids[index] = await self.store.add_event(
                    owner, kind=event.kind, summary=event.summary, occurred_at=occurred,
                    score=event.admission_score, now=now, sources=[("turn", turn_id) for turn_id in event.source_turn_ids],
                )
            for op, cited in gated.ops:
                try:
                    await self._apply(owner, op, [event_ids[index] for index in cited], gated.events, cited, turns, now)
                    report.ops += 1
                except OpRejected as exc:
                    report.rejected.append(f"{op.op} {op.record_id or op.title or ''}: {exc}")
            await self.store.mark_reconciled([turn.id for turn in batch], now)
            await self.store.rebuild_profile(owner, now)
        report.events = len(event_ids)
        for line in report.dropped:
            logger.info("memory gate owner=%s dropped %s", owner, line)
        for line in report.rejected:
            logger.warning("memory reconcile owner=%s rejected %s", owner, line)
        logger.info(
            "memory reconcile owner=%s turns=%d events=%d ops=%d dropped=%d rejected=%d discarded=%d",
            owner, report.turns, report.events, report.ops, len(report.dropped), len(report.rejected), len(report.discarded),
        )
        return report

    async def _apply(
        self,
        owner: str,
        op: MemoryOp,
        event_ids: list[str],
        events: dict[int, LedgerEvent],
        cited: list[int],
        turns: dict[str, LoggedTurn],
        now: datetime,
    ) -> None:
        sources = [("event", event_id) for event_id in event_ids]
        for cited_id in dict.fromkeys(op.from_records):
            cited_record = await self.store.get_record(owner, cited_id)
            if cited_id != op.record_id and cited_record is not None and cited_record["status"] == "ACTIVE":
                sources.append(("record", cited_id))
        record = await self.store.get_record(owner, op.record_id) if op.record_id else None
        active = record is not None and record["status"] == "ACTIVE"
        if op.op == "create":
            if not (op.type and op.title and op.body):
                raise OpRejected("create needs type, title, and body")
            if active:
                await self.store.revise_record(
                    owner, record["id"], title=op.title, aliases=op.aliases, body=op.body,
                    expires_at=parse_ts(op.expires_at), now=now, sources=sources, keep_sources=True,
                )
                return
            wanted = op.record_id if op.record_id and _RECORD_ID.match(op.record_id) and record is None else None
            await self.store.create_record(
                owner, record_id=wanted or await self.store.free_id(owner, op.type, op.title), type=op.type,
                title=op.title, body=op.body, aliases=op.aliases, expires_at=parse_ts(op.expires_at),
                source="reconciler", now=now, sources=sources,
            )
        elif op.op in ("update", "supersede"):
            if not active:
                raise OpRejected(f"unknown record id {op.record_id!r}")
            await self.store.revise_record(
                owner, record["id"], title=op.title, aliases=op.aliases or None, body=op.body,
                expires_at=parse_ts(op.expires_at), now=now, sources=sources, keep_sources=op.op == "update",
            )
        elif op.op == "expire":
            if not active:
                raise OpRejected(f"unknown record id {op.record_id!r}")
            await self.store.set_status(owner, [record["id"]], "EXPIRED", now)
        elif op.op == "link":
            if not active:
                raise OpRejected(f"unknown record id {op.record_id!r}")
            targets = [
                link for link in LINK.findall(f"{op.body or ''} {' '.join(op.aliases)}")
                if link != record["id"] and await self.store.get_record(owner, link) is not None
            ]
            if not targets:
                raise OpRejected("link names no known record")
            extra = "".join(f"\n- Related: [[{link}]]" for link in targets if f"[[{link}]]" not in record["body"])
            await self.store.revise_record(
                owner, record["id"], body=record["body"] + extra, now=now, sources=sources, keep_sources=True
            )
        elif op.op == "commitment_add":
            if not op.title:
                raise OpRejected("commitment_add needs a title")
            first_turn = turns[events[cited[0]].source_turn_ids[0]]
            commitment_id = await self._add_commitment(owner, op, first_turn)
            await self.store.link_events_to_commitment(event_ids, commitment_id)
        else:
            commitment = await self.store.get_commitment(owner, op.record_id or "")
            if commitment is None or commitment["status"] != "PENDING":
                raise OpRejected(f"unknown open commitment {op.record_id!r}")
            await self.store.link_events_to_commitment(event_ids, commitment["id"])
            if op.op == "commitment_done":
                await self.store.finish_commitment(commitment["id"])
            else:
                await self.store.set_commitment_plan(
                    commitment["id"], next_check_at=parse_ts(op.next_check_at),
                    on_no_progress=op.on_no_progress, waiting_on=op.waiting_on,
                )

    async def _add_commitment(self, owner: str, op: MemoryOp, turn: LoggedTurn) -> str:
        title = redact(op.title or "")
        existing = [row for row in await self.store.open_commitments(owner, 500) if row["commitment"].strip().lower() == title.strip().lower()]
        if existing:
            commitment_id = existing[0]["id"]
        else:
            channel = turn.key.split(":")[1] if ":" in turn.key else turn.key
            due = parse_ts(op.due)
            fields = {
                "workspace_id": self.store.workspace_id,
                "source_type": "DIRECT_DM" if channel.startswith("D") else "APP_MENTION",
                "channel_id": channel,
                "raw_text": title,
                "summary": title,
                "commitment": title,
                "due_date": format_ts(due) if due else None,
                "embedding": generate_embedding(title),
                "owner_user_id": owner,
            }
            repo = self.store.repo
            if op.person:
                _contact, commitment_id = await repo.record_interaction(contact_name=op.person, **fields)
            else:
                commitment_id = await repo.insert_interaction(contact_id=None, **fields)
        await self.store.set_commitment_plan(
            str(commitment_id), next_check_at=parse_ts(op.next_check_at), on_no_progress=op.on_no_progress,
            waiting_on=op.waiting_on,
        )
        return str(commitment_id)

    async def tick(self, now: datetime | None = None) -> None:
        """Run whatever memory work is due. The running process calls this every minute."""
        now = now or self.store.clock()
        for owner in await self.store.owners():
            try:
                await self.tick_owner(owner, now)
            except Exception:
                logger.exception("memory tick failed owner=%s", owner)

    async def tick_owner(self, owner: str, now: datetime) -> None:
        profile = await self.store.profile(owner)
        night = _night_of(now, _zone(profile), self.config.nightly_hour)
        if profile is None or profile["nightly_on"] is None:
            async with self._tx:
                await self.store.set_nightly_on(owner, night.isoformat())
        elif profile["nightly_on"] != night.isoformat():
            await self.nightly(owner, now)
            return
        await self.reconcile(owner, now)

    async def nightly(self, owner: str, now: datetime) -> None:
        await self.reconcile(owner, now, everything=True)
        zone = _zone(await self.store.profile(owner))
        night = _night_of(now, zone, self.config.nightly_hour)
        yesterday = night - timedelta(days=1)
        await self._daily_episode(owner, yesterday, zone, now)
        if night.weekday() == 0:
            await self._weekly_episode(owner, night, now)
        async with self._tx:
            expired = await self.store.expire_records(owner, now)
            deleted = await self.store.delete_reconciled_turns(owner, now - timedelta(days=self.config.raw_retention_days))
            await self.store.rebuild_profile(owner, now)
            await self.store.set_nightly_on(owner, night.isoformat())
        logger.info("memory nightly owner=%s night=%s expired=%d turns_deleted=%d", owner, night, len(expired), deleted)

    async def _daily_episode(self, owner: str, day: date, zone: ZoneInfo, now: datetime) -> None:
        start = datetime.combine(day, time(0), zone)
        events = await self.store.events_created_between(owner, start, start + timedelta(days=1))
        if not events:
            return
        text = json.dumps({
            "date": day.isoformat(),
            "events": [{"kind": e["kind"], "summary": e["summary"], "when": str(e["occurred_at"])} for e in events],
            "open_commitments": [row["commitment"] for row in await self.store.open_commitments(owner)],
        })
        draft = await self.model.generate_structured(
            tier="light", system=DAILY_EPISODE_PROMPT, text=text, schema=EpisodeDraft, timeout_s=BACKGROUND_TIMEOUT_S
        )
        await self._save_episode(
            owner, f"episode_daily:{day.isoformat()}", "episode_daily", f"Day of {day.isoformat()}", draft.body,
            [("event", event["id"]) for event in events], now,
        )

    async def _weekly_episode(self, owner: str, monday: date, now: datetime) -> None:
        dailies = []
        for offset in range(7, 0, -1):
            record = await self.store.get_record(owner, f"episode_daily:{(monday - timedelta(days=offset)).isoformat()}")
            if record is not None and record["status"] == "ACTIVE":
                dailies.append(record)
        if not dailies:
            return
        text = "\n\n".join(f"{record['title']}:\n{record['body']}" for record in dailies)
        draft = await self.model.generate_structured(
            tier="light", system=WEEKLY_EPISODE_PROMPT, text=text, schema=EpisodeDraft, timeout_s=BACKGROUND_TIMEOUT_S
        )
        week = (monday - timedelta(days=7)).isocalendar()
        await self._save_episode(
            owner, f"episode_weekly:{week.year}-w{week.week:02d}", "episode_weekly",
            f"Week of {(monday - timedelta(days=7)).isoformat()}", draft.body,
            [("record", record["id"]) for record in dailies], now,
        )

    async def _save_episode(
        self, owner: str, record_id: str, kind: str, title: str, body: str, sources: list[tuple[str, str]], now: datetime
    ) -> None:
        async with self._tx:
            existing = await self.store.get_record(owner, record_id)
            if existing is not None:
                await self.store.revise_record(owner, record_id, body=body, now=now, sources=sources, keep_sources=False)
            else:
                await self.store.create_record(
                    owner, record_id=record_id, type=kind, title=title, body=body, source="reconciler", now=now,
                    sources=sources,
                )

    async def rebuild(self, owner: str, since: datetime | None = None) -> None:
        """Wipe compiled memory and replay the retained turns through the live write path, in time order."""
        async with self._tx:
            await self.store.wipe_derived(owner, since)
        turns = await self.store.turns(owner, since=since)
        now = self.store.clock()
        moments: list[tuple[datetime, int, LoggedTurn | None]] = [(now, 0, None)]
        for turn in turns:
            moments += [(turn.at, 1, turn), (turn.at + self.config.idle, 0, None)]
        if turns:
            zone = _zone(await self.store.profile(owner))
            day = turns[0].at.astimezone(zone).date()
            while day <= now.astimezone(zone).date():
                moments.append((datetime.combine(day, time(self.config.nightly_hour), zone), 0, None))
                day += timedelta(days=1)
        for at, _order, turn in sorted((m for m in moments if m[0] <= now), key=lambda m: (m[0], m[1])):
            if turn is None:
                await self.tick_owner(owner, at)
            elif turn.role == "tool":
                await self._replay(owner, turn, at)
        logger.info("memory rebuild owner=%s since=%s turns=%d", owner, since, len(turns))

    async def _replay(self, owner: str, turn: LoggedTurn, at: datetime) -> None:
        try:
            call = json.loads(turn.text)
        except json.JSONDecodeError:
            logger.warning("memory rebuild skipped unreadable tool turn %s", turn.id)
            return
        args, source = call.get("args") or {}, call.get("turn")
        if str(call.get("result", "")).startswith('{"error"'):
            return
        if call.get("tool") == "remember":
            await self.remember(owner, args["text"], args.get("type") or "fact", args.get("about"), source, now=at)
        elif call.get("tool") == "forget":
            await self.forget(owner, args["query_or_id"], source, now=at)


def _title(text: str, limit: int = 80) -> str:
    first = summarize_body(text.strip().splitlines()[0] if text.strip() else "note", limit)
    return first.rstrip(".") or "note"


def _zone(profile: dict[str, Any] | None) -> ZoneInfo:
    try:
        return ZoneInfo((profile or {}).get("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _night_of(now: datetime, zone: ZoneInfo, hour: int) -> date:
    """The local date of the most recent nightly boundary (03:00 by default) at or before now."""
    local = now.astimezone(zone)
    return local.date() if local.hour >= hour else local.date() - timedelta(days=1)
