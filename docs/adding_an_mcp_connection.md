# Adding an MCP connection

Knappy acts in other tools through remote MCP servers. Each server is one entry in `mcp_servers.toml`, and adding one needs no Python changes. Each user connects their own account from a Slack DM. Reads run freely. Every write shows an approval card first. Design: [Spec 19](../specs/19_mcp_connections.md), [Spec 20](../specs/20_acting_through_mcp.md), [Spec 21](../specs/21_launch_connections.md).

## Before you start

MCP is off unless both of these are set in `.env`:

```sh
KNAPPY_PUBLIC_URL=https://<the HTTPS origin that reaches port 8080>
KNAPPY_SECRET_KEY=<from the command below>
```

Generate the secret once and keep it. If you change it, every stored token becomes unreadable and users must reconnect.

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The OAuth redirect URI is `${KNAPPY_PUBLIC_URL}/oauth/callback`. Knappy serves it on `KNAPPY_CALLBACK_PORT`, which defaults to `8080`.

## Steps

1. **Probe the server.**

   ```sh
   python -m knappy.mcp probe https://mcp.example.com
   ```

   This reads the server's OAuth metadata and prints a ready-to-paste `[[server]]` entry. The entry includes the auth mode, the advertised scopes, and a note when the server cannot refresh tokens. If the probe fails, the URL is probably not the MCP endpoint. Check the server's docs for the exact path. Google's Gmail and Calendar servers, for example, live at `/mcp/v1`.

2. **Paste the entry into `mcp_servers.toml`.**
   - Rename `name` if you like. Tools reach the model as `<name>__<tool>`.
   - Trim `scopes` to what Knappy needs.
   - Deny tools Knappy should never offer with `tools = { allow = ["*"], deny = ["..."] }`.

3. **Add env vars, but only for `oauth_static`.** Servers with dynamic client registration (`oauth_dcr`) register themselves. A server without it needs an OAuth client you create in the provider's console. Its redirect URI is `${KNAPPY_PUBLIC_URL}/oauth/callback`. Put the client's id and secret in `.env` under the names the entry gives in `client_id_env` and `client_secret_env`.

4. **Restart, connect, and finish the globs.**
   - Restart Knappy.
   - In a **DM** with Knappy, say "connect <name>". Knappy only sends connect links in DMs, because a link binds the consent to whoever asked.
   - Open the link and approve. Knappy DMs you "<App> connected."
   - Then run:

     ```sh
     python -m knappy.mcp tools <name> --user <your Slack user id>
     ```

     This prints every tool with its `readOnlyHint`, how Knappy classifies it, whether Gemini accepts it, and its arguments.
   - Read the suggested `read`, `write`, and `body_field` lines against each tool's description. Paste the ones you agree with into the entry, then restart.

   A tool without `readOnlyHint: true` is treated as a write until a `read` glob says otherwise. `body_field` names the argument the approval card shows as the editable body. Without it, the card shows the arguments as JSON, which you can also edit.

   Your Slack user id is under your Slack profile, in the menu, as "Copy member ID".

## Google (Gmail, Calendar, BigQuery)

Google has no dynamic registration, so one OAuth web client covers all three servers. One consent connects all three, because they share `auth_group = "google"`.

1. **Create a GCP project** inside your Google Workspace organization:

   ```sh
   gcloud projects create <project-id>
   ```

2. **Enable the APIs and MCP services.** The service names come from Google's [Workspace MCP guide](https://developers.google.com/workspace/guides/configure-mcp-servers) and [BigQuery MCP guide](https://docs.cloud.google.com/bigquery/docs/use-bigquery-mcp):

   ```sh
   gcloud services enable gmail.googleapis.com calendar-json.googleapis.com bigquery.googleapis.com --project=<project-id>
   gcloud services enable gmailmcp.googleapis.com calendarmcp.googleapis.com --project=<project-id>
   gcloud beta services mcp enable bigquery.googleapis.com --project=<project-id>
   ```

3. **Set the OAuth consent screen to Internal.** In the console, go to Google Auth Platform, then Audience, and choose **Internal**. Gmail scopes are *restricted*. An Internal app is limited to your Workspace users and needs no Google verification. An External app needs Google's verification and a security assessment before anyone outside the test-user list can use it. Under Data Access, add the scopes from `mcp_servers.toml`:
   - `gmail.readonly`
   - `gmail.compose`
   - `calendar.events`
   - `calendar.calendarlist.readonly`
   - `calendar.events.freebusy`
   - `bigquery`

4. **Create the client.** Go to Google Auth Platform, then Clients, then Create client. Choose **Web application** and add the authorized redirect URI `${KNAPPY_PUBLIC_URL}/oauth/callback`. Then put the client's values in `.env`:

   ```sh
   GOOGLE_OAUTH_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```

5. **Grant BigQuery roles.** Each user who queries needs `roles/mcp.toolUser`, `roles/bigquery.jobUser`, and `roles/bigquery.dataViewer` on the project they query. Grant them for a user like this:

   ```sh
   for role in roles/mcp.toolUser roles/bigquery.jobUser roles/bigquery.dataViewer; do
     gcloud projects add-iam-policy-binding <project-id> --member=user:<email> --role=$role
   done
   ```

   Not confirmed: whether Gmail and Calendar MCP calls also need `roles/mcp.toolUser`. Google's Workspace guide does not list it. If a Gmail or Calendar call fails with a permission error after consent, grant it on the OAuth client's project.

Notes:
- Gmail's MCP server cannot send mail. Knappy creates a draft, and you send it from Gmail.
- BigQuery tools take a `projectId`, so tell Knappy which project to use.

## Running locally behind a tunnel

The OAuth redirect must reach your machine over HTTPS.

```sh
cloudflared tunnel --url http://localhost:8080
```

`cloudflared` prints an `https://<random>.trycloudflare.com` URL. Set that URL as `KNAPPY_PUBLIC_URL` in `.env`, then start Knappy:

```sh
python -m knappy.main
```

A quick tunnel gets a new URL every run. Each time the URL changes:
- Update `KNAPPY_PUBLIC_URL`.
- Update the Google client's redirect URI.
- Expect DCR servers to re-register on the next connect. Knappy does this automatically.

Existing connections keep working, because tokens do not depend on the redirect URI. A named Cloudflare tunnel keeps one hostname and avoids this.

## Deploying

The `Dockerfile` copies `mcp_servers.toml`, exposes `8080`, and runs one process. Point your host's HTTPS ingress at port 8080, and set `KNAPPY_PUBLIC_URL` to that origin. Run exactly one instance. Socket Mode and the in-memory tool cache both assume a single instance.
