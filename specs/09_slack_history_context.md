# Specification 09: Invited Slack History

> **Amended by [Spec 23](./23_slack_message_reads.md).** Reads use the user token when the runtime has one. A permalink is `read_slack_message`. `search_slack_history` accepts `since`, includes thread replies, and matches the author's display name.

## 1. Overview & Objectives

Answers may use recent text from the current DM and from channels where Knappy has been invited. That text is a tool result for the fast path and the ReAct loop. It is not written into contacts unless the existing note gate already would ingest the message the user just sent.

---

## 2. Tool Contract

```python
def search_slack_history(query: str, channel_id: str | None = None) -> list[dict]:
    """
    Return recent Slack messages whose text overlaps the query.

    Reads conversations.history for the current conversation and for public
    and private channels the bot is in. Does not insert contacts or interactions.
    """
```

Each hit is `{"channel", "user", "ts", "text"}`.

The current DM uses the existing `im:history` scope. Public and private channels require `channels:history` and `groups:history` on the bot. Listing invited conversations uses `users.conversations`. If that call fails, the tool still reads the current channel. Knappy does not request history for every channel in the workspace.

---

## 3. Scope

### In-Scope
- `search_slack_history` on the fast path and inside the ReAct tool registry.
- Manifest scopes `channels:history` and `groups:history`.

### Out-of-Scope
- Workspace-wide `channels:read` or history for channels the bot has not joined.
- Persisting history messages as contacts.

---

## 4. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-HIST-01** | Fake `conversations.history` contains "ship the budget Friday". Ask what was said about the budget. | The reply includes that sentence. No new contact row is written. |
