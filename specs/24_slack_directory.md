# Specification 24: Slack Directory Cache

## 1. Overview & Objectives

Knappy keeps a table of the Slack people and channels its user can see, so a search can turn "Jonah" and "#subscriber-self" into ids before it reads history. The table is a cache of names. It is not memory.

**Amends:** [Spec 23](./23_slack_message_reads.md) §4, which matches an author name by calling `users.info` on each search. The directory is checked first. `users.info` remains the fallback and fills the table when it hits.

A `person` record ([Spec 13](./13_memory_system.md)) may store `slack_user_id` when the reconciler creates or updates one and the directory has a single match for the title. That column is not embedded and is not a new memory type.

---

## 2. Tables

Both are scoped to one workspace and one owner. Awareness catch-up refreshes them from `users.conversations` and `users.list`. Message text is not written.

```sql
CREATE TABLE slack_directory_users (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    slack_user_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    real_name TEXT NOT NULL DEFAULT '',
    handle TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, owner_user_id, slack_user_id)
);

CREATE TABLE slack_directory_channels (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, owner_user_id, channel_id)
);
```

`memory_records.slack_user_id` is nullable. It is set only for a `person` row.

Lookup is exact after normalizing case and a leading `#` or `@`. A first name matches too. A person record is linked only when one directory row matches the title. Two Jonahs leave `slack_user_id` null.

---

## 3. Search

`search_slack_history` loads this owner's directory when an owner is set.

- A channel name in the query, or a `channel_id` argument that is a name, selects that channel and does not scan the other conversations.
- A person name matches messages whose author id is that directory row, including when the name is absent from the text. The hit's `author` is the directory display name, then real name, then handle.
- A name missing from the directory is resolved with `users.info`, and a successful lookup is written into the table.
- The message body is still read live ([Spec 23](./23_slack_message_reads.md)). The directory does not store it.

---

## 4. Scope

### In-Scope
- The two directory tables, refreshed on awareness catch-up.
- Search resolving a channel name and an author id from the directory before `conversations.history`.
- `slack_user_id` on a `person` record when the reconciler saves one and the directory match is unique.

### Out-of-Scope
- A vector node per directory row, or a channel memory type.
- Embedding `slack_user_id` or directory names into `memory_records.embedding`.
- Storing message text in the directory.
- Linking a person record when more than one directory row matches the title.

---

## 5. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-DIR-01** | Catch up with a user client whose directory has Jonah and whose conversations include `#subscriber-self`, plus one message in that channel. | `slack_directory_users` has Jonah's id, display name, real name, and handle. `slack_directory_channels` has `subscriber-self`. The message text is not in those rows. |
| **TEST-DIR-02** | Directory knows Jonah and `#subscriber-self`. Jonah's message in that channel does not contain his name. Another channel also has a message from him. The Slack client has no `users.info` profile. Search `#subscriber-self Jonah`. | One hit, from `#subscriber-self`, `author` Jonah. `users.info` was not called. The other channel was not read. |
| **TEST-DIR-03** | Directory has Jonah Hale and Jonah Pike, both display name Jonah. The reconciler creates a person titled "Jonah Hale" and one titled "Jonah". | "Jonah Hale" has `slack_user_id` of Hale's id. That id is not in the body or aliases. "Jonah" has a null `slack_user_id`. |
