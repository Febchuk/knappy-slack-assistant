"""Slack action wiring, event dedupe, and production wiring."""

from __future__ import annotations

from datetime import timedelta

import pytest

from knappy.config import Settings
from knappy.db.repository import SqliteRepository, format_ts, utc_now
from knappy.hitl.gateway import ApprovalGateway
from knappy.runtime import KnappyRuntime
from knappy.slack.actions import register_actions
from knappy.slack.events import EventDeduplicator, on_app_mention, on_message
from fakes import FakeApp, FakeSlack, HeuristicModel


class Recorder:
    async def execute(self, draft):
        return None


def _body(action_id: str, value: str, user: str = "U1") -> dict:
    return {
        "user": {"id": user},
        "channel": {"id": "D1"},
        "message": {"ts": "9.0"},
        "trigger_id": "trig",
        "actions": [{"action_id": action_id, "value": value}],
    }


@pytest.mark.asyncio
async def test_action_handlers_update_slack(repo: SqliteRepository) -> None:
    runtime = KnappyRuntime(repo, workspace_id="T_TEST", model=HeuristicModel(), executor=Recorder())
    app = FakeApp()
    register_actions(app, runtime)
    draft_id = await repo.create_draft(
        workspace_id="T_TEST",
        user_id="U1",
        channel_id="D1",
        action_type="SEND_SLACK_DM",
        payload={
            "recipient_name": "Alex",
            "recipient_identifier": "U_ALEX",
            "staged_content": "Hello",
            "preview_summary": "Hello",
            "action_type": "SEND_SLACK_DM",
            "metadata": {},
        },
    )
    client = FakeSlack()

    async def ack():
        return None

    await app.handlers["btn_approve_action"](ack=ack, body=_body("btn_approve_action", draft_id), client=client)
    assert client.updates

    contact_id = await repo.upsert_contact("T_TEST", "Sam")
    interaction_id = await repo.insert_interaction(
        workspace_id="T_TEST",
        contact_id=contact_id,
        source_type="DIRECT_DM",
        channel_id="D1",
        raw_text="promise to send the notes tomorrow",
        summary="notes",
        commitment="send the notes",
        due_date=format_ts(utc_now() + timedelta(hours=2)),
    )
    await app.handlers["btn_resolve_commitment"](
        ack=ack, body=_body("btn_resolve_commitment", interaction_id), client=client
    )
    resolved = await repo.get_interaction(interaction_id)
    assert resolved["status"] == "FULFILLED"
    await app.handlers["btn_snooze_commitment"](
        ack=ack, body=_body("btn_snooze_commitment", interaction_id), client=client
    )

    other_id = await repo.create_draft(
        workspace_id="T_TEST",
        user_id="U1",
        channel_id="D1",
        action_type="SEND_SLACK_DM",
        payload={
            "recipient_name": "Alex",
            "recipient_identifier": "U_ALEX",
            "staged_content": "Edit me",
            "preview_summary": "Edit",
            "action_type": "SEND_SLACK_DM",
            "metadata": {},
        },
    )
    await app.handlers["btn_edit_draft"](ack=ack, body=_body("btn_edit_draft", other_id), client=client)
    assert client.modals
    await app.handlers["hitl_edit_modal"](
        ack=ack,
        body={
            "user": {"id": "U1"},
            "view": {
                "private_metadata": other_id,
                "state": {"values": {"content": {"staged_content": {"value": "Changed"}}}},
            },
        },
        client=client,
    )
    edited = await repo.get_draft(other_id)
    assert edited["payload"]["staged_content"] == "Changed"
    await app.handlers["btn_cancel_action"](ack=ack, body=_body("btn_cancel_action", other_id), client=client)
    cancelled = await repo.get_draft(other_id)
    assert cancelled["status"] == "CANCELLED"

    denied = await repo.create_draft(
        workspace_id="T_TEST",
        user_id="U1",
        channel_id="D1",
        action_type="GMAIL_DRAFT",
        payload={
            "recipient_name": "Alex",
            "recipient_identifier": "a@b.c",
            "staged_content": "mail",
            "preview_summary": "mail",
            "action_type": "GMAIL_DRAFT",
            "metadata": {},
        },
    )
    await app.handlers["btn_approve_proactive_action"](
        ack=ack, body=_body("btn_approve_proactive_action", denied, user="U_OTHER"), client=client
    )
    assert client.ephemerals


