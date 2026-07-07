"""
AgenticOS — Unified SQLite Schema
==================================
Consolidates data from:
  - hermes sessions export (primary)
  - hermes insights (via computed metrics)
  - hermes kanban
  - hermes cron list
  - Existing JSONL collector snapshots
  - External cost data (receipts, OpenRouter API)

Used by the Flask dashboard and the collector/aggregator pipeline.
"""

# ── Schema SQL ────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Sessions from hermes sessions export
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL DEFAULT 'unknown',
    model           TEXT NOT NULL DEFAULT 'unknown',
    profile         TEXT NOT NULL DEFAULT 'default',
    
    -- Token counts
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    reasoning_tokens    INTEGER DEFAULT 0,
    total_tokens        INTEGER GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,
    
    -- Timing
    started_at      TEXT,
    ended_at        TEXT,
    duration_seconds INTEGER DEFAULT 0,
    
    -- Activity
    message_count   INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    api_call_count  INTEGER DEFAULT 0,
    rewind_count    INTEGER DEFAULT 0,
    
    -- Cost
    billing_provider    TEXT,
    billing_mode        TEXT,           -- 'flat-rate', 'payg', 'free'
    estimated_cost_usd  REAL DEFAULT 0.0,
    actual_cost_usd     REAL,           -- NULL until verified
    cost_status         TEXT,           -- 'estimated', 'verified', 'reconciled'
    cost_source         TEXT,
    pricing_version     TEXT,
    
    -- Platform info
    user_id         TEXT,
    chat_id         TEXT,
    thread_id       TEXT,
    session_key     TEXT,
    handoff_platform TEXT,
    handoff_state   TEXT,
    
    -- Git context
    git_branch      TEXT,
    git_repo_root   TEXT,
    
    -- Title + sync metadata
    title           TEXT,
    ended_reason    TEXT,
    archived        INTEGER DEFAULT 0,
    first_seen_at   TEXT DEFAULT (datetime('now')),
    last_updated_at TEXT DEFAULT (datetime('now'))
);

-- Sessions tag table (for flexible metadata)
CREATE TABLE IF NOT EXISTS session_tags (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (session_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag);

-- Provider snapshots (from existing JSONL tracking)
CREATE TABLE IF NOT EXISTS provider_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens      INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0.0,
    billing_mode TEXT,  -- 'flat-rate', 'payg', 'free'
    sessions    INTEGER DEFAULT 1,
    source_file TEXT
);
CREATE INDEX IF NOT EXISTS idx_prov_snap_ts ON provider_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_prov_snap_provider ON provider_snapshots(provider);

-- Skills tracking (from Hermes insights)
CREATE TABLE IF NOT EXISTS skills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    category    TEXT,
    description TEXT,
    loaded_count    INTEGER DEFAULT 0,
    edit_count      INTEGER DEFAULT 0,
    last_used_at    TEXT,
    success_count   INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    profile         TEXT DEFAULT 'default'
);

-- Cron jobs
CREATE TABLE IF NOT EXISTS cron_jobs (
    id          TEXT PRIMARY KEY,   -- Hermes cron job ID or unique name
    name        TEXT,
    machine     TEXT DEFAULT 'mac', -- 'mac', 'pi'
    schedule    TEXT,               -- cron expression
    description TEXT,
    skill       TEXT,               -- linked skill name
    status      TEXT DEFAULT 'active', -- 'active', 'paused', 'error'
    created_at  TEXT,
    
    -- Runtime tracking
    last_run_at     TEXT,
    last_output     TEXT,
    last_error      TEXT,
    last_duration_seconds INTEGER,
    success_count   INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    
    -- Platform
    deliver_to  TEXT,
    script_path TEXT
);

-- Cron job run history
CREATE TABLE IF NOT EXISTS cron_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,       -- 'success', 'failure', 'timeout', 'error'
    output      TEXT,
    error_msg   TEXT,
    tokens      INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    triggered_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_runs(job_id);

-- Kanban tasks
CREATE TABLE IF NOT EXISTS kanban_tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT,
    status      TEXT DEFAULT 'todo', -- 'triage','todo','ready','running','blocked','done'
    priority    TEXT DEFAULT 'medium',
    assignee    TEXT,
    profile     TEXT,
    
    -- Dependencies
    parent_id   TEXT,               -- parent task ID
    linked_session_id TEXT,         -- session that worked on this
    
    -- Timing
    created_at  TEXT,
    started_at  TEXT,
    completed_at TEXT,
    due_at      TEXT,               -- schedule/blocked until
    
    -- Metrics
    tags        TEXT,               -- comma-separated
    board_slug  TEXT DEFAULT 'default'
);

-- Cost receipts (from actual invoices/subscriptions)
CREATE TABLE IF NOT EXISTS cost_receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT DEFAULT 'USD',
    date        TEXT NOT NULL,
    description TEXT,
    category    TEXT DEFAULT 'subscription', -- 'subscription', 'payg', 'prepaid'
    receipt_source TEXT,             -- 'manual', 'openrouter-api', 'email'
    verified    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cost_receipts_provider ON cost_receipts(provider);
CREATE INDEX IF NOT EXISTS idx_cost_receipts_date ON cost_receipts(date);

-- Monthly cost rollups (computed)
CREATE TABLE IF NOT EXISTS monthly_costs (
    year_month  TEXT NOT NULL,       -- '2026-06'
    provider    TEXT NOT NULL,
    billing_mode TEXT,               -- 'flat-rate', 'payg', 'mixed'
    session_count   INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    estimated_cost  REAL DEFAULT 0.0,
    actual_cost     REAL DEFAULT 0.0,
    receipt_cost    REAL,            -- from cost_receipts
    reconcilation_note TEXT,
    PRIMARY KEY (year_month, provider)
);

-- Daily cost snapshots (for trends)
CREATE TABLE IF NOT EXISTS daily_costs (
    date        TEXT NOT NULL,       -- '2026-07-04'
    provider    TEXT NOT NULL,
    model       TEXT,
    session_count   INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    estimated_cost  REAL DEFAULT 0.0,
    PRIMARY KEY (date, provider, model)
);

-- Sync metadata (tracking last collection times)
CREATE TABLE IF NOT EXISTS sync_meta (
    source      TEXT PRIMARY KEY,    -- 'hermes-sessions', 'hermes-insights', 'jsonl-snapshots', etc.
    last_sync_at TEXT,
    records_count INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'ok'
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT DEFAULT (datetime('now')),
    description TEXT
);
INSERT OR IGNORE INTO schema_version (version, description) VALUES (1, 'Initial schema: sessions, skills, cron, kanban, costs, sync_meta');
""".strip()
