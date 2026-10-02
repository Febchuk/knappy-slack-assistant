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
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slack_user_id TEXT,                        -- Slack User ID if internal colleague (e.g. U01234567)
    email TEXT,
    company TEXT,
    role TEXT,
    reminder_cadence_days INTEGER DEFAULT 30,  -- Days before flagging relationship as dormant
    last_interaction_ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL,               -- Slack user Knappy is assisting
    CONSTRAINT uq_workspace_contact_name UNIQUE(workspace_id, owner_user_id, name)
);

-- 3. Interactions Table
CREATE TABLE IF NOT EXISTS interactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
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
    last_alerted_at TIMESTAMP WITH TIME ZONE,  -- Last proactive alert; suppresses re-query churn
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL                -- Slack user Knappy is assisting
);

-- 4. Action Drafts (HITL Staged Actions)
CREATE TABLE IF NOT EXISTS action_drafts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                     -- Slack User ID authorized to approve
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ('SEND_SLACK_DM', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
    payload JSONB NOT NULL,                    -- Target recipient, message text, event times
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours'),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP WITH TIME ZONE       -- Set only after the external call succeeds
);

-- 5. Briefing Items (morning digest queue; cadence alerts have no interaction row)
CREATE TABLE IF NOT EXISTS briefing_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                     -- Slack user who receives the digest
    kind TEXT NOT NULL CHECK(kind IN ('COMMITMENT', 'CADENCE')),
    interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED', 'DELIVERED', 'DISMISSED')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL                -- Slack user who owns this briefing
);

-- Indexes for Fast Querying
CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_interactions_embedding ON interactions USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
```

### 3.2 SQLite Local Development Schema (`sqlite-vec` or In-Memory Cosine)

For local development without Docker or Postgres, SQLite uses the same logical model as PostgreSQL. Types are dialect-legal: TEXT ids, TEXT JSON, DATETIME timestamps, and a BLOB embedding (sqlite-vec or application-level cosine). There is no `hnsw` index.

```sql
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,                       -- Slack Team ID (e.g. T01234567)
    team_name TEXT NOT NULL,
    bot_token TEXT NOT NULL,
    installed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slack_user_id TEXT,
    email TEXT,
    company TEXT,
    role TEXT,
    reminder_cadence_days INTEGER DEFAULT 30,
    last_interaction_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL,               -- Slack user Knappy is assisting
    UNIQUE(workspace_id, owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('DIRECT_DM', 'APP_MENTION', 'NOTE_INGEST')),
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    commitment TEXT,
    due_date DATETIME,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'EXPIRED')),
    embedding BLOB,                            -- Serialized float32[384] for sqlite-vec or numpy cosine
    last_alerted_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL                -- Slack user Knappy is assisting
);

CREATE TABLE IF NOT EXISTS action_drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ('SEND_SLACK_DM', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
    payload TEXT NOT NULL,                     -- Serialized JSON string
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME                       -- Set only after the external call succeeds
);

CREATE TABLE IF NOT EXISTS briefing_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('COMMITMENT', 'CADENCE')),
    interaction_id TEXT REFERENCES interactions(id) ON DELETE CASCADE,
    contact_id TEXT REFERENCES contacts(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED', 'DELIVERED', 'DISMISSED')),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL                -- Slack user who owns this briefing
);

CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
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
| **TEST-INFRA-03** | Schema Initialization | Run DB setup script on clean SQLite and PostgreSQL instances. | All 5 tables (`workspaces`, `contacts`, `interactions`, `action_drafts`, `briefing_items`) created with indexes; no syntax errors. |
| **TEST-INFRA-04** | Vector Serialization | Insert 384-dimensional float vector into `interactions` and run cosine distance query. | Nearest neighbor query retrieves the inserted record with cosine distance $\approx 0.0$. |
| **TEST-INFRA-05** | Cascading Deletes | Delete a contact record from `contacts`. | Associated records in `interactions` are cleanly deleted by cascade without orphan rows. |
