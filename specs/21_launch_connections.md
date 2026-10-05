# Specification 21: Launch Connections

## 1. Overview & Objectives

[Spec 19](./19_mcp_connections.md) connects users to MCP servers. [Spec 20](./20_acting_through_mcp.md) puts their tools in front of the model with HITL. This spec turns both on for the five launch servers: Lorikeet, Grain, Gmail, Google Calendar, and BigQuery. It adds no per-server Python. Every server is one `mcp_servers.toml` entry.

It also delivers four smaller pieces:

- A lever for finishing an entry after connecting: `python -m knappy.mcp tools <server> --user <id>`.
- Two hardening fixes from Spec 20's open issues.
- A container that serves the OAuth callback.
- A walkthrough for adding the next server.

The live run needs a person. They create the Google OAuth client, finish each consent in a browser, and run the tunnel. Everything before that point ships here, together with the checklist they follow (§8).

---

## 2. The Five Entries (`mcp_servers.toml`)

Each entry started from `python -m knappy.mcp probe <url>`. The Google tool lists below were read live on 2026-10-04 with unauthenticated `tools/list` calls, which all three Google endpoints answer. Lorikeet and Grain answer `401` without a token, so their tools are unknown until someone connects.

| Name | URL | Auth | Scopes | Tool policy |
| :--- | :--- | :--- | :--- | :--- |
| `lorikeet` | `https://mcp.lorikeetcx.ai` | `oauth_dcr` | `tickets:read tickets:write customers:read manage:read` | Allow all. Classification comes from annotations, so unknown tools default to `write`. Globs are confirmed with `tools` after connecting. |
| `grain` | `https://api.grain.com/_/mcp` | `oauth_dcr` | none advertised | Same as Lorikeet. Grain advertises no `refresh_token` grant, so users reconnect when the token expires. |
| `gmail` | `https://gmailmcp.googleapis.com/mcp/v1` | `oauth_static`, group `google` | `gmail.readonly`, `gmail.compose` | Read tools, plus `create_draft`, with `body_field` set to `body`. Label, trash, and spam tools are denied. |
| `calendar` | `https://calendarmcp.googleapis.com/mcp/v1` | `oauth_static`, group `google` | `calendar.events`, `calendar.calendarlist.readonly`, `calendar.events.freebusy` | All 9 tools. Writes are edited as JSON. |
| `bigquery` | `https://bigquery.googleapis.com/mcp` | `oauth_static`, group `google` | `bigquery` | Read tools only. `execute_sql` and `cancel_job` are denied. |

The three Google entries share `client_id_env = "GOOGLE_OAUTH_CLIENT_ID"` and `client_secret_env = "GOOGLE_OAUTH_CLIENT_SECRET"`. One consent covers all three servers.

### 2.1 Why these values

- **Gmail and Calendar URLs end in `/mcp/v1`.** On 2026-10-04, a POST `initialize` to `https://gmailmcp.googleapis.com/mcp` returned 404, and the same request to `.../mcp/v1` returned 200. Calendar behaves the same way. Google's guide ([Configure the Google Workspace MCP servers](https://developers.google.com/workspace/guides/configure-mcp-servers)) lists the `/mcp/v1` URLs, and the protected-resource metadata at `/.well-known/oauth-protected-resource/mcp/v1` names `.../mcp/v1` as the resource. The `/mcp` metadata still exists, which is why the probe accepted that URL.
- **Gmail has no send tool.** Its 23 tools include `create_draft` but nothing that sends. `gmail.compose` covers creating drafts. `gmail.send` would grant nothing a tool uses, so it is not requested. These are the two scopes Google's guide lists. The label, trash, and spam tools need `gmail.modify` or `gmail.labels`. Knappy does not request those scopes, so the tools are denied. Without the deny, the model could stage drafts that would only fail after approval.
- **Calendar scopes.** Google's guide lists read-only scopes: `calendar.calendarlist.readonly`, `calendar.events.freebusy`, and `calendar.events.readonly`. Creating events needs `calendar.events`, which also covers reading events, so it replaces `calendar.events.readonly`. All three scopes appear in the server's `scopes_supported`.
- **BigQuery has one scope.** `scopes_supported` lists only `https://www.googleapis.com/auth/bigquery`. Google's BigQuery guide asks for the same scope plus the IAM roles `roles/mcp.toolUser`, `roles/bigquery.jobUser`, and `roles/bigquery.dataViewer`. The plan covers read queries, and `execute_sql_readonly` serves those. `execute_sql` is annotated destructive and can run DML or DDL, so it is denied together with `cancel_job`. Removing the deny turns both back on behind approval cards.
- **No `read` or `write` overrides for Google.** All 41 Google tools carry `readOnlyHint`, and in every case the hint matches the tool's name and description.
- **Lorikeet's path is the root.** A POST to `https://mcp.lorikeetcx.ai` returned `401` with `WWW-Authenticate: Bearer realm="MCP", resource_metadata="https://mcp.lorikeetcx.ai/.well-known/oauth-protected-resource"`, and `/mcp` returned `404 Cannot POST /mcp`. The protected-resource metadata names the root as the resource.
- **Grain's `WWW-Authenticate`** points at `https://grain.com/.well-known/oauth-protected-resource`. Discovery uses the path-inserted `api.grain.com` form, which resolves (Spec 19 §3.1).

