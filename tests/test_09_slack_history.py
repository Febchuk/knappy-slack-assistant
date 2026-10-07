"""On-demand Slack reads (Spec 23): a permalink, Slack search for the installer, and nobody else reading their token."""

from datetime import datetime, timezone

from knappy.agent.prompt import IDENTITY
from knappy.agent.tools import current_owner, parse_slack_permalink
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.llm.fake import FakeModel
from knappy.llm.types import ModelTurn
from knappy.runtime import KnappyRuntime
from fakes import FakeSlack, member

CHANNEL = "C08TXKJDWN4"
PARENT = "1728225600.000100"
REPLY = "1728225660.000100"
ASSIGNMENT = "Agama, own the Self chat widget rollout, due Friday Oct 9"


def _permalink(ts: str, thread_ts: str | None = None) -> str:
    url = f"https://acme.slack.com/archives/{CHANNEL}/p{ts.replace('.', '')}"
    return f"{url}?thread_ts={thread_ts}&cid={CHANNEL}" if thread_ts else url


def test_permalink_splits_the_timestamp_and_thread() -> None:
    assert parse_slack_permalink(_permalink(REPLY, PARENT)) == (CHANNEL, REPLY, PARENT)


def _runtime(repo: SqliteRepository, bot: FakeSlack, user: FakeSlack) -> KnappyRuntime:
    return KnappyRuntime(
        repo, workspace_id="T_TEST", model=FakeModel([ModelTurn(text="ok")]), slack=bot,
        user_client=user, awareness_owner="U1",
    )


async def _as(owner: str, call):
    token = current_owner.set(owner)
    try:
        return await call
    finally:
        current_owner.reset(token)


async def test_read_slack_message_uses_the_installers_token_for_the_installer(repo: SqliteRepository) -> None:
    bot = FakeSlack()
    user = FakeSlack(members=[member("UJONAH", "Jonah Hale", display_name="Jonah")], user_id="U1")
    user.history[CHANNEL] = [
        {"user": "UAIDAN", "ts": PARENT, "text": "starting the thread", "reply_count": 1, "latest_reply": REPLY},
        {"user": "UJONAH", "ts": REPLY, "thread_ts": PARENT, "text": ASSIGNMENT},
    ]
    runtime = _runtime(repo, bot, user)

    found = await _as("U1", runtime.tools.read_slack_message(_permalink(REPLY, PARENT)))

    assert found["author"] == "Jonah"
    assert found["text"] == ASSIGNMENT
    assert found["channel"] == CHANNEL and found["ts"] == REPLY
    assert found["permalink"] == _permalink(REPLY, PARENT)
    assert any(call.startswith(f"conversations.replies {CHANNEL} {PARENT}") for call in user.api_calls)
    assert not any(CHANNEL in call for call in bot.api_calls)
    missing = await _as("U1", runtime.tools.read_slack_message(_permalink("1728225660.000999", PARENT)))
    assert missing == {"error": "No message at that link.", "channel": CHANNEL, "ts": "1728225660.000999"}


async def test_someone_else_never_reads_through_the_installers_token(repo: SqliteRepository) -> None:
    bot = FakeSlack()
    user = FakeSlack(user_id="U1")
    user.conversations = [{"id": "D_HR", "is_im": True, "user": "U_HR"}]
    user.history["D_HR"] = [{"user": "U_HR", "ts": "10.000000", "text": "salary review for you next week"}]
    user.history[CHANNEL] = [{"user": "UJONAH", "ts": REPLY, "text": ASSIGNMENT}]
    runtime = _runtime(repo, bot, user)

    searched = await _as("U_OTHER", runtime.tools.search_slack_history("salary"))
    read = await _as("U_OTHER", runtime.tools.read_slack_message(_permalink(REPLY)))

    assert searched["hits"] == [] and "only to the person who installed Knappy" in searched["note"]
    assert "salary" not in str(searched)
    assert ASSIGNMENT not in str(read) and "error" in read
    assert user.api_calls == [] and user.searches == [], "the installer's token was not used"


async def test_installer_search_asks_slack_search_with_the_named_filters(repo: SqliteRepository) -> None:
    morning = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    bot = FakeSlack()
    user = FakeSlack(members=[member("UJONAH", "Jonah Hale", display_name="Jonah")], user_id="U1")
    user.conversations = [{"id": CHANNEL, "name": "subscriber-self"}]
    assigned = f"{morning.timestamp() + 2460:.6f}"
    user.history[CHANNEL] = [
        {"user": "UJONAH", "ts": assigned, "text": ASSIGNMENT},
        {"user": "UJONAH", "ts": f"{morning.timestamp() - 86400:.6f}", "text": "yesterday's note"},
        {"user": "UAIDAN", "ts": f"{morning.timestamp() + 60:.6f}", "text": "work on the Self chat resumes shortly"},
    ]
    now = format_ts(utc_now())
    await repo.upsert_directory_user("T_TEST", "U1", "UJONAH", display_name="Jonah", real_name="Jonah Hale", refreshed_at=now)
    await repo.upsert_directory_channel("T_TEST", "U1", CHANNEL, name="subscriber-self", refreshed_at=now)
    runtime = _runtime(repo, bot, user)

    hits = await _as("U1", runtime.tools.search_slack_history("Jonah #subscriber-self this morning", since=morning))

    assert user.searches == ["in:#subscriber-self from:<@UJONAH> after:2026-10-05"]
    assert [hit["ts"] for hit in hits] == [assigned]
    assert hits[0]["author"] == "Jonah" and hits[0]["text"] == ASSIGNMENT
    assert hits[0]["permalink"].endswith(assigned.replace(".", ""))
    assert f"conversations.history {CHANNEL}" in user.api_calls, "the named channel's latest page covers search delay"


async def test_search_without_the_search_scope_reads_the_named_channel(repo: SqliteRepository) -> None:
    user = FakeSlack(members=[member("UJONAH", "Jonah Hale", display_name="Jonah")], user_id="U1")
    user.search_error = "missing_scope"
    user.history[CHANNEL] = [{"user": "UJONAH", "ts": REPLY, "text": ASSIGNMENT}]
    runtime = _runtime(repo, FakeSlack(), user)

    result = await _as("U1", runtime.tools.search_slack_history("widget rollout", channel_id=CHANNEL))

    assert [hit["text"] for hit in result["hits"]] == [ASSIGNMENT]
    assert "Reinstall Knappy" in result["note"]


async def test_a_rate_limited_read_says_so_at_once(repo: SqliteRepository) -> None:
    user = FakeSlack(user_id="U1")
    user.history[CHANNEL] = [{"user": "UJONAH", "ts": REPLY, "text": ASSIGNMENT}]
    user.rate_limited["conversations.history"] = 1
    runtime = _runtime(repo, FakeSlack(), user)

    read = await _as("U1", runtime.tools.read_slack_message(_permalink(REPLY)))

    assert read == {
        "error": "Slack is rate-limiting message reads for this workspace. Try again in a minute.",
        "channel": CHANNEL, "ts": REPLY,
    }
    assert user.api_calls == ["conversations.history ratelimited"], "no retry, no wait"


def test_prompt_records_an_assignment_from_the_message() -> None:
    assert "read_slack_message" in IDENTITY
    assert "never with fetch_url" in IDENTITY
    assert "search index lagging" in IDENTITY
    assert "call add_commitment in the same turn" in IDENTITY
    assert "Do not ask them to repeat it." in IDENTITY
