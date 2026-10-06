"""On-demand Slack reads: a permalink, and a search that reaches past the latest page."""

from datetime import datetime, timezone

from knappy.agent.prompt import IDENTITY
from knappy.agent.tools import ToolRegistry, parse_slack_permalink
from knappy.db.repository import SqliteRepository
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


async def test_read_slack_message_uses_the_user_client_for_a_thread_reply(repo: SqliteRepository) -> None:
    bot = FakeSlack()
    user = FakeSlack(members=[member("UJONAH", "Jonah Hale", display_name="Jonah")], user_id="U1")
    user.history[CHANNEL] = [
        {"user": "UAIDAN", "ts": PARENT, "text": "starting the thread", "reply_count": 1, "latest_reply": REPLY},
        {"user": "UJONAH", "ts": REPLY, "thread_ts": PARENT, "text": ASSIGNMENT},
    ]
    runtime = KnappyRuntime(
        repo, workspace_id="T_TEST", model=FakeModel([ModelTurn(text="ok")]), slack=bot,
        user_client=user, awareness_owner="U1",
    )

    found = await runtime.tools.read_slack_message(_permalink(REPLY, PARENT))

    assert found["author"] == "Jonah"
    assert found["text"] == ASSIGNMENT
    assert found["channel"] == CHANNEL and found["ts"] == REPLY
    assert found["permalink"] == _permalink(REPLY, PARENT)
    assert any(call.startswith(f"conversations.replies {CHANNEL} {PARENT}") for call in user.api_calls)
    assert not any(CHANNEL in call for call in bot.api_calls)
    missing = await runtime.tools.read_slack_message(_permalink("1728225660.000999", PARENT))
    assert missing == {"error": "No message at that link.", "channel": CHANNEL, "ts": "1728225660.000999"}


async def test_search_reaches_past_the_first_page_and_matches_the_author(repo: SqliteRepository) -> None:
    slack = FakeSlack(members=[
        member("UJONAH", "Jonah Hale", display_name="Jonah"),
        member("UOTHER", "Aidan Cole", display_name="Aidan"),
    ])
    reply = "1220.000100"
    old = "1000.000000"
    history = [
        {"user": "UOTHER", "ts": "1220.000000", "text": "checking in", "reply_count": 1, "latest_reply": reply},
        {"user": "UJONAH", "ts": reply, "thread_ts": "1220.000000", "text": "the widget ships Friday"},
    ]
    history += [
        {"user": "UOTHER", "ts": f"{ts}.000000", "text": f"status update {ts}"}
        for ts in range(1219, 1000, -1)
    ]
    history.append({"user": "UJONAH", "ts": old, "text": "own the Self chat widget rollout, due Friday"})
    slack.history["C_BUSY"] = history
    tools = ToolRegistry(repo, "T_TEST", history=slack)

    recent = await tools.search_slack_history("Jonah", channel_id="C_BUSY")
    assert [hit["ts"] for hit in recent] == [reply]
    assert "Jonah" not in recent[0]["text"] and recent[0]["author"] == "Jonah"

    window = await tools.search_slack_history(
        "Jonah", channel_id="C_BUSY", since=datetime.fromtimestamp(999, timezone.utc)
    )
    assert [hit["ts"] for hit in window] == [reply, old]
    assert "Jonah" not in window[1]["text"] and window[1]["author"] == "Jonah"
    assert sum(call == "conversations.history C_BUSY" for call in slack.api_calls) >= 3


def test_prompt_records_an_assignment_from_the_message() -> None:
    assert "read_slack_message" in IDENTITY
    assert "never with fetch_url" in IDENTITY
    assert "search index lagging" in IDENTITY
    assert "call add_commitment in the same turn" in IDENTITY
    assert "Do not ask them to repeat it." in IDENTITY