### 2.2 Tool names after prefixing

The longest prefixed Google name is `gmail__apply_sensitive_message_label`, at 36 characters, which is under Gemini's limit of 64. The denied tools are the longest ones, and the longest exposed name is `calendar__respond_to_event`. Lorikeet and Grain names are checked by `tools` (§3).

---

## 3. The `tools` Lever (`python -m knappy.mcp tools <server> --user <slack_user_id>`)

This command lists one server's tools using a connection that is already stored. It reads `KNAPPY_DATABASE_URL`, `KNAPPY_PUBLIC_URL`, and `KNAPPY_SECRET_KEY` from `.env` and the environment. It takes the workspace from `--workspace`, falling back to `KNAPPY_WORKSPACE_ID`, and then to the only workspace that holds a connection for that user and auth group.

It prints one row per tool the server lists, including tools the entry's `tools.deny` hides:

| Column | Meaning |
| :--- | :--- |
| `tool` | The server's tool name. |
| `hint` | `readOnlyHint`: `true`, `false`, or `-` when absent. |
| `access` | `read` or `write` under the current entry (Spec 19 §6.3), or `denied`. |
| `gemini` | `ok`, or the reason Spec 20 would skip the tool: `name` when `<server>__<tool>` breaks Gemini's name rule, `schema` when the schema is invalid or has an unresolvable `$ref` (§4.2). |
| `args` | Top-level argument names, with required ones marked `*`. |

After the rows come suggested lines, each with a comment asking you to confirm it before pasting:

- `read = [...]` lists tools named like reads (`get_*`, `list_*`, `search_*`, `find_*`, `read_*`, `query_*`, `describe_*`) that are not annotated read-only. They default to `write` today.
- `write = [...]` lists tools annotated read-only whose names suggest a change (`create`, `update`, `delete`, `send`, `post`, `reply`, `add`, `remove`, `set`, `close`, `assign`, `archive`, `cancel`, `execute`).
- `body_field = {...}` maps each write tool with a top-level string argument named `body`, `text`, `message`, `content`, `comment`, `note`, or `reply` to that argument.

If the user is not connected, or the connection needs reauthorization, the command prints the status and exits 1. It never prints tokens.

Hub support: `McpHub.list_server(owner, server)` returns the raw `mcp.types.Tool` list or `NotConnected`. The command uses this method, so it goes through the same refresh and 401 handling as a turn does.

---

## 4. Hardening

### 4.1 Negative cache for `list_tools`

Before this spec, a server that failed or timed out was not cached. The next message tried it again, and a down server cost every message up to Spec 20's 10-second budget. After this spec:

- Each server's listing has its own 8-second timeout, which is under the 10-second turn budget. Servers are listed concurrently, so one slow server does not consume the time of the others.
- A failure or timeout caches an empty list for that owner and server for 2 minutes (`FAILED_TTL`). Successes keep the 10-minute TTL.
- A `401` still marks the connection `needs_reauth` and caches nothing, because the next turn sees the connection as not connected and skips the network.
- `forget(owner)` clears negative entries too, so reconnecting retries immediately.

The hub's injected clock drives both TTLs.

### 4.2 Schemas Gemini rejects

Spec 20 sent each server's `inputSchema` to Gemini verbatim and skipped a tool only when `jsonschema` called its schema invalid. On 2026-10-04 that was tested live against `gemini-3-flash-preview` (scratch script, not committed):

- **The real schemas.** All 41 real Google tool schemas, sent together in one request, were accepted. They include `$defs`, `$ref`, `readOnly`, `deprecated`, `format: int32`, `format: byte`, and `x-google-enum-descriptions`.
- **Accepted constructs.** Twenty-two individual probes were accepted. They cover `const`, `type: [string, null]`, `allOf`, `not`, `patternProperties`, `if`/`then`, `uniqueItems`, `examples`, `x-` keys, recursive `$ref`, `definitions`, root `$ref: "#"`, `$anchor`, percent-encoded refs, a relative `$id`, a missing `type`, an empty `enum`, boolean subschemas, object defaults, unknown formats, `dependentRequired`, `contentEncoding`, `$schema` draft-07, OpenAPI `nullable`, arrays without `items`, mixed-type `enum`, an invalid regex `pattern`, and nesting 7 levels deep.
- **Rejections.** Gemini rejected only one thing: a `$ref` that does not resolve inside the schema, whether it points at a missing local definition or an external URL. The error was `400 INVALID_ARGUMENT: reference to undefined schema at properties.a`.
- **Blast radius.** When one good tool and one tool with a dangling `$ref` were declared together, the whole request failed with that same 400.

So `app_tool_spec` now resolves every `$ref` in the schema against the schema itself, using `jsonschema`'s `referencing` registry with no remote retrieval. If any reference does not resolve, the tool is skipped and a `reason=schema` warning is logged. The tools command uses the same check. No key stripping was added, because no other key was rejected.

