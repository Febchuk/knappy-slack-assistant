# Specification 10: Seeded End-to-End Conversations

> **Amended by [Spec 17](./17_end_to_end_acceptance.md).** The seeded-DB and path-log approach carries over into `tests/journeys/`. Cases that assert fixed heuristic wording (identity text, general-question refusal) are retired.

## 1. Overview & Objectives

A reply that exists only in memory is not a Slack reply. These checks open a file-backed SQLite database, seed one person's Alex commitment, and require a Slack API post for every turn. They also record the path the agent took so a silent channel can be diagnosed from the terminal.

---

## 2. Seed

The seed is a temporary SQLite file initialized with `init_schema`, the same entry as `python -m knappy.main`. It is not the developer's live `knappy.db`.

| Row | Value |
| :--- | :--- |
| Workspace | `T_TEST` |
| Owner | `U1` |
| Contact | Alex |
| Commitment | send the revised budget by Thursday |
| Status | `PENDING` |

A second owner, `U2`, has no rows.

---

## 3. Conversation Cases

| Case | Input | Expected Slack result |
| :--- | :--- | :--- |
| Identity | Channel `<@UBOT> who are you` from `U1` | One reply that identifies Knappy |
| Open work | Channel `<@UBOT> what do I have to do` from `U1` | The seeded Alex commitment |
| Isolation | The same question from `U2` | No Alex commitment |
| Note then recall | DM `note:` about Alex, then `What did I promise to send Alex?` | Acknowledgement, then the stored sentence |
| Failed post | The poster raises | One visible `I hit an error answering that.` and a logged traceback |

DMs use `chat.postMessage`. A channel mention tries `chat.postEphemeral` for the asker. If that call raises, Knappy posts the same reply with `chat.postMessage` in the mention thread.

---

## 4. Path Log

Logger name `knappy`, level INFO. Each turn logs:

- received event type, channel, user, and text after the mention is stripped
- the route: `identity`, `list_commitments`, `search_commitments`, `search_history`, `chitchat`, or `react`
- the tool name on each ReAct step
- `ephemeral` or `postMessage`, plus the channel
- on failure, the exception type and message before the fallback post

---

## 5. Scope

### In-Scope
- File-backed seed and Slack-post assertions.
- Terminal path logs.
- A visible fallback when posting fails.

### Out-of-Scope
- Calling the live Slack workspace from the test suite.
- Seeding or migrating the developer's `knappy.db`.
