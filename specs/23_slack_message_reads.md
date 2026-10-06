# Specification 23: On-Demand Slack Message Reads

## 1. Overview & Objectives

When the user points at a Slack message, Knappy reads that message and acts on it. A keyword scan of the latest page is not a read. An empty scan is not proof the message is missing, and it is not explained as Slack search lag.

**Amends:** [Spec 09](./09_slack_history_context.md) §2 (tool contract and client). [Spec 12](./12_agent_loop_v2.md) §4 (the prompt records an assignment from a message it was shown). [Spec 14](./14_web_research.md) §3: a `slack.com/archives` link is not fetched as a web page.

> **Amended by [Spec 24](./24_slack_directory.md).** A known person or channel name is resolved from the directory table before `users.info` and before scanning other conversations.

This does not change how people are learned. A display name resolved for a read is not a `person` record. Periodic learning stays the memory loop ([Spec 13](./13_memory_system.md)). Unsolicited DMs stay under the silence rules ([Spec 16](./16_proactive_v2.md) §3.1).

---

## 2. Client

Slack reads use the installer's user token when the runtime has one, and the bot token otherwise. The user token is the same client workspace awareness uses ([Spec 18](./18_workspace_awareness.md)). A private channel the bot has not joined is readable when that user token can see it.

Author ids are resolved to a display name with `users.info` for the read. The name is returned on the hit. It is not written to memory.

---

## 3. `read_slack_message`

```python
class ReadSlackMessageArgs(BaseModel):
    url: str   # https://….slack.com/archives/{channel}/p{ts}[?thread_ts=…]

def read_slack_message(url: str) -> dict:
    """
    Return the message at a Slack permalink.
    A miss is {"error", "channel", "ts"}, never [].
    A URL that is not a slack.com/archives link is {"error", "url"}.
    """
```

A hit is `{"channel", "user", "author", "ts", "text", "permalink"}`. `author` is the display name.

Parse the permalink: the digits after `p` are the message ts, with the dot inserted six places from the end. `thread_ts` in the query string marks a reply.

- **Parent** (no `thread_ts`, or `thread_ts` equal to the message ts): `conversations.history` with `latest` and `oldest` set to that ts and `inclusive` true.
- **Reply:** `conversations.replies` on `thread_ts`, then the message whose ts matches.

Do not call `fetch_url` on this URL. Do not call Slack `search.messages`.

---

## 4. `search_slack_history`

```python
class SearchSlackHistoryArgs(BaseModel):
    query: str
    channel_id: str | None = None          # defaults to the current conversation, then others the user is in
    since: datetime | None = None          # ISO 8601 with the user's offset; messages at or after this time
    limit: int = 20                        # 1..50, cap on hits returned
```

Each hit is `{"channel", "user", "author", "ts", "text"}`.

- With `since` omitted, read one page of `limit` parent messages (the latest page). This is the short default.
- With `since` set, page `conversations.history` back to that time, `inclusive`, up to 10 pages of 200. Do not stop at 20 parents.
- For each parent in that window with `reply_count`, include thread replies from `conversations.replies` that fall in the same window.
- Match query tokens against the message text and the author's display name. The author's name need not appear in the text.
- A channel whose history call fails is skipped. The tool still returns `[]` when nothing matches. The prompt, not this return value, decides whether to read a link or a named window instead.

`users.conversations` still lists further channels after `channel_id`. If that list call fails, the named channel is still read.

---

## 5. Prompt

In the identity rules:

- A `slack.com/archives` URL is `read_slack_message`, never `fetch_url`.
- When the user names a channel, a person, or a time, call `search_slack_history` with that `channel_id` and `since`. An empty keyword search is not proof the message is missing in that case: read that window.
- Never explain a miss as Slack's search index lagging.
- When a message the user showed assigns them work or states a due date, call `add_commitment` in the same turn and confirm what was saved. Do not ask them to repeat it. Open commitments already feed the morning brief.

---

## 6. Scope

### In-Scope
- `read_slack_message` for a permalink, parent or thread reply, on the user token when present.
- `search_slack_history` paging to `since`, thread replies in that window, and author-name matching.
- The prompt rules in §5.
- Tests in `tests/test_09_slack_history.py`.

### Out-of-Scope
- Slack `search.messages`. It lags. Timestamp reads do not. [Spec 18](./18_workspace_awareness.md) §8 stays as written.
- Writing a `person` record, contact, or profile entry from a display-name lookup.
- Changing awareness admission, the reconciler, or the heartbeat.
- Unsolicited DMs. This spec is the path the user already asked for.
- Reading a thread reply whose permalink omitted `thread_ts`.

---

## 7. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-READ-01** | Parse `https://acme.slack.com/archives/C…/p…?thread_ts=…`. | Channel, message ts, and `thread_ts` match the link. |
| **TEST-READ-02** | The bot client has no history for the channel. The user client has a thread reply whose author is Jonah. Call `read_slack_message` on that permalink through a runtime built with both clients. | The hit's `author` is Jonah, `text` is the reply, and `permalink` is the URL. `conversations.replies` ran on the user client. The bot client was not called for that channel. |
| **TEST-READ-03** | The same permalink shape for a ts that is not in the thread. | `{"error": "No message at that link.", "channel", "ts"}`. Not `[]`. |
| **TEST-READ-04** | A channel whose first history page is 20 messages. Jonah's reply is on that page; his name is not in the text. An older message of his is past that page; his name is not in that text either. Search for "Jonah" with no `since`, then again with `since` covering the older message. | Without `since`, only the reply. With `since`, the reply and the older message, `author` Jonah on both. History was requested more than once for the window. |
| **TEST-READ-05** | Read the identity prompt. | It names `read_slack_message`, forbids `fetch_url` for a Slack link, forbids a search-index explanation, and tells the model to call `add_commitment` in the same turn without asking the user to repeat the task. |
