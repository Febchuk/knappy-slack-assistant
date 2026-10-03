"""Block Kit action handlers for approval and proactive buttons."""

from __future__ import annotations

from knappy.hitl.blocks import settle_item
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
        if result.replacement_blocks:
            note = result.replacement_blocks[0]["text"]["text"]
            result.replacement_blocks = settle_item(_shown(body), _action_value(body), note) or result.replacement_blocks
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
        await _settle(client, body, ":white_check_mark: Marked complete.", "Marked complete")

    @app.action("btn_snooze_commitment")
    async def snooze(ack, body, client):
        await ack()
        await runtime.heartbeat.snooze(_action_value(body))
        await _settle(client, body, ":zzz: Snoozed for 24 hours.", "Snoozed")


    @app.action("btn_attention_done")
    async def attention_done(ack, body, client):
        await ack()
        if await runtime.attention.resolve(_user_id(body), _action_value(body), "DONE", runtime.clock()):
            await _settle(client, body, ":white_check_mark: Done.", "Done")

    @app.action("btn_attention_snooze")
    async def attention_snooze(ack, body, client):
        await ack()
        if await runtime.attention.resolve(_user_id(body), _action_value(body), "SNOOZED", runtime.clock()):
            await _settle(client, body, ":zzz: Snoozed for 24 hours.", "Snoozed")

    @app.action("btn_attention_reply")
    async def attention_reply(ack, body, client):
        """Draft reply: ask the agent, in the brief's thread, as if the user had typed it."""
        await ack()
        owner, item_id = _user_id(body), _action_value(body)
        if await runtime.attention.get(owner, item_id) is None:
            return
        channel = body.get("channel", {}).get("id")
        thread = body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts")
        await runtime.handle_event({
            "type": "message", "channel": channel, "channel_type": "im", "user": owner, "thread_ts": thread,
            "text": f"Draft a reply to attention item {item_id} for me to approve.",
        })


def _shown(body: dict) -> list[dict]:
    return (body.get("message") or {}).get("blocks") or []


async def _settle(client, body: dict, note: str, text: str) -> None:
    """Resolve one item in place; a brief keeps its other items. Without the message's blocks, replace it."""
    channel = body.get("channel", {}).get("id")
    message_ts = body.get("message", {}).get("ts")
    if not (channel and message_ts):
        return
    blocks = settle_item(_shown(body), _action_value(body), note)
    if blocks is None:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": note}}]
    await client.chat_update(channel=channel, ts=message_ts, text=text, blocks=blocks)
