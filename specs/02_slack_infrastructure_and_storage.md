# Specification 02: Slack Infrastructure and Dual-Memory Storage

## 1. Overview & Objectives

This specification details the Slack transport infrastructure and the persistence tier for Knappy. It provides:
1. **Slack App Topology:** Configuration for Socket Mode, event routing, and required minimal OAuth scopes.
2. **Dual-Memory Schema:** Complete relational and vector DDL for SQLite (local development/PoC) and PostgreSQL + `pgvector` (production).
3. **Database Client Contracts:** Typed Python interfaces for contacts, interaction history, and staged action drafts.

---

## 2. Slack App Configuration & Scopes

### 2.1 Slack Manifest (YAML)

```yaml
display_information:
  name: Knappy Assistant
  description: Ambient relationship and executive assistant for Slack.
  background_color: "#1A1D21"
features:
  bot_user:
    display_name: Knappy
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write              # Send direct messages and thread replies
      - im:history              # Read messages in direct message channels
      - im:read                 # Access direct message channel metadata
      - im:write                # Initiate private DM conversations with users
      - app_mentions:read       # Listen to @Knappy mentions in invited public/private channels
settings:
  socket_mode_enabled: true
  event_subscriptions:
    bot_events:
      - message.im
      - app_mention
  interactivity:
    is_enabled: true
```

### 2.2 Credentials & Token Hierarchy

The application requires three environment variables:
- `SLACK_BOT_TOKEN` (`xoxb-...`): Authorizes bot actions and API calls.
- `SLACK_APP_TOKEN` (`xapp-...`): Grants WebSocket connection access via Socket Mode (`connections:write`).
- `SLACK_SIGNING_SECRET`: Used for payload verification when transitioning to webhook mode.

---

## 3. Storage Layer: Dual-Memory Architecture

Knappy uses a **dual-memory paradigm**:
1. **Relational Layer:** Deterministic queries on contact metadata, last contact timestamps, reminder cadences, and staged action approvals.
2. **Dense Vector Layer:** 384-dimensional dense vectors for semantic similarity search over meeting notes, conversation summaries, and commitments.

### 3.1 PostgreSQL + `pgvector` Schema (Production)

```sql
-- Enable UUID and Vector extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

-- 1. Workspaces Table
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,                       -- Slack Team ID (e.g. T01234567)
    team_name TEXT NOT NULL,
    bot_token TEXT NOT NULL,
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Contacts Table
CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slack_user_id TEXT,                        -- Slack User ID if internal colleague (e.g. U01234567)
    email TEXT,
    company TEXT,
    role TEXT,
    reminder_cadence_days INTEGER DEFAULT 30,  -- Days before flagging relationship as dormant
    last_interaction_ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_workspace_contact_name UNIQUE(workspace_id, name)
);

-- 3. Interactions Table
CREATE TABLE IF NOT EXISTS interactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('DIRECT_DM', 'APP_MENTION', 'NOTE_INGEST')),
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    commitment TEXT,                           -- e.g. "Send budget deck by Friday"
    due_date TIMESTAMP WITH TIME ZONE,         -- Extracted deadline
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'EXPIRED')),
    embedding VECTOR(384),                     -- Generated via bge-small-en-v1.5
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. Action Drafts (HITL Staged Actions)
CREATE TABLE IF NOT EXISTS action_drafts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                     -- Slack User ID authorized to approve
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ('SEND_SLACK_DM', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
    payload JSONB NOT NULL,                    -- Target recipient, message text, event times
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED')),
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours'),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP WITH TIME ZONE
);

-- Indexes for Fast Querying
CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_interactions_embedding ON interactions USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
```

### 3.2 SQLite Local Development Schema (`sqlite-vec` or In-Memory Cosine)

For local development without Docker or Postgres, a compatible SQLite schema is supported:

```sql
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default_ws',
    name TEXT NOT NULL UNIQUE,
    slack_user_id TEXT,
    email TEXT,
    company TEXT,
    role TEXT,
    reminder_cadence_days INTEGER DEFAULT 30,
    last_interaction_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default_ws',
    contact_id TEXT REFERENCES contacts(id),
    source_type TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    commitment TEXT,
    due_date DATETIME,
    status TEXT NOT NULL DEFAULT 'PENDING',
    embedding BLOB,                            -- Serialized float array for sqlite-vec or numpy
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS action_drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'default_ws',
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL,
    payload TEXT NOT NULL,                     -- Serialized JSON string
    status TEXT NOT NULL DEFAULT 'PENDING',
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME
);
```

---

## 4. Scope Boundaries for Infrastructure & Storage

### In-Scope
- Socket Mode event router using `slack_bolt.App` and `SocketModeHandler`.
- Asynchronous database connection pooling (e.g. `aiosqlite` for SQLite, `asyncpg` or SQLAlchemy async for PostgreSQL).
- Abstract Database Repository interface so business logic remains agnostic to SQLite vs. PostgreSQL.
- Fast database transactions ensuring atomic inserts of contacts and interaction logs.

### Out-of-Scope
- Public Webhook endpoints requiring SSL certificates, reverse proxies, or DNS configuration for local development.
- Multi-region database replication or distributed sharding.
- User authentication screens; authentication is handled implicitly by Slack's verified workspace context.

---

## 5. Verification Plan

| Test Case ID | Scope | Verification Procedure | Expected Outcome |
| :--- | :--- | :--- | :--- |
| **TEST-INFRA-01** | Slack Auth | Initialize `SocketModeHandler(app, SLACK_APP_TOKEN)` with valid tokens. | Terminal outputs `⚡️ Bolt app is running!` without authentication rejection. |
| **TEST-INFRA-02** | Message Ingress | Send test DM to bot in development workspace. | Event listener acknowledges message within $< 500\text{ ms}$; no retry events from Slack. |
| **TEST-INFRA-03** | Schema Initialization | Run DB setup script on clean SQLite and PostgreSQL instances. | All 4 tables created with indexes; no syntax errors. |
| **TEST-INFRA-04** | Vector Serialization | Insert 384-dimensional float vector into `interactions` and run cosine distance query. | Nearest neighbor query retrieves the inserted record with cosine distance $\approx 0.0$. |
| **TEST-INFRA-05** | Cascading Deletes | Delete a contact record from `contacts`. | Associated records in `interactions` are cleanly deleted by cascade without orphan rows. |