---

## 5. Container (`Dockerfile`)

The container copies `mcp_servers.toml` next to the package, so `DEFAULT_PATH` resolves. It runs `EXPOSE 8080`, the default `KNAPPY_CALLBACK_PORT`. It still runs one process, because Socket Mode and the in-memory tool cache both assume a single worker.

---

## 6. Walkthrough (`docs/adding_an_mcp_connection.md`)

A one-page guide covering these steps:

1. Run `probe`.
2. Paste the entry.
3. Add env vars, only for `oauth_static`.
4. Restart, connect from a Slack DM, and run `tools` to finalize the globs.

The guide also covers Google setup and local running behind `cloudflared` with a generated `KNAPPY_SECRET_KEY`.

---

## 7. Scope

### In-Scope
- The five entries, the `tools` command, `McpHub.list_server`, the negative cache with per-server timeouts, the `$ref` check, the `Dockerfile`, the walkthrough, and the live checklist.

### Out-of-Scope
- Running the live checklist. It needs a person for OAuth consent.
- A Gmail send tool. The server offers none. "Email me a test" becomes a draft the user sends from Gmail.
- An `api_key` modal, token revocation, and disconnect.

---

## 8. Live Acceptance Checklist

The user runs this checklist after following `docs/adding_an_mcp_connection.md`, with Knappy running locally behind `cloudflared`. Each row is a pass or a fail with a note. Rows L-01 to L-06 come from the plan's live-run table. Rows L-07 to L-12 are the open items from Specs 19 and 20.

| ID | Check | Pass when |
| :--- | :--- | :--- |
| **L-01** | DM "connect lorikeet", finish consent, then ask for a ticket summary. Request a write, approve the card, and check Lorikeet. | The summary arrives with no card. The write shows a card, and after approval the change is visible in Lorikeet. |
| **L-02** | Connect Grain, then ask "what was decided in my last call with X". | An answer drawn from Grain, with no card. |
| **L-03** | DM "connect google", consent once, then ask "what apps do I have?" | `list_apps` shows Gmail, Google Calendar, and BigQuery all `connected`. One DM says all three connected. |
| **L-04** | "Draft me a test email to myself", approve the card, and open Gmail Drafts. | The draft exists with the subject and body from the card. Gmail MCP cannot send. |
| **L-05** | "What's on tomorrow?", then "invite me to a 15-minute test at 3pm tomorrow". Approve the card. | The first answer has no card. The second shows a JSON card, and after approval the event exists. |
| **L-06** | "In BigQuery project `<id>`, list my datasets", then run a small `SELECT`. | Both answers arrive with no card. |
| **L-07** | Real schemas load in Gemini. Watch Knappy's log (INFO by default) during L-01 to L-06. | No `mcp tool skipped ... reason=schema` lines, and no Gemini 400s. |
| **L-08** | Tool names fit after prefixing. Run `python -m knappy.mcp tools lorikeet --user <you>` and the same for `grain`. | Every row has `gemini ok`. Google names are already checked (§2.2). |
| **L-09** | Annotations are correct. Run `tools` for Lorikeet and Grain and compare `access` with each tool's description. | No read tool shows `write`, and no write tool shows `read`. Paste any suggested `read`, `write`, and `body_field` lines you agree with, then restart. |
| **L-10** | The JSON edit flow works. On the L-05 card, press Edit, change the time in the JSON, and approve. | The event is created at the edited time. |
| **L-11** | Google returns a refresh token. After L-03, run `sqlite3 knappy.db "select auth_group, refresh_token is not null, expires_at from mcp_connections"`. | The `google` row has a refresh token. An hour later, a Calendar read still works without reconnecting. |
| **L-12** | Lorikeet's MCP path. `curl -si -X POST https://mcp.lorikeetcx.ai -H 'content-type: application/json' -d '{}'`. | `401` with `WWW-Authenticate: Bearer ... resource_metadata=...`. L-01 passing confirms the root path end to end. |

---

## 9. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-LAUNCH-01** | `tests/test_19_mcp_conformance.py` over the five real entries. | Lorikeet and Grain pass. The Google entries skip, naming `GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET`, unless both are set. |
| **TEST-LAUNCH-02** | `tools fake --user U_A` against the fake MCP server after connecting, with a deny glob. | One row per listed tool, including the denied one. Hints, access, and argument names are correct. Suggested `read`, `write`, and `body_field` lines appear. No token is printed. |
| **TEST-LAUNCH-03** | `tools` for an unconnected user. | Exit 1 with the status. |
| **TEST-LAUNCH-04** | A server failing `list_tools`. Call `tools()` twice, then move the clock forward 2 minutes and call again. | One attempt for the first two calls, then a retry. |
| **TEST-LAUNCH-05** | A server that hangs during listing. | `tools()` returns within the per-server timeout, and the other server's tools are still returned. |
| **TEST-LAUNCH-06** | `app_tool_spec` with a dangling local `$ref`, an external `$ref`, and a valid `$defs` reference. | The first two are skipped with a log line. The third is kept. |
