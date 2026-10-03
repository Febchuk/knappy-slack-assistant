"""SQLite and PostgreSQL DDL for the dual-memory store."""

ACTION_TYPES = "'SEND_SLACK_DM', 'SHARE_FILE', 'POST_THREAD_REPLY', 'GMAIL_DRAFT', 'CALENDAR_INVITE', 'POST_CHANNEL'"
PROVENANCE_SOURCES = "'turn', 'document', 'event', 'record', 'migration', 'slack_message'"


SQLITE_MEMORY_PROVENANCE = f"""CREATE TABLE IF NOT EXISTS memory_provenance (
    target_type TEXT NOT NULL CHECK(target_type IN ('event', 'record')),
    target_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ({PROVENANCE_SOURCES})),
    source_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, target_type, target_id, source_type, source_id)
);"""

# Spec 18. Raw message text is never stored: attention items and observations keep a summary and a permalink.
AWARENESS_TABLES = """
CREATE TABLE IF NOT EXISTS attention_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('asks_user', 'assigns_user', 'waiting_on_user')),
    summary TEXT NOT NULL,
    who TEXT,
    who_slack_id TEXT,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    thread_ts TEXT,
    source_ts TEXT NOT NULL,
    permalink TEXT,
    due_at TEXT,
    urgency TEXT NOT NULL CHECK(urgency IN ('low', 'today', 'now')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'ANSWERED', 'DONE', 'DISMISSED', 'SNOOZED')),
    snoozed_until TEXT,
    last_surfaced_at TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (owner_user_id, channel_id, source_ts)
);
CREATE INDEX IF NOT EXISTS idx_attention_open ON attention_items (workspace_id, owner_user_id, status);

CREATE TABLE IF NOT EXISTS awareness_excluded (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, owner_user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS awareness_cursors (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    PRIMARY KEY (workspace_id, owner_user_id, channel_id)
);
"""

# SQLite cannot alter a CHECK constraint, so the migration rebuilds the table from this definition.
SQLITE_ACTION_DRAFTS = f"""CREATE TABLE IF NOT EXISTS action_drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_ts TEXT,
    action_type TEXT NOT NULL CHECK(action_type IN ({ACTION_TYPES})),
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'APPROVED', 'CANCELLED', 'EXPIRED', 'FAILED')),
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    executed_at DATETIME
);"""

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
    owner_user_id TEXT NOT NULL DEFAULT '',
    next_check_at DATETIME,
    on_no_progress TEXT,
    waiting_on TEXT
);

""" + SQLITE_ACTION_DRAFTS + """

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

CREATE TABLE IF NOT EXISTS model_usage (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, owner_user_id, day)
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    slack_ts TEXT,
    created_at DATETIME NOT NULL,
    reconciled_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_turns_conv ON conversation_turns (owner_user_id, conversation_key, seq);
CREATE INDEX IF NOT EXISTS idx_turns_unreconciled ON conversation_turns (owner_user_id, reconciled_at) WHERE reconciled_at IS NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    content, content='conversation_turns', content_rowid='seq', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS turns_fts_insert AFTER INSERT ON conversation_turns BEGIN
    INSERT INTO turns_fts (rowid, content) VALUES (new.seq, new.content);
END;
CREATE TRIGGER IF NOT EXISTS turns_fts_delete AFTER DELETE ON conversation_turns BEGIN
    INSERT INTO turns_fts (turns_fts, rowid, content) VALUES ('delete', old.seq, old.content);
END;

