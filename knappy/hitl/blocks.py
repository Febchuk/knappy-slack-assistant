"""Slack Block Kit payloads for staged actions and receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

KEY_ARGUMENTS = 8
ARGUMENT_CHARS = 150


def approval_blocks(
    draft_id: str | None,
    recipient_name: str,
    staged_content: str,
    file_name: str | None = None,
    *,
    recipient_id: str | None = None,
    problem: str | None = None,
    post_as_user: bool = False,
) -> list[dict]:
    """The approval card. Without a draft there is nobody to send to: the card shows why and offers no send button.

    A reply posted as the user (Spec 18 §5) says so: it will appear under their name, not Knappy's.
    """
    headline = "Staged Reply" if post_as_user else "Staged File Share" if file_name else "Staged Outbound Message"
    target = f"{recipient_name} (<@{recipient_id}>)" if recipient_id else recipient_name
    if post_as_user:
        target = f"Post as you in {recipient_name}"
    attached = f"\n*File:* {file_name}" if file_name else ""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Action Required:* {headline}\n*Target:* {target}{attached}",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {staged_content}"},
        },
    ]
    if draft_id is None:
        return blocks + [unreachable_block(recipient_name, problem)]
    return blocks + [
        {
            "type": "actions",
            "block_id": f"hitl_action_block_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "btn_approve_action",
                    "text": {"type": "plain_text", "text": "Approve & Post" if post_as_user else "Approve & Send"},
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


def app_action_blocks(
    draft_id: str,
    app: str,
    tool_title: str,
    arguments: dict[str, Any],
    body_field: str | None,
    staged_content: str,
) -> list[dict]:
    """Spec 20 §5: the app, the tool, its key arguments, and the editable body (or every argument as JSON)."""
    lines = [f"*Action Required:* {app}: {tool_title}"]
    if body_field is not None:
        shown = [(name, value) for name, value in arguments.items() if name != body_field][:KEY_ARGUMENTS]
        lines += [f"*{name}:* {_argument(value)}" for name, value in shown]
        body = f"> {staged_content}"
    else:
        body = f"```{staged_content}```"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:3000]}},
        {
            "type": "actions",
            "block_id": f"hitl_action_block_{draft_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "btn_approve_action",
                    "text": {"type": "plain_text", "text": "Approve & Run"},
                    "style": "primary",
                    "value": draft_id,
                },
                {"type": "button", "action_id": "btn_edit_draft", "text": {"type": "plain_text", "text": "Edit"}, "value": draft_id},
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


def _argument(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= ARGUMENT_CHARS else text[: ARGUMENT_CHARS - 1] + "…"


def unreachable_block(recipient_name: str, problem: str | None) -> dict:
    reason = problem or f"I don't know who {recipient_name} is in Slack"
    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f":warning: Not sendable: {reason}. @-mention them in a reply and I'll redraft it.",
            }
        ],
    }


def _done(action_type: str | None, recipient_name: str, file_name: str | None, tool_title: str | None) -> str:
    if action_type == "APP_ACTION":
        return f"ran *{tool_title}* in *{recipient_name}*"
    if action_type == "SHARE_FILE":
        return f"shared {f'*{file_name}*' if file_name else 'the file'} with *{recipient_name}*"
    if action_type == "SEND_SLACK_DM":
        return f"sent the message to *{recipient_name}*"
    if action_type == "POST_THREAD_REPLY":
        return f"posted your reply as you in {recipient_name}"
    return f"dispatched to *{recipient_name}*"


def receipt_blocks(
    user_id: str,
    recipient_name: str,
    timestamp: str | None = None,
    *,
    action_type: str | None = None,
    file_name: str | None = None,
    tool_title: str | None = None,
) -> list[dict]:
    when = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: *Done:* approved by <@{user_id}>; "
                    f"{_done(action_type, recipient_name, file_name, tool_title)} at {when} UTC."
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


def failed_blocks(
    recipient_name: str, *, action_type: str | None = None, file_name: str | None = None, tool_title: str | None = None
) -> list[dict]:
    if action_type == "APP_ACTION":
        text = (
            f":x: *Failed:* Could not run *{tool_title}* in *{recipient_name}*. "
            f"If it needs reconnecting, ask me to connect {recipient_name}."
        )
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if action_type == "SHARE_FILE":
        what = f"share {f'*{file_name}*' if file_name else 'the file'} with *{recipient_name}*"
    elif action_type == "SEND_SLACK_DM":
        what = f"send the message to *{recipient_name}*"
    elif action_type == "POST_THREAD_REPLY":
        what = f"post your reply in {recipient_name}"
    else:
        what = f"dispatch to *{recipient_name}*"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f":x: *Failed:* Could not {what}. Nothing was sent."},
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


@dataclass(frozen=True)
class ProactiveCard:
    """One actionable item under a proactive message: its draft to another person, if any, and its buttons."""

    label: str
    interaction_id: str | None = None
    recipient: str | None = None
    recipient_id: str | None = None
    draft_id: str | None = None
    message: str | None = None
    problem: str | None = None
    attention_id: str | None = None
    permalink: str | None = None


def proactive_blocks(text: str, cards: list[ProactiveCard]) -> list[dict]:
    """A brief or nudge: the text for the user, then each item with Send (only with a draft), Done, and Snooze."""
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}]
    for card in cards:
        blocks.extend(_card_blocks(card))
    return blocks


def _card_blocks(card: ProactiveCard) -> list[dict]:
    body = f"*{card.label}*"
    if card.permalink:
        body += f" <{card.permalink}|Open in Slack>"
    if card.draft_id and card.message:
        body += f"\nDraft to *{card.recipient}* (<@{card.recipient_id}>):\n> {card.message}"
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": body[:3000]}}]
    if card.recipient and not card.draft_id:
        if card.problem:
            note = f":warning: I can't message {card.recipient} from here: {card.problem}."
        else:
            note = f"Reply in this thread and I'll draft a message to {card.recipient}."
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    buttons: list[dict] = []
    if card.draft_id:
        buttons += [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": f"Send to {card.recipient}"[:75]},
                "style": "primary",
                "action_id": "btn_approve_proactive_action",
                "value": card.draft_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Edit"},
                "action_id": "btn_edit_draft",
                "value": card.draft_id,
            },
        ]
    if card.interaction_id:
        buttons += [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Mark as Done"},
                "action_id": "btn_resolve_commitment",
                "value": card.interaction_id,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Snooze (24h)"},
                "action_id": "btn_snooze_commitment",
                "value": card.interaction_id,
            },
        ]
    if card.attention_id:
        buttons += [
            {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action, "value": card.attention_id}
            for text, action in (("Done", "btn_attention_done"), ("Snooze (24h)", "btn_attention_snooze"), ("Draft reply", "btn_attention_reply"))
        ]
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    return blocks


def settle_item(blocks: list[dict], value: str, note: str) -> list[dict] | None:
    """The message with the buttons carrying `value` replaced by `note`, so one item resolves without wiping a brief.

    None when no actions block carries the value: the caller replaces the whole message instead.
    """
    settled, found = [], False
    for block in blocks:
        if block.get("type") == "actions" and any(element.get("value") == value for element in block.get("elements") or []):
            settled.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
            found = True
        else:
            settled.append(block)
    return settled if found else None
