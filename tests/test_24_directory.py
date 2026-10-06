"""Directory cache: Slack names in a table, person records point at an id, message text stays live."""

from datetime import datetime, timezone

from knappy.agent.tools import ToolRegistry, current_owner
from knappy.awareness.ingest import Pacing
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn
from knappy.memory.engine import MemoryEngine
from knappy.memory.store import MemoryStore
from knappy.memory.types import LedgerEvent, MemoryOp
from knappy.runtime import KnappyRuntime
from knappy.slack.directory import slack_id_for_name
from fakes import FakeSlack, member


def _event() -> LedgerEvent:
    return LedgerEvent(
        kind="learned", summary="Jonah owns the Self rollout", occurred_at="2026-10-06T15:00:00+00:00",
        source_turn_ids=["t1"], admission_score=0.9,
    )


def _person(title: str) -> MemoryOp:
    return MemoryOp(
        op="create", type="person", title=title, body="- Owns the Self rollout",
        from_events=[0], admission_score=0.9, reason="test",
    )


async def test_catch_up_caches_people_and_channels_not_message_text(repo: SqliteRepository) -> None:
    user = FakeSlack(
        members=[member("UJONAH", "Jonah Hale", display_name="Jonah")],
        user_id="U1",
    )
    user.conversations = [{"id": "C08TXKJDWN4", "name": "subscriber-self"}]
    user.history["C08TXKJDWN4"] = [
        {"user": "UJONAH", "ts": "10.000000", "text": "ship the widget Friday"},
    ]
    runtime = KnappyRuntime(
        repo, workspace_id="T_TEST", model=FakeModel([ModelTurn(text="ok")]), slack=FakeSlack(),
        user_client=user, awareness_owner="U1", awareness_pacing=Pacing(call_gap_s=0),
    )

    await runtime.awareness.catch_up(datetime(2026, 10, 6, tzinfo=timezone.utc))

    users = await repo.directory_users("T_TEST", "U1")
    channels = await repo.directory_channels("T_TEST", "U1")
    assert users == [{
        "slack_user_id": "UJONAH", "display_name": "Jonah", "real_name": "Jonah Hale", "handle": "jonah.hale",
    }]
    assert channels == [{"channel_id": "C08TXKJDWN4", "name": "subscriber-self"}]
    stored = " ".join(row["display_name"] + row["real_name"] + row["handle"] for row in users)
    assert "ship the widget" not in stored


async def test_search_resolves_a_channel_and_author_from_the_directory(repo: SqliteRepository) -> None:
    slack = FakeSlack()
    slack.conversations = [
        {"id": "C_SELF", "name": "subscriber-self"},
        {"id": "C_OTHER", "name": "general"},
    ]
    slack.history["C_SELF"] = [{"user": "UJONAH", "ts": "10.000000", "text": "own the rollout Friday"}]
    slack.history["C_OTHER"] = [{"user": "UJONAH", "ts": "11.000000", "text": "unrelated note"}]
    await repo.upsert_directory_user(
        "T_TEST", "U1", "UJONAH", display_name="Jonah", real_name="Jonah Hale", handle="jonah.hale",
        refreshed_at=format_ts(utc_now()),
    )
    await repo.upsert_directory_channel(
        "T_TEST", "U1", "C_SELF", name="subscriber-self", refreshed_at=format_ts(utc_now()),
    )
    await repo.upsert_directory_channel(
        "T_TEST", "U1", "C_OTHER", name="general", refreshed_at=format_ts(utc_now()),
    )
    tools = ToolRegistry(repo, "T_TEST", history=slack)
    token = current_owner.set("U1")
    try:
        hits = await tools.search_slack_history("#subscriber-self Jonah")
    finally:
        current_owner.reset(token)

    assert [hit["ts"] for hit in hits] == ["10.000000"]
    assert hits[0]["author"] == "Jonah" and "Jonah" not in hits[0]["text"]
    assert slack.users_info_calls == 0
    assert not any(call.startswith("conversations.history C_OTHER") for call in slack.api_calls)


async def test_reconciler_links_a_unique_person_and_skips_two_matches(repo: SqliteRepository) -> None:
    await repo.upsert_directory_user(
        "T_TEST", "U1", "UJONAH", display_name="Jonah", real_name="Jonah Hale", handle="jonah.hale",
        refreshed_at=format_ts(utc_now()),
    )
    await repo.upsert_directory_user(
        "T_TEST", "U1", "UJONAH2", display_name="Jonah", real_name="Jonah Pike", handle="jonah.pike",
        refreshed_at=format_ts(utc_now()),
    )
    assert await slack_id_for_name(repo, "T_TEST", "U1", "Jonah") is None
    assert await slack_id_for_name(repo, "T_TEST", "U1", "Jonah Hale") == "UJONAH"

    engine = MemoryEngine(MemoryStore(repo, "T_TEST"), FakeModel([]))
    async with repo.transaction():
        await engine._apply("U1", _person("Jonah Hale"), ["e1"], {0: _event()}, [0], {}, utc_now())
        await engine._apply("U1", _person("Jonah"), ["e2"], {0: _event()}, [0], {}, utc_now())

    hale = await engine.store.get_record("U1", "person:jonah-hale")
    ambiguous = await engine.store.get_record("U1", "person:jonah")
    assert hale["slack_user_id"] == "UJONAH"
    assert "UJONAH" not in hale["body"] and "UJONAH" not in hale["aliases"]
    assert ambiguous["slack_user_id"] is None
