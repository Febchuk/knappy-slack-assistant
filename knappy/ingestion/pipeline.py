"""Four-stage ingestion: filter, extract, embed, commit."""

from __future__ import annotations

from datetime import datetime

from knappy.db.repository import SqliteRepository, format_ts
from knappy.ingestion.extract import ExtractedInteraction, SlmExtractor
from knappy.ingestion.embed import generate_embedding
from knappy.ingestion.gate import CompositeSystemOneGate, IngestionGateDecision


def to_sqlite_ts(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("T", " ")
    if len(normalized) == 10:
        normalized = f"{normalized} 00:00:00"
    return normalized[:19]


class IngestionPipeline:
    def __init__(
        self,
        repo: SqliteRepository,
        gate: CompositeSystemOneGate,
        extractor: SlmExtractor | None = None,
        workspace_id: str = "default_ws",
    ) -> None:
        self.repo = repo
        self.gate = gate
        self.extractor = extractor or SlmExtractor()
        self.workspace_id = workspace_id

    async def run(self, event: dict) -> ExtractedInteraction | None:
        passes, _decision = await self.gate.should_ingest(event)
        if not passes:
            return None
        return await self.commit(event)

    async def commit(self, event: dict, decision: IngestionGateDecision | None = None) -> ExtractedInteraction:
        text = event.get("text", "")
        extracted = await self.extractor.extract(text, datetime.now())
        source = "NOTE_INGEST" if text.strip().lower().startswith("note:") else (
            "APP_MENTION" if event.get("type") == "app_mention" else "DIRECT_DM"
        )
        embedding = generate_embedding(extracted.summary)
        await self.repo.record_interaction(
            workspace_id=event.get("workspace_id") or self.workspace_id,
            contact_name=extracted.contact_name,
            contact_email=extracted.contact_email,
            company=extracted.company,
            source_type=source,
            channel_id=event.get("channel") or event.get("channel_id") or "unknown",
            thread_ts=event.get("thread_ts"),
            raw_text=text,
            summary=extracted.summary,
            commitment=extracted.commitment,
            due_date=to_sqlite_ts(extracted.due_date),
            embedding=embedding,
            last_interaction_ts=format_ts(),
            owner_user_id=str(event.get("user") or ""),
        )
        return extracted


def acknowledgement(extracted: ExtractedInteraction) -> str:
    lines = [f"Logged interaction with *{extracted.contact_name}*."]
    if extracted.commitment:
        lines.append(f"*Commitment:* {extracted.commitment}")
    return "\n".join(lines)
