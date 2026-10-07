# Specification 23: On-Demand Slack Message Reads

## 1. Overview & Objectives

When the user points at a Slack message, Knappy reads that message and acts on it. A keyword scan of the latest page is not a read. An empty scan is not proof the message is missing, and it is not explained as Slack search lag.

**Amends:** [Spec 09](./09_slack_history_context.md) §2 (tool contract and client). [Spec 12](./12_agent_loop_v2.md) §4 (the prompt records an assignment from a message it was shown). [Spec 14](./14_web_research.md) §3: a `slack.com/archives` link is not fetched as a web page.

> **Amended by [Spec 24](./24_slack_directory.md).** A known person or channel name is resolved from the directory table before `users.info` and before scanning other conversations.

This does not change how people are learned. A display name resolved for a read is not a `person` record. Periodic learning stays the memory loop ([Spec 13](./13_memory_system.md)). Unsolicited DMs stay under the silence rules ([Spec 16](./16_proactive_v2.md) §3.1).

### 1.1 What prompted this (2026-10-06)

Mikun installed Knappy into the Lorikeet workspace through `/slack/install` ([Spec 22](./22_multi_workspace_install.md)). Their feedback: "not proactive" and "can't see Slack messages after a certain time cutoff". Knappy could not read a pasted message link, blamed Slack's search index, and asked Mikun to retype the task. Production logs showed why:

- **Slack's limit on distributed apps outside the Marketplace.** Every `conversations.history` and `conversations.replies` call in Lorikeet was rate limited, with retries about 60 seconds apart (912 retries in one deployment). Since 2025-05-29 for new apps, and 2026-03-03 for existing installs, these apps get 1 call a minute and at most 15 messages per call. Mitable, the app's home workspace, is not limited.
- **The hourly catch-up never finished.** Reading Mikun's 262 conversations took about 8 hours (08:19 to 16:04 UTC), and the next catch-up was due every hour, so it held the entire history allowance and every other read waited behind it.
- **Nothing proactive to say.** Each heartbeat for Mikun had zero candidates. No commitment had been saved, and attention items arrived only after the 8-hour catch-up.

---

## 2. Client

Slack reads go through the **installer's user token only when the installer is the one asking**. Everyone else reads through the bot token, which sees only conversations Knappy was added to. When that stops a read, the tool says so instead of returning nothing. Per-person tokens for everyone else are a later spec.

> Before this rule, any user's search could list the installer's private DMs. A search for "salary" by someone else returned the installer's DM with HR.

Tool reads never wait out a rate limit. A `ratelimited` answer becomes "Slack is rate-limiting message reads for this workspace. Try again in a minute." The user client has no automatic retry. Only awareness waits for `Retry-After` (§5).

Author ids are resolved to a display name with `users.info` on the same token that read the message. The name is returned on the hit. It is not written to memory.

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

Do not call `fetch_url` on this URL. This is one history call, so it fits within the limit.

---

## 4. `search_slack_history`

```python
class SearchSlackHistoryArgs(BaseModel):
    query: str
    channel_id: str | None = None          # defaults to the current conversation
    since: datetime | None = None          # ISO 8601 with the user's offset; messages at or after this time
    limit: int = 20                        # 1..50, cap on hits returned
```

Each hit is `{"channel", "user", "author", "ts", "text"}`, plus `permalink` when search supplied one. When something stopped part of the read, the result is `{"hits": [...], "note": "..."}`. The note says why: someone other than the installer, a missing scope, or a rate limit.

- **Installer:** Slack `search.messages` (user scope `search:read`), which is not under the 1-a-minute limit. Names resolve through the directory ([Spec 24](./24_slack_directory.md)). A channel, a single author, and `since` become one query: `in:#name from:<@U…> after:YYYY-MM-DD`, where `after:` is the day before `since`. Without filters, each of up to 3 keywords is its own query, because Slack ANDs words.
- **Named or current conversation:** also read its latest page (`limit` messages, from `since` when set) with `conversations.history`. That costs one call and covers the few seconds search can trail a new message.
- **Someone else:** only that one page, through the bot. With no conversation named, the note says broader reading is available only to the installer.
- **Missing `search:read`** (installed before this spec): the page is still read, and the note asks for a reinstall.
- Hits before `since` are dropped. Words match the text or the author's name. A message from a named author always matches.
## 5. Prompt

In the identity rules:

