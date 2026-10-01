"""Slack Block Kit payloads for staged actions and receipts."""

from __future__ import annotations

from datetime import datetime, timezone


def approval_blocks(draft_id: str, recipient_name: str, staged_content: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Action Required:* Staged Outbound Message\n*Target:* {recipient_name}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {staged_content}"},
        },
        {
            "type": "actions",
            "block_id": f"hitl_action_block_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "btn_approve_action",
                    "text": {"type": "plain_text", "text": "Approve & Send"},
                    "style": "primary",
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "action_id": "btn_edit_draft",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "action_id": "btn_cancel_action",
                    "text": {"type": "plain_text", "text": "Cancel"},
                    "style": "danger",
                    "value": draft_id,
                },
            ],
        },
    ]


def receipt_blocks(user_id: str, recipient_name: str, timestamp: str | None = None) -> list[dict]:
    when = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: *Executed:* Action approved by <@{user_id}> "
                    f"and dispatched to *{recipient_name}* at {when}."
                ),
            },
        }
    ]


def cancelled_blocks() -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":x: Action cancelled."},
        }
    ]


def failed_blocks(recipient_name: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":x: *Failed:* Could not dispatch to *{recipient_name}*.",
            },
        }
    ]


def edit_modal(draft_id: str, staged_content: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "hitl_edit_modal",
        "private_metadata": draft_id,
        "title": {"type": "plain_text", "text": "Edit draft"},
        "submit": {"type": "plain_text", "text": "Save"},
        "blocks": [
            {
                "type": "input",
                "block_id": "content",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "staged_content",
                    "multiline": True,
                    "initial_value": staged_content,
                },
                "label": {"type": "plain_text", "text": "Message"},
            }
        ],
    }


def proactive_blocks(draft_id: str, interaction_id: str, contact_name: str, commitment_text: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    ":alarm_clock: *Commitment Due Today*\n"
                    f"You promised *{contact_name}*:\n> \"{commitment_text}\""
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Slack DM"},
                    "style": "primary",
                    "action_id": "btn_approve_proactive_action",
                    "value": draft_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Mark as Done"},
                    "action_id": "btn_resolve_commitment",
                    "value": interaction_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Snooze (24h)"},
                    "action_id": "btn_snooze_commitment",
                    "value": interaction_id,
                },
            ],
        },
    ]
