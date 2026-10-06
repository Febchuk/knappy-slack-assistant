# Specification 22: Multi-Workspace Install

## 1. Overview & Objectives

Until now Knappy served exactly one Slack workspace: the one `SLACK_BOT_TOKEN` belongs to. Anyone in another workspace had no way to install it.

This spec makes Knappy a distributed Slack app. A person in any workspace opens `<KNAPPY_PUBLIC_URL>/slack/install`, approves the scopes, and Knappy starts serving that workspace from the same process, with no restart and no new deployment.

- **Install flow.** `GET /slack/install` redirects to Slack's OAuth consent screen. `GET /slack/oauth_redirect` exchanges the code, stores the tokens, starts the workspace's runtime, and DMs the installer a welcome message.
- **Per-workspace tokens.** Each installation's bot token and installer user token are stored in `workspaces`, encrypted with a key derived from `KNAPPY_SECRET_KEY`.
- **One runtime per workspace.** A `Fleet` keeps one `KnappyRuntime` per team. Bolt's `authorize` gives every event its own workspace's token, and every handler routes to that workspace's runtime.
- **Uninstall.** `app_uninstalled` clears the tokens and stops serving the workspace. Its memory stays, so a reinstall resumes where it left off.

`SLACK_BOT_TOKEN` keeps working. The workspace it belongs to is served without an OAuth install, so the current deployment needs no migration.

---

## 2. Data Shape

```python
@dataclass(frozen=True)
class Installation:
    team_id: str
    team_name: str
    bot_token: str
    bot_user_id: str | None
    installer_user_id: str | None
    user_token: str | None   # the installer's; turns on Spec 18 awareness for them
```

Stored in the existing `workspaces` table. Three columns are added (`bot_user_id`, `installer_user_id`, `user_token`). `bot_token` and `user_token` hold Fernet ciphertext. A row whose `bot_token` does not decrypt is not an installation: a revoked row (`''`), or a row written before this spec with a plaintext token.

`upsert_workspace(id, name, token)` becomes `ensure_workspace(id, name)`, which creates the row and never touches tokens. Before this change, every boot overwrote the token column.

---

## 3. Process Model

| Piece | Before | After |
| :--- | :--- | :--- |
| Bolt app | `AsyncApp(token=SLACK_BOT_TOKEN)` | `AsyncApp(authorize=...)`. The installation is looked up by `team_id`. |
| Runtime | One, built at startup | `Fleet`: one per team, built at startup for every stored installation, on first event, and again on reinstall |
| Event handlers | `processor(event)` | `processor(event, team_id)`, with awareness resolved per team |
| Action handlers | `register_actions(app, runtime)` | `register_actions(app, runtime_for)`, keyed by `body["team"]["id"]` |
| Background loops | One runtime each | Each tick visits every runtime in the fleet. One workspace's failure is logged and the others still run. |
| Database | Opened per runtime | Opened once and shared |
| MCP callback | One hub | `state_workspace` reads the workspace sealed in the OAuth state and picks that workspace's hub |
| Workspace awareness | `SLACK_USER_TOKEN` | The installer's user token from the OAuth install. `SLACK_USER_TOKEN` still applies to the `SLACK_BOT_TOKEN` workspace. |

When a team has both an OAuth installation and `SLACK_BOT_TOKEN`, the OAuth installation wins. It is newer and carries a user token.

---

## 4. Configuration

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`, `GEMINI_API_KEY` | Always | Unchanged |
| `SLACK_BOT_TOKEN` | One of the two modes | The hand-installed workspace |
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `KNAPPY_PUBLIC_URL`, `KNAPPY_SECRET_KEY` | One of the two modes | Turn on `/slack/install` |

`Settings.from_env` refuses to start when neither mode is configured.

---

## 5. Security

- **State.** The OAuth `state` is a Fernet token under its own derived key (`slack-install`) with a 10-minute TTL. It proves the install started at this server. It is not bound to a browser session: an attacker could at most get a victim to install Knappy into a workspace the victim already administers.
- **Tokens at rest.** Encrypted with a key derived from `KNAPPY_SECRET_KEY`, separate from the MCP token and state keys. Rotating `KNAPPY_SECRET_KEY` makes every stored installation unreadable, so each workspace has to reinstall.
- **Enterprise Grid.** Org-wide installs are refused with a message. Knappy keys everything by `team_id`.

---

## 6. Slack App Setup (done once by a person)

1. In the app's settings, under **OAuth & Permissions → Redirect URLs**, add `<KNAPPY_PUBLIC_URL>/slack/oauth_redirect`. The manifest has a placeholder host.
2. Under **Event Subscriptions → Subscribe to bot events**, add `app_uninstalled`, or re-apply `slack/manifest.yml`.
3. Under **Manage Distribution**, complete the checklist and click **Activate Public Distribution**. Socket Mode apps can be distributed this way, but cannot be listed in the Slack Marketplace.
4. Copy the **Client ID** and **Client Secret** from **Basic Information** into `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` on Railway.
5. Share `<KNAPPY_PUBLIC_URL>/slack/install`.

---

## 7. Tests (`tests/test_22_install.py`)

| ID | Checks |
| :--- | :--- |
| Scopes | The install requests exactly the manifest's bot and user scopes. The manifest has the redirect URL and `app_uninstalled`. |
| Store | Tokens are not stored in plaintext. A wrong key reads nothing. `ensure_workspace` does not touch tokens. Revoke and reinstall work. |
| Flow | The authorize URL is correct. The code exchange is correct. Stale, forged, and Enterprise installs are refused before the exchange. |
| Routes | `/slack/install` → Slack → `/slack/oauth_redirect` end to end through aiohttp, including the error pages |
| Fleet | Each workspace is built once. Reinstall replaces it. Unknown teams are ignored. |
| MCP | The OAuth state names its workspace, and a wrong key or expired state names none. |
| Serve | `_serve` with two stored installations builds two runtimes. `authorize` returns each team's own tokens. Awareness follows the installer's token. Uninstall revokes the stored tokens. |

Postgres was checked by hand against `pgvector/pgvector:pg16`: an existing `workspaces` table gains the columns, `init_schema` runs twice, and save, revoke, and reinstall work.