- A `slack.com/archives` URL is `read_slack_message`, never `fetch_url`.
- When the user names a channel, a person, or a time, call `search_slack_history` with that `channel_id` and `since`. An empty keyword search is not proof the message is missing in that case: read that window.
- Never explain a miss as Slack's search index lagging.
- When a message the user showed assigns them work or states a due date, call `add_commitment` in the same turn and confirm what was saved. Do not ask them to repeat it. Open commitments already feed the morning brief.

---

## 5a. Catch-up and first run

**Catch-up** ([Spec 18](./18_workspace_awareness.md) §2.3) runs once, when the runtime starts or the workspace installs, not every hour. It reads at most 30 conversations, DMs first and then the most recently read ones, one page each. Live user events carry everything after that. A `ratelimited` answer waits for Slack's `Retry-After`, at most 3 times, then skips that conversation.

**First run.** When the installer's first catch-up has been read and every buffer has flushed, Knappy DMs them once. The message lists the open attention items and commitments it found, or says plainly that nothing needs them, and when the morning brief comes. `user_profile.first_run_at` records that it was sent, so a restart does not repeat it.

---

## 6. Scope

### In-Scope
- `read_slack_message` for a permalink, parent or thread reply, on the user token when present.
- `search_slack_history` paging to `since`, thread replies in that window, and author-name matching.
- The prompt rules in §5.
- Tests in `tests/test_09_slack_history.py`.

### Out-of-Scope
- Per-person Slack tokens for people other than the installer.
- Marketplace approval, which would lift the 1-a-minute limit but requires HTTP events instead of Socket Mode.
- Writing a `person` record, contact, or profile entry from a display-name lookup.
- Changing awareness admission, the reconciler, or the heartbeat.
- Unsolicited DMs. This spec is the path the user already asked for.
- Reading a thread reply whose permalink omitted `thread_ts`.

---

## 7. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-READ-01** | Parse `https://acme.slack.com/archives/C…/p…?thread_ts=…`. | Channel, message ts, and `thread_ts` match the link. |
| **TEST-READ-02** | The installer asks for a thread reply by permalink. The bot has no history for the channel. | The hit's `author` is Jonah, `text` is the reply, and `permalink` is the URL. `conversations.replies` ran on the user client. The bot client was not called for that channel. |
| **TEST-READ-06** | Someone other than the installer searches for "salary" and reads a link. The installer's token can see a DM with HR. | No hits, and a note that broad reading is installer-only. The user client made no calls. |
| **TEST-READ-07** | The installer searches "Jonah #subscriber-self this morning" with `since`. | One `search.messages` query, `in:#subscriber-self from:<@UJONAH> after:<day before>`. Only Jonah's message from that morning. The channel's latest page was also read. |
| **TEST-READ-08** | `search.messages` answers `missing_scope`. | The named channel's page is returned, with a note asking for a reinstall. |
| **TEST-READ-09** | `conversations.history` answers `ratelimited` to a link read. | The rate-limit error at once, and exactly one call. |
| **TEST-CATCH-01..03** | 4 DMs and 40 channels with a cap of 10. Two ticks 3 hours apart. A rate limit twice, then too many times. | 10 history reads in total, DMs first. The read succeeds after `Retry-After`. After the retries run out, the conversation is skipped. |
| **TEST-FIRST-01** | A new installer, with one request in #design. Advance, restart, advance. | One DM listing the request. No second DM after the restart. |
| **TEST-READ-03** | The same permalink shape for a ts that is not in the thread. | `{"error": "No message at that link.", "channel", "ts"}`. Not `[]`. |
| **TEST-READ-05** | Read the identity prompt. | It names `read_slack_message`, forbids `fetch_url` for a Slack link, forbids a search-index explanation, and tells the model to call `add_commitment` in the same turn without asking the user to repeat the task. |

---

## 8. Rollout Checklist (needs people)

1. In the Slack app dashboard, add the `search:read` user scope, or re-apply `slack/manifest.yml`.
2. Each installer (Mitable and Lorikeet) opens `<KNAPPY_PUBLIC_URL>/slack/install` again and approves, which grants `search:read`. Until they do, search falls back to the named channel's latest page and the reply says so.
3. After deploy, check the logs:
   - Lorikeet's catch-up line appears within about 30 minutes.
   - `awareness rate limited` lines stop after it.
   - No `search.messages` call is rate limited.
   - Each installer gets one `first run sent` line.
