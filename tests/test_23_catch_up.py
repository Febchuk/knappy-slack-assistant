"""Spec 23 §5-§6: catch-up fits Slack's 1-a-minute history limit, and the installer hears from Knappy once."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from knappy.awareness.ingest import Pacing
from knappy.db.repository import SqliteRepository
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn
from knappy.runtime import KnappyRuntime
from fakes import FakeSlack

NOW = datetime(2026, 10, 6, 15, tzinfo=timezone.utc)


def _workspace(dms: int, channels: int) -> FakeSlack:
    user = FakeSlack(user_id="U1")
    user.conversations = [{"id": f"D{index}", "is_im": True, "user": f"U_{index}"} for index in range(dms)]
    user.conversations += [{"id": f"C{index}", "name": f"channel-{index}"} for index in range(channels)]
    for conversation in user.conversations:
        user.history[conversation["id"]] = [
            {"user": "U_SAM", "ts": f"{NOW.timestamp() - 600:.6f}", "text": "can you review the launch deck please"}
        ]
    return user


def _runtime(repo: SqliteRepository, user: FakeSlack, clock, pacing: Pacing) -> KnappyRuntime:
    return KnappyRuntime(
        repo, workspace_id="T_TEST", model=FakeModel([ModelTurn(text="ok")]), slack=FakeSlack(), user_client=user,
        awareness_owner="U1", awareness_pacing=pacing, clock=clock,
    )


def _history_reads(user: FakeSlack) -> list[str]:
    return [call.split()[1] for call in user.api_calls if call.startswith("conversations.history ") and "ratelimited" not in call]


async def test_catch_up_runs_once_dms_first_and_capped(repo: SqliteRepository) -> None:
    user = _workspace(dms=4, channels=40)
    now = [NOW]
    runtime = _runtime(repo, user, lambda: now[0], Pacing(call_gap_s=0, catch_up_conversations=10))

    await runtime.awareness.tick()
    now[0] += timedelta(hours=3)
    await runtime.awareness.tick()

    reads = _history_reads(user)
    assert len(reads) == 10, "one catch-up, capped; an hourly re-read cannot fit 1 call a minute"
    assert reads[:4] == ["D0", "D1", "D2", "D3"], "DMs first"


async def test_catch_up_waits_out_a_rate_limit(repo: SqliteRepository) -> None:
    user = _workspace(dms=1, channels=0)
    user.rate_limited["conversations.history"] = 2
    runtime = _runtime(repo, user, lambda: NOW, Pacing(call_gap_s=0))

    buffered = await runtime.awareness.catch_up(NOW)

    assert user.api_calls.count("conversations.history ratelimited") == 2
    assert _history_reads(user) == ["D0"] and buffered == 1, "the read went through after Retry-After"


async def test_catch_up_gives_up_after_its_retries(repo: SqliteRepository) -> None:
    user = _workspace(dms=1, channels=0)
    user.rate_limited["conversations.history"] = 10
    runtime = _runtime(repo, user, lambda: NOW, Pacing(call_gap_s=0, rate_limit_retries=2))

    assert await runtime.awareness.catch_up(NOW) == 0
    assert user.api_calls.count("conversations.history ratelimited") == 3


def test_first_run_text_reads_a_due_date_from_either_database() -> None:
    from knappy.runtime import first_run_text

    as_text = first_run_text([], [{"commitment": "ship the widget", "due_date": "2026-10-09T17:00:00+00:00"}])
    as_datetime = first_run_text([], [{"commitment": "ship the widget", "due_date": datetime(2026, 10, 9, 17, tzinfo=timezone.utc)}])
    assert "• ship the widget, due 2026-10-09" in as_text
    assert as_datetime == as_text, "Postgres hands back a datetime"
