"""SQLite and PostgreSQL DDL for the dual-memory store."""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
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
    owner_user_id TEXT NOT NULL DEFAULT '',
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
    embedding BLOB,
    last_alerted_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS action_drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ('SEND_SLACK_DM', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME
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
    owner_user_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
"""

POSTGRES_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    team_name TEXT NOT NULL,
    bot_token TEXT NOT NULL,
    installed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slack_user_id TEXT,
    email TEXT,
    company TEXT,
    role TEXT,
    reminder_cadence_days INTEGER DEFAULT 30,
    last_interaction_ts TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL DEFAULT '',
    CONSTRAINT uq_workspace_contact_name UNIQUE(workspace_id, owner_user_id, name)
);

CREATE TABLE IF NOT EXISTS interactions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK(source_type IN ('DIRECT_DM', 'APP_MENTION', 'NOTE_INGEST')),
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    raw_text TEXT NOT NULL,
    summary TEXT NOT NULL,
    commitment TEXT,
    due_date TIMESTAMP WITH TIME ZONE,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'FULFILLED', 'CANCELLED', 'EXPIRED')),
    embedding VECTOR(384),
    last_alerted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS action_drafts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ('SEND_SLACK_DM', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL')),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours'),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    executed_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS briefing_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('COMMITMENT', 'CADENCE')),
    interaction_id UUID REFERENCES interactions(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    summary TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED' CHECK(status IN ('QUEUED', 'DELIVERED', 'DISMISSED')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    owner_user_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_interactions_embedding ON interactions USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
"""

EXPECTED_TABLES = frozenset(
    {"workspaces", "contacts", "interactions", "action_drafts", "briefing_items"}
)
