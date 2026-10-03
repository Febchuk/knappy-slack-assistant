"""Spec 03: sequential ingestion pipeline."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from knappy.db.repository import SqliteRepository
from knappy.ingestion.embed import generate_embedding
from knappy.ingestion.extract import ExtractedInteraction, SlmExtractor
from knappy.llm.fake import FakeModel
from fakes import HeuristicModel
from knappy.ingestion.filter import LocalStructuralFilter
from knappy.ingestion.gate import CompositeSystemOneGate, JevSystemOneAdapter, RegexFallbackAdapter
from knappy.ingestion.pipeline import IngestionPipeline


def test_ingest_01_bot_message_rejected() -> None:
    started = time.perf_counter()
    assert LocalStructuralFilter.should_evaluate(
        {"subtype": "bot_message", "text": "Jira bot updated PROJ-123 status"}
    ) is False
    assert time.perf_counter() - started < 0.002


def test_ingest_02_short_ping_rejected() -> None:
    assert LocalStructuralFilter.should_evaluate({"text": "hey there"}) is False


class _FakeJev:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = 0

    async def system_one(self, state, questions):
        self.calls += 1
        return self.response


def _response(intel, commitment, category, confidence, urgency=1.0):
    return SimpleNamespace(
        nouls={
            "has_actionable_intel": SimpleNamespace(noul=intel),
            "has_commitment": SimpleNamespace(noul=commitment),
        },
        choices={"category": SimpleNamespace(choice=category, confidence=confidence)},
        scores={"urgency": SimpleNamespace(score=urgency)},
    )


@pytest.mark.asyncio
async def test_ingest_03_note_acceptance() -> None:
    client = _FakeJev(_response(0.96, 0.4, "meeting_note", 0.95))
    decision = await JevSystemOneAdapter(client).evaluate(
        {"text": "note: Met with Dave, agreed on Q4 roadmap"}
    )
    assert decision.should_ingest is True
    assert decision.category == "MEETING_NOTE"
    assert decision.confidence >= 0.90


@pytest.mark.asyncio
async def test_ingest_04_non_regex_commitment() -> None:
    client = _FakeJev(_response(0.88, 0.81, "commitment", 0.8))
    decision = await JevSystemOneAdapter(client).evaluate(
        {"text": "Can you take a look at the revised deck before our call?"}
    )
    assert decision.should_ingest is True
    assert decision.contains_commitment is True
    assert decision.confidence >= 0.75


class _Boom:
    async def evaluate(self, event):
        raise TimeoutError("jev down")


@pytest.mark.asyncio
async def test_ingest_05_circuit_breaker() -> None:
    gate = CompositeSystemOneGate(_Boom(), RegexFallbackAdapter())
    passes, decision = await gate.should_ingest(
        {"text": "note: Met with Dave, agreed on Q4 roadmap"}
    )
    assert passes is True
    assert decision is not None
    assert decision.category == "MEETING_NOTE"


@pytest.mark.asyncio
async def test_ingest_06_extractor_sends_reference_date_and_validates() -> None:
    async def structured(schema, system, text):
        return {
            "contact_name": "Alex",
            "summary": "Sync with Alex from Acme",
            "commitment": "deliver the pitch deck",
            "due_date": "2026-10-02T17:00:00",
        }

    model = FakeModel(structured=structured)
    note = "Sync with Alex from Acme, promised to deliver the pitch deck by tomorrow at 5pm"
    extracted = await SlmExtractor(model).extract(note, datetime(2026, 10, 1, 12, 0, 0))
    assert isinstance(extracted, ExtractedInteraction)
    assert extracted.due_date == "2026-10-02T17:00:00"
    schema, system, text = model.structured_requests[0]
    assert schema is ExtractedInteraction
    assert "2026-10-01T12:00:00" in system
    assert text == note


def test_ingest_07_embedding_shape_and_latency() -> None:
    generate_embedding("warmup")
    started = time.perf_counter()
    vector = generate_embedding("Delivering the Q3 budget deck to leadership")
    elapsed = time.perf_counter() - started
    assert len(vector) == 384
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-5)
    assert elapsed < 0.02


@pytest.mark.asyncio
async def test_ingest_08_atomic_upsert(repo: SqliteRepository) -> None:
    gate = CompositeSystemOneGate(RegexFallbackAdapter(), RegexFallbackAdapter())
    pipeline = IngestionPipeline(repo, gate, SlmExtractor(HeuristicModel()), workspace_id="T_TEST")
    first = {"text": "note: Met with Alex from Acme, promised to send the revised budget by Thursday.", "channel": "D1"}
    second = {"text": "note: Met with Alex from Acme, promised to send the signed contract by Friday.", "channel": "D1"}
    await pipeline.run(first)
    await pipeline.run(second)
    contacts = await repo.find_contacts("T_TEST", name="Alex")
    assert len(contacts) == 1
    interactions = await repo.list_interactions_for_contact(contacts[0]["id"])
    assert len(interactions) == 2
    assert contacts[0]["last_interaction_ts"] is not None