CREATE TABLE IF NOT EXISTS memory_records (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN (
        'person', 'org', 'fact', 'preference', 'decision',
        'workstream', 'episode_daily', 'episode_weekly', 'document')),
    title TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    links TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    contact_id TEXT REFERENCES contacts(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'SUPERSEDED', 'FORGOTTEN', 'EXPIRED')),
    supersedes TEXT,
    valid_from DATETIME NOT NULL,
    expires_at DATETIME,
    updated_at DATETIME NOT NULL,
    embedding BLOB,
    UNIQUE (workspace_id, owner_user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_records_owner ON memory_records (owner_user_id, status, updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, aliases, body, content='memory_records', content_rowid='seq', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_fts (rowid, title, aliases, body) VALUES (new.seq, new.title, new.aliases, new.body);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, title, aliases, body) VALUES ('delete', old.seq, old.title, old.aliases, old.body);
END;
CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE OF title, aliases, body ON memory_records BEGIN
    INSERT INTO memory_fts (memory_fts, rowid, title, aliases, body) VALUES ('delete', old.seq, old.title, old.aliases, old.body);
    INSERT INTO memory_fts (rowid, title, aliases, body) VALUES (new.seq, new.title, new.aliases, new.body);
END;

CREATE TABLE IF NOT EXISTS user_profile (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    timezone TEXT,
    generated_at DATETIME NOT NULL,
    nightly_on TEXT,
    PRIMARY KEY (workspace_id, owner_user_id)
);

CREATE TABLE IF NOT EXISTS conversation_recaps (
    owner_user_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    body TEXT NOT NULL,
    through_turn_id TEXT NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (owner_user_id, conversation_key)
);

CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'learned', 'changed', 'commitment_made', 'commitment_progress',
        'commitment_done', 'decision', 'document_added', 'forgotten')),
    summary TEXT NOT NULL,
    occurred_at DATETIME NOT NULL,
    admission_score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'RETRACTED')),
    commitment_id TEXT,
    created_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_owner_time ON memory_events (owner_user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_commitment ON memory_events (commitment_id) WHERE commitment_id IS NOT NULL;

""" + SQLITE_MEMORY_PROVENANCE + """
CREATE INDEX IF NOT EXISTS idx_prov_source ON memory_provenance (owner_user_id, source_type, source_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    slack_file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    text TEXT,
    summary TEXT,
    conversation_key TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE (workspace_id, owner_user_id, slack_file_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents (workspace_id, owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS document_chunks (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB,
    PRIMARY KEY (document_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';
""" + AWARENESS_TABLES

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
    action_type TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS model_usage (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, owner_user_id, day)
);

CREATE INDEX IF NOT EXISTS idx_contacts_cadence ON contacts (workspace_id, last_interaction_ts);
CREATE INDEX IF NOT EXISTS idx_interactions_due ON interactions (status, due_date) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions (contact_id);
CREATE INDEX IF NOT EXISTS idx_interactions_embedding ON interactions USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_action_drafts_pending ON action_drafts (user_id, status) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_briefing_items_queued ON briefing_items (workspace_id, status) WHERE status = 'QUEUED';

ALTER TABLE interactions ADD COLUMN IF NOT EXISTS next_check_at TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS on_no_progress TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS waiting_on TEXT;
ALTER TABLE interactions ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMP WITH TIME ZONE;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_alerted_at TIMESTAMP WITH TIME ZONE;

CREATE TABLE IF NOT EXISTS conversation_turns (
    seq BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    slack_ts TEXT,
    created_at TEXT NOT NULL,
    reconciled_at TEXT,
    search TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);
CREATE INDEX IF NOT EXISTS idx_turns_conv ON conversation_turns (owner_user_id, conversation_key, seq);
CREATE INDEX IF NOT EXISTS idx_turns_unreconciled ON conversation_turns (owner_user_id, reconciled_at) WHERE reconciled_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_turns_search ON conversation_turns USING GIN (search);

CREATE TABLE IF NOT EXISTS memory_records (
    seq BIGSERIAL PRIMARY KEY,
    id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN (
        'person', 'org', 'fact', 'preference', 'decision',
        'workstream', 'episode_daily', 'episode_weekly', 'document')),
    title TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    links TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL,
    contact_id TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'SUPERSEDED', 'FORGOTTEN', 'EXPIRED')),
    supersedes TEXT,
    valid_from TEXT NOT NULL,
    expires_at TEXT,
    updated_at TEXT NOT NULL,
    embedding BYTEA,
    search TSVECTOR GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title || ' ' || aliases), 'A') || setweight(to_tsvector('english', body), 'D')
    ) STORED,
    UNIQUE (workspace_id, owner_user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_records_owner ON memory_records (owner_user_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_records_search ON memory_records USING GIN (search);

CREATE TABLE IF NOT EXISTS user_profile (
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    body TEXT NOT NULL,
    timezone TEXT,
    generated_at TEXT NOT NULL,
    nightly_on TEXT,
    PRIMARY KEY (workspace_id, owner_user_id)
);
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS brief_on TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS nudges_on TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS nudges_sent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS conversation_recaps (
    owner_user_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    body TEXT NOT NULL,
    through_turn_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, conversation_key)
);

CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'learned', 'changed', 'commitment_made', 'commitment_progress',
        'commitment_done', 'decision', 'document_added', 'forgotten')),
    summary TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    admission_score DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'RETRACTED')),
    commitment_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_owner_time ON memory_events (owner_user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_commitment ON memory_events (commitment_id) WHERE commitment_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_provenance (
    target_type TEXT NOT NULL CHECK(target_type IN ('event', 'record')),
    target_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN (""" + PROVENANCE_SOURCES + """)),
    source_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, target_type, target_id, source_type, source_id)
);
CREATE INDEX IF NOT EXISTS idx_prov_source ON memory_provenance (owner_user_id, source_type, source_id);

ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS metadata TEXT;
ALTER TABLE memory_provenance DROP CONSTRAINT IF EXISTS memory_provenance_source_type_check;
ALTER TABLE memory_provenance ADD CONSTRAINT memory_provenance_source_type_check CHECK(source_type IN (""" + PROVENANCE_SOURCES + """));

ALTER TABLE action_drafts DROP CONSTRAINT IF EXISTS action_drafts_action_type_check;
ALTER TABLE action_drafts ADD CONSTRAINT action_drafts_action_type_check CHECK(action_type IN (""" + ACTION_TYPES + """));

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    slack_file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    text TEXT,
    summary TEXT,
    conversation_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, owner_user_id, slack_file_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents (workspace_id, owner_user_id, created_at);

CREATE TABLE IF NOT EXISTS document_chunks (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BYTEA,
    PRIMARY KEY (document_id, seq)
);
""" + AWARENESS_TABLES

EXPECTED_TABLES = frozenset(
    {
        "workspaces", "contacts", "interactions", "action_drafts", "briefing_items", "model_usage",
        "conversation_turns", "memory_records", "user_profile", "conversation_recaps",
        "memory_events", "memory_provenance", "documents", "document_chunks",
        "attention_items", "awareness_excluded", "awareness_cursors",
    }
)
