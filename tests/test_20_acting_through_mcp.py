"""Spec 20: the agent acts through connected MCP servers. Reads run now; writes wait for the owner's approval."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import aiosqlite

from fakes import FakeClock, FakeSlack, agent, dm, knappy_runtime, memory_structured
from knappy.agent.prompt import APPS
from knappy.agent.tools import TOOL_SPECS, SlackThread, StagedDraft, ToolRegistry, current_owner, current_thread
from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import app_action_blocks
from knappy.hitl.gateway import ApprovalGateway
from knappy.llm.fake import FakeModel
from knappy.mcp.hub import McpHub
from knappy.slack.executor import SlackActionExecutor
from mcp_fakes import WORKSPACE, World

BODY_FIELDS = {"send_*": "body"}


@contextmanager
def as_user(user: str, channel: str = "D1") -> Iterator[None]:
    owner = current_owner.set(user)
    thread = current_thread.set(SlackThread(channel, None))
    try:
        yield
    finally:
        current_thread.reset(thread)
        current_owner.reset(owner)


async def setup(world: World, *users: str, **entry: Any) -> tuple[McpHub, ToolRegistry, ApprovalGateway]:
    hub = await world.hub(world.entry(body_field=BODY_FIELDS, **entry))
    for user in users:
        await world.connect(hub, user)
    registry = ToolRegistry(world.repo, WORKSPACE, mcp=hub)
    return hub, registry, ApprovalGateway(world.repo, SlackActionExecutor(None, mcp=hub))


def token_of(world: World, user: str) -> str:
    return next(f"Bearer {token}" for token, owner in world.oauth.access.items() if owner == user)


async def drafts(repo: SqliteRepository) -> list[dict[str, Any]]:
    cursor = await repo.connection.execute("SELECT id, action_type, status, user_id, payload FROM action_drafts")
    return [{**dict(row), "payload": json.loads(row["payload"])} for row in await cursor.fetchall()]


async def stage(registry: ToolRegistry, user: str, name: str, args: dict[str, Any]) -> str:
    with as_user(user):
        staged = await registry.call(name, args)
    assert isinstance(staged, StagedDraft) and staged.draft_id
    return staged.draft_id


async def test_read_tool_runs_now_without_a_draft(world: World) -> None:
    _hub, registry, _gateway = await setup(world, "U_A")
    with as_user("U_A"):
        result = await registry.call("fake__whoami", {})
    assert result == {"app": "Fake", "tool": "whoami", "content": token_of(world, "U_A"), "is_error": False}
    assert [call.tool for call in world.calls] == ["whoami"]
    assert await drafts(world.repo) == []


async def test_write_and_unannotated_tools_only_stage_drafts(world: World) -> None:
    _hub, registry, _gateway = await setup(world, "U_A")
    ticket = {"subject": "Refund", "body": "Please refund order 7", "priority": "high"}
    with as_user("U_A"):
        created = await registry.call("fake__create_ticket", ticket)
        noted = await registry.call("fake__send_note", {"body": "hello there"})
    assert isinstance(created, StagedDraft) and isinstance(noted, StagedDraft)
    assert created.for_model()["status"].startswith("Drafted for Fake. Waiting for the user's approval")
    assert world.calls == []
    rows = {row["id"]: row for row in await drafts(world.repo)}
    create_row, note_row = rows[created.draft_id], rows[noted.draft_id]
    assert {row["action_type"] for row in rows.values()} == {"APP_ACTION"}
    assert {row["user_id"] for row in rows.values()} == {"U_A"}
    assert create_row["payload"]["metadata"] == {
        "server": "fake", "tool": "create_ticket", "tool_title": "Create ticket", "arguments": ticket, "body_field": None,
    }
    assert json.loads(create_row["payload"]["staged_content"]) == ticket
    assert note_row["payload"]["metadata"]["body_field"] == "body"
    assert note_row["payload"]["staged_content"] == "hello there"
    card = json.dumps(noted.blocks)
    assert "Fake: send_note" in card and "hello there" in card and "btn_approve_action" in card
    assert created.blocks[1]["text"]["text"] == f"```{create_row['payload']['staged_content']}```"


async def test_approving_calls_once_with_the_owners_token(world: World) -> None:
    _hub, registry, gateway = await setup(world, "U_A", "U_B")
    draft_id = await stage(registry, "U_A", "fake__send_note", {"body": "ship it"})
    stranger = await gateway.approve(draft_id, "U_B")
    assert stranger.ephemeral and world.calls == []
    first = await gateway.approve(draft_id, "U_A")
    second = await gateway.approve(draft_id, "U_A")
    assert (first.status, second.status) == ("APPROVED", "ignored")
    assert [(call.tool, call.authorization, call.args) for call in world.calls] == [
        ("send_note", token_of(world, "U_A"), {"body": "ship it"}),
    ]
    assert "ran *send_note* in *Fake*" in json.dumps(first.replacement_blocks)


async def test_edits_reach_the_call(world: World) -> None:
    _hub, registry, gateway = await setup(world, "U_A")
    note = await stage(registry, "U_A", "fake__send_note", {"body": "draft one"})
    ticket = await stage(registry, "U_A", "fake__create_ticket", {"subject": "Bug", "body": "broken"})
    await gateway.save_edit(note, "U_A", "final words")
    await gateway.save_edit(ticket, "U_A", json.dumps({"subject": "Bug", "body": "broken", "priority": "high"}))
    assert (await gateway.approve(note, "U_A")).status == "APPROVED"
    assert (await gateway.approve(ticket, "U_A")).status == "APPROVED"
    assert [call.args for call in world.calls] == [
        {"body": "final words"}, {"subject": "Bug", "body": "broken", "priority": "high"},
    ]


async def test_mcp_error_marks_the_draft_failed(world: World) -> None:
    _hub, registry, gateway = await setup(world, "U_A")
    draft_id = await stage(registry, "U_A", "fake__close_ticket", {"ticket_id": "T-9"})
    result = await gateway.approve(draft_id, "U_A")
    assert result.status == "FAILED"
    assert [row["status"] for row in await drafts(world.repo)] == ["FAILED"]
    assert "Could not run *close_ticket* in *Fake*" in json.dumps(result.replacement_blocks)
    assert [call.tool for call in world.calls] == ["close_ticket"]


async def test_a_lost_connection_fails_the_draft_without_calling(world: World) -> None:
    _hub, registry, gateway = await setup(world, "U_A")
    draft_id = await stage(registry, "U_A", "fake__send_note", {"body": "later"})
    world.oauth.access.clear()
    result = await gateway.approve(draft_id, "U_A")
    assert result.status == "FAILED" and world.calls == []
    assert "ask me to connect Fake" in json.dumps(result.replacement_blocks)


async def test_not_connected_returns_a_connect_url_only_in_a_dm(world: World) -> None:
    _hub, registry, _gateway = await setup(world)
    with as_user("U_C"):
        missing = await registry.call("fake__whoami", {})
        link = await registry.connect_app("fake")
        listed = await registry.list_apps()
    with as_user("U_C", channel="C1"):
        public = await registry.connect_app("fake")
        public_missing = await registry.call("fake__whoami", {})
    assert missing["error"] == "not_connected" and missing["status"] == "not_connected"
    assert missing["connect_url"].startswith(f"{world.oauth.base}/authorize?")
    assert link["connect_url"].startswith(f"{world.oauth.base}/authorize?")
    assert listed == {"apps": [{"server": "fake", "app": "Fake", "status": "not_connected"}]}
    assert "connect_url" not in public and "personal" in public["note"]
    assert "connect_url" not in public_missing and public_missing["error"] == "not_connected"
    assert world.calls == []


async def test_a_user_cannot_see_or_call_through_another_users_connection(world: World) -> None:
    hub = await world.hub(world.entry(), world.entry("other"))
    await world.connect(hub, "U_A", "fake")
    await world.connect(hub, "U_B", "other")
    registry = ToolRegistry(world.repo, WORKSPACE, mcp=hub)
    with as_user("U_A"):
        names = {spec.name for spec in await registry.specs()}
        blocked = await registry.call("other__whoami", {})
        blocked_write = await registry.call("other__send_note", {"body": "x"})
    assert "fake__whoami" in names and not any(name.startswith("other__") for name in names)
    assert blocked["error"] == blocked_write["error"] == "not_connected"
    assert world.calls == [] and await drafts(world.repo) == []
    with as_user("U_B"):
        own = await registry.call("other__whoami", {})
    assert own["content"] == token_of(world, "U_B")


async def test_mcp_off_leaves_tools_and_prompt_unchanged(world: World, repo: SqliteRepository) -> None:
    off = await ToolRegistry(repo, WORKSPACE).specs()
    assert off == list(TOOL_SPECS.values())
    _hub, registry, _gateway = await setup(world)
    with as_user("U_NOBODY"):
        on = await registry.specs()
    assert [spec.name for spec in on] == [*TOOL_SPECS, "list_apps", "connect_app"]
    clock = FakeClock(world.clock())
    for mcp, expected in ((None, False), (_hub, True)):
        runtime = knappy_runtime(repo, FakeSlack(), clock, mcp=mcp)
        assert (APPS in await runtime._system_prompt("U_A", "dm:D1")) is expected


async def test_agent_turn_reads_and_stages_through_app_tools(world: World) -> None:
    hub = await world.hub(world.entry(body_field=BODY_FIELDS))
    await world.connect(hub, "U_A")
    model = FakeModel(
        agent({
            "check": [("fake__whoami", {}), ("fake__send_note", {"body": "on my way"})],
            "broken": ("fake__send_note", {}),
        }),
        structured=memory_structured(),
    )
    slack = FakeSlack()
    runtime = knappy_runtime(world.repo, slack, FakeClock(world.clock()), model=model, mcp=hub)
    reply = await runtime.handle_event(dm("check my apps and send a note", "1.0", user="U_A"))
    offered = {spec.name: spec for spec in model.requests[0].tools}
    assert offered["fake__send_note"].json_schema()["required"] == ["body"]
    assert "Changes data" in offered["fake__send_note"].description
    assert token_of(world, "U_A") in reply.text
    assert reply.blocks and "btn_approve_action" in json.dumps(reply.blocks)
    assert [call.tool for call in world.calls] == ["whoami"]
    [draft] = await drafts(world.repo)
    assert draft["payload"]["metadata"]["arguments"] == {"body": "on my way"}
    invalid = await runtime.handle_event(dm("broken note", "2.0", user="U_A"))
    assert "Invalid arguments for fake__send_note" in invalid.text
    assert len(await drafts(world.repo)) == 1


async def test_old_sqlite_drafts_table_gains_app_action(tmp_path) -> None:
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as old:
        await old.executescript(
            """
            CREATE TABLE workspaces (id TEXT PRIMARY KEY, team_name TEXT NOT NULL, bot_token TEXT NOT NULL,
                installed_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO workspaces (id, team_name, bot_token) VALUES ('T_TEST', 't', 'x');
            CREATE TABLE action_drafts (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL, channel_id TEXT NOT NULL, thread_ts TEXT,
                action_type TEXT NOT NULL CHECK(action_type IN (
                    'SEND_SLACK_DM', 'SHARE_FILE', 'POST_THREAD_REPLY', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
                expires_at DATETIME, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, executed_at DATETIME
            );
            INSERT INTO action_drafts (id, workspace_id, user_id, channel_id, action_type, payload)
            VALUES ('d1', 'T_TEST', 'U1', 'D1', 'POST_THREAD_REPLY', '{}');
            """
        )
        await old.commit()
    database = SqliteRepository(path)
    await database.connect()
    try:
        await database.init_schema()
        draft = await database.create_draft(
            workspace_id="T_TEST", user_id="U1", channel_id="D1", action_type="APP_ACTION", payload={}
        )
        cursor = await database.connection.execute("SELECT id, action_type FROM action_drafts")
        kept = {row["id"]: row["action_type"] for row in await cursor.fetchall()}
    finally:
        await database.close()
    assert kept == {"d1": "POST_THREAD_REPLY", draft: "APP_ACTION"}


async def test_connecting_a_group_dms_the_user_once(world: World, repo: SqliteRepository) -> None:
    group = {"auth": "service", "auth_group": "suite", "token_env": "SUITE_TOKEN"}
    hub = await world.hub(world.entry("mail", **group), world.entry("cal", **group))
    slack = FakeSlack()
    runtime = knappy_runtime(repo, slack, FakeClock(world.clock()), mcp=hub)
    await runtime.app_connected("U_A", "suite")
    assert [(post["channel"], post["text"]) for post in slack.posts] == [("DU_A", "Mail and Cal connected.")]


def test_card_lists_key_arguments_beside_the_body() -> None:
    arguments = {"subject": "Refund", "body": "Please refund", "tags": ["vip", "billing"], "note": "x" * 400}
    blocks = app_action_blocks("d1", "Lorikeet", "Create ticket", arguments, "body", "Please refund, edited")
    head, body = blocks[0]["text"]["text"], blocks[1]["text"]["text"]
    assert head.splitlines()[:3] == ["*Action Required:* Lorikeet: Create ticket", "*subject:* Refund", '*tags:* ["vip", "billing"]']
    assert len(head.splitlines()[3]) < 170 and "Please refund" not in head
    assert body == "> Please refund, edited"