@pytest.mark.asyncio
async def test_duplicate_and_mention_events() -> None:
    deduper = EventDeduplicator(maxlen=1)
    seen: list[str] = []

    async def processor(event):
        seen.append(event["text"])

    async def ack():
        return None

    await on_message({"text": "hello there from me", "channel_type": "im", "client_msg_id": "m1"}, ack, processor=processor, deduper=deduper)
    await on_message({"text": "hello there from me", "channel_type": "im", "client_msg_id": "m1"}, ack, processor=processor, deduper=deduper)
    await on_message({"text": "channel chatter here now", "channel_type": "channel", "ts": "2"}, ack, processor=processor, deduper=deduper)
    await on_app_mention({"text": "<@U> hello there friend", "ts": "3"}, ack, processor=processor, deduper=deduper)
    await on_app_mention({"text": "<@U> hello there friend", "ts": "3"}, ack, processor=processor, deduper=deduper)
    assert seen == ["hello there from me", "<@U> hello there friend"]


@pytest.mark.asyncio
async def test_gateway_edit_and_missing(repo: SqliteRepository) -> None:
    gateway = ApprovalGateway(repo, Recorder())
    missing = await gateway.approve("missing", "U1")
    assert missing.status == "missing"
    draft_id = await repo.create_draft(
        workspace_id="T_TEST",
        user_id="U1",
        channel_id="D1",
        action_type="CALENDAR_INVITE",
        payload={
            "recipient_name": "Alex",
            "recipient_identifier": "alex@acme.test",
            "staged_content": "Tuesday",
            "preview_summary": "Invite",
            "action_type": "CALENDAR_INVITE",
            "metadata": {},
        },
    )
    opened = await gateway.open_edit(draft_id, "U1")
    assert opened.modal is not None
    denied = await gateway.open_edit(draft_id, "U2")
    assert denied.ephemeral
    saved = await gateway.save_edit(draft_id, "U1", "Wednesday")
    assert saved.ok is True
    blocked = await gateway.cancel(draft_id, "U2")
    assert blocked.ephemeral


@pytest.mark.asyncio
async def test_digest_and_cadence(repo: SqliteRepository) -> None:
    from knappy.heartbeat.engine import HeartbeatEngine
    from knappy.heartbeat.triage import ProactiveAlertTriager

    await repo.upsert_contact(
        "T_TEST",
        "Old Friend",
        reminder_cadence_days=30,
        last_interaction_ts="2020-01-01 00:00:00",
    )
    sent: list[dict] = []

    async def sender(**kwargs):
        sent.append(kwargs)

    async def classify(candidate):
        return {
            "interrupt_probability": 0.2,
            "strategy": "suppress_low_value",
            "strategy_confidence": 0.9,
            "consequence_score": 0.1,
        }

    engine = HeartbeatEngine(
        repo,
        ProactiveAlertTriager(classify),
        workspace_id="T_TEST",
        user_id="U1",
        sender=sender,
    )
    counts = await engine.run_tick(include_cadence=True, deliver_digest=True)
    assert counts["scanned"] == 1
    assert counts["suppressed"] == 1

    friend = await repo.find_contacts("T_TEST", name="Old Friend")
    await repo.enqueue_briefing(
        workspace_id="T_TEST",
        user_id="U1",
        kind="CADENCE",
        summary="Check in with Old Friend",
        contact_id=friend[0]["id"],
    )
    delivered = await engine.deliver_digest()
    assert delivered == 1
    assert sent


@pytest.mark.asyncio
async def test_socket_mode_serve(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("KNAPPY_DATABASE_URL", f"sqlite:///{tmp_path / 'knappy.db'}")
    monkeypatch.setenv("KNAPPY_WORKSPACE_ID", "T_SERVE")
    monkeypatch.setenv("KNAPPY_ADMISSION_THRESHOLD", "0.55")

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient

    from knappy.main import _serve

    async def start_async(self):
        await self.client.close()

    async def auth_test(self, *args, **kwargs):
        return {"team_id": "T_FROM_SLACK"}

    monkeypatch.setattr(AsyncSocketModeHandler, "start_async", start_async)
    monkeypatch.setattr(AsyncWebClient, "auth_test", auth_test)
    import knappy.main as main_module
    from knappy.llm.client import GeminiClient

    built: list = []
    real_runtime = main_module.KnappyRuntime

    def capture(*args, **kwargs):
        runtime = real_runtime(*args, **kwargs)
        built.append(runtime)
        return runtime

    monkeypatch.setattr(main_module, "KnappyRuntime", capture)
    ticking: list = []

    async def memory_loop(engine):
        ticking.append(engine)

    monkeypatch.setattr(main_module, "_memory_loop", memory_loop)
    await _serve()
    runtime = built[0]
    assert ticking == [runtime.memory_engine]
    assert runtime.memory_engine.model is runtime.loop.model
    assert runtime.memory_engine.config.admission_threshold == 0.55
    assert isinstance(runtime.loop.model, GeminiClient)
    assert runtime.pipeline.extractor.model is runtime.loop.model
    assert runtime.tools.history is runtime.users.client is not None
    assert runtime.daily_budget_usd == 1.0
    settings = Settings.from_env()
    assert settings.database_url.endswith("knappy.db")
