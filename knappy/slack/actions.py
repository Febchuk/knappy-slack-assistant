"""Block Kit action handlers for approval and proactive buttons."""

from __future__ import annotations

from knappy.runtime import KnappyRuntime


def _action_value(body: dict) -> str:
    return body["actions"][0]["value"]


def _user_id(body: dict) -> str:
    return body["user"]["id"]


async def _apply(client, body: dict, result) -> None:
    channel = body.get("channel", {}).get("id")
    message_ts = body.get("message", {}).get("ts")
    if result.ephemeral and channel:
        await client.chat_postEphemeral(channel=channel, user=_user_id(body), text=result.ephemeral)
    if result.replacement_blocks and channel and message_ts:
        await client.chat_update(
            channel=channel,
            ts=message_ts,
            blocks=result.replacement_blocks,
            text="Updated",
        )
    if result.modal:
        await client.views_open(trigger_id=body.get("trigger_id"), view=result.modal)


def register_actions(app, runtime: KnappyRuntime) -> None:
    @app.action("btn_approve_action")
    async def approve(ack, body, client):
        await ack()
        result = await runtime.gateway.approve(_action_value(body), _user_id(body))
        await _apply(client, body, result)

    @app.action("btn_approve_proactive_action")
    async def approve_proactive(ack, body, client):
        await ack()
        result = await runtime.gateway.approve(_action_value(body), _user_id(body))
        await _apply(client, body, result)

    @app.action("btn_cancel_action")
    async def cancel(ack, body, client):
        await ack()
        result = await runtime.gateway.cancel(_action_value(body), _user_id(body))
        await _apply(client, body, result)

    @app.action("btn_edit_draft")
    async def edit(ack, body, client):
        await ack()
        result = await runtime.gateway.open_edit(_action_value(body), _user_id(body))
        await _apply(client, body, result)

    @app.view("hitl_edit_modal")
    async def save_edit(ack, body, client):
        await ack()
        draft_id = body["view"]["private_metadata"]
        content = body["view"]["state"]["values"]["content"]["staged_content"]["value"]
        await runtime.gateway.save_edit(draft_id, _user_id(body), content)

    @app.action("btn_resolve_commitment")
    async def resolve(ack, body, client):
        await ack()
        await runtime.heartbeat.mark_done(_action_value(body))
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        if channel and message_ts:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text="Marked complete",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": ":white_check_mark: Marked complete."}}],
            )

    @app.action("btn_snooze_commitment")
    async def snooze(ack, body, client):
        await ack()
        await runtime.heartbeat.snooze(_action_value(body))
        channel = body.get("channel", {}).get("id")
        message_ts = body.get("message", {}).get("ts")
        if channel and message_ts:
            await client.chat_update(
                channel=channel,
                ts=message_ts,
                text="Snoozed",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": ":zzz: Snoozed for 24 hours."}}],
            )
