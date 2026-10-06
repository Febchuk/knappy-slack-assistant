"""Spec 16 PRO-BUG-2, for every draft: a name becomes a Slack user id at staging time, or the card cannot send."""

from __future__ import annotations

import json

from fakes import FakeSlack, member
from knappy.agent.tools import SlackThread, StagedDraft, ToolRegistry, current_owner, current_thread
from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import failed_blocks, receipt_blocks
from knappy.slack.users import RecipientResolver, UserDirectory


async def stage(repo: SqliteRepository, slack: FakeSlack, recipient: str, identifier: str | None = None) -> StagedDraft:
    """Stage a DM as the model would, through one registry per fake workspace so the directory cache is shared."""
    if not hasattr(slack, "tools"):
        slack.tools = ToolRegistry(repo, "T_TEST", history=slack)
    tools = slack.tools
    owner, thread = current_owner.set("U1"), current_thread.set(SlackThread("DU1", None))
    try:
        result = await tools.stage_outbound_action(
            "SEND_SLACK_DM", recipient, "Ask for the deck", "Could you send the deck?", recipient_identifier=identifier
        )
    finally:
        current_owner.reset(owner)
        current_thread.reset(thread)
    assert isinstance(result, StagedDraft)
    return result


def buttons(blocks: list[dict]) -> list[str]:
    return [element["action_id"] for block in blocks if block["type"] == "actions" for element in block["elements"]]


async def staged_to(repo: SqliteRepository) -> list[str]:
    cursor = await repo.connection.execute("SELECT payload FROM action_drafts")
    return [json.loads(row["payload"])["recipient_identifier"] for row in await cursor.fetchall()]


async def test_a_mention_wins_without_any_lookup(repo: SqliteRepository) -> None:
    slack = FakeSlack(members=[member("UOTHER", "Alex Other")])
    from_text = await stage(repo, slack, "<@UALEX>")
    from_id = await stage(repo, slack, "Alex", "UALEX2")

    assert await staged_to(repo) == ["UALEX", "UALEX2"]
    assert "<@UALEX>" in json.dumps(from_text.blocks) and buttons(from_id.blocks)[0] == "btn_approve_action"
    assert slack.directory_calls == []


async def test_a_contacts_stored_id_is_used(repo: SqliteRepository) -> None:
    await repo.upsert_contact("T_TEST", "Alex", slack_user_id="UALEX", owner_user_id="U1")
    slack = FakeSlack()
    await stage(repo, slack, "alex")

    assert await staged_to(repo) == ["UALEX"]
    assert slack.directory_calls == []


async def test_a_contacts_email_is_looked_up_and_remembered(repo: SqliteRepository) -> None:
    contact = await repo.upsert_contact("T_TEST", "Alex", email="alex@acme.test", owner_user_id="U1")
    slack = FakeSlack(members=[member("UALEX", "Alexander Kim", email="alex@acme.test")])
    await stage(repo, slack, "Alex")
    await stage(repo, slack, "Alex")

    assert await staged_to(repo) == ["UALEX", "UALEX"]
    assert slack.directory_calls == ["users.lookupByEmail alex@acme.test"], "the second draft reads the stored id"
    assert (await repo.get_contact(contact))["slack_user_id"] == "UALEX"


async def test_another_owners_contact_is_not_used(repo: SqliteRepository) -> None:
    await repo.upsert_contact("T_TEST", "Alex", slack_user_id="UALEX", owner_user_id="U2")
    staged = await stage(repo, FakeSlack(), "Alex")

    assert staged.draft_id is None and await staged_to(repo) == []


async def test_a_unique_directory_name_match(repo: SqliteRepository) -> None:
    slack = FakeSlack(members=[member("UALEX", "Alex Kim", display_name="akim"), member("USAM", "Sam Lee")])
    await stage(repo, slack, "Alex Kim")
    await stage(repo, slack, "akim")
    await stage(repo, slack, "Alex")

    assert await staged_to(repo) == ["UALEX", "UALEX", "UALEX"]
    assert slack.directory_calls == ["users.list"], "the directory is fetched once and cached"


async def test_an_ambiguous_name_offers_no_send_button(repo: SqliteRepository) -> None:
    slack = FakeSlack(members=[member("UALEX1", "Alex Kim"), member("UALEX2", "Alex Moreno")])
    staged = await stage(repo, slack, "Alex")

    assert staged.draft_id is None and await staged_to(repo) == []
    assert buttons(staged.blocks) == []
    assert "2 people" in json.dumps(staged.blocks) and "Could you send the deck?" in json.dumps(staged.blocks)
    told = staged.for_model()
    assert told["draft_id"] is None and "Not staged" in told["status"] and "@-mention" in told["status"]


async def test_an_unknown_name_offers_no_send_button(repo: SqliteRepository) -> None:
    await repo.upsert_contact("T_TEST", "Alex", owner_user_id="U1")
    staged = await stage(repo, FakeSlack(members=[member("USAM", "Sam Lee")]), "Alex", "Alex")

    assert staged.draft_id is None and buttons(staged.blocks) == []
    assert "couldn't find Alex" in json.dumps(staged.blocks)


async def test_an_email_identifier_is_looked_up(repo: SqliteRepository) -> None:
    resolver = RecipientResolver(repo, "T_TEST", UserDirectory(FakeSlack(members=[member("UALEX", "Alex Kim", email="a@x.test")])))
    found = await resolver.resolve("U1", "Alex", slack_id="a@x.test")

    assert found.user_id == "UALEX"


def test_receipts_say_what_happened_per_action() -> None:
    message = json.dumps(receipt_blocks("U1", "Alex", action_type="SEND_SLACK_DM"))
    shared = json.dumps(receipt_blocks("U1", "Alex", action_type="SHARE_FILE", file_name="plan.md"))
    failed_message = json.dumps(failed_blocks("Alex", action_type="SEND_SLACK_DM"))
    failed_share = json.dumps(failed_blocks("Alex", action_type="SHARE_FILE", file_name="plan.md"))

    assert "sent the message to *Alex*" in message and "shared *plan.md* with *Alex*" in shared
    assert "send the message to *Alex*" in failed_message and "share *plan.md* with *Alex*" in failed_share
