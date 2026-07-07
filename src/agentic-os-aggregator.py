#!/usr/bin/env python3
"""
AgenticOS Aggregator — Pi-side
================================
Reads JSONL snapshot files (from Mac collector) + provider tracking data,
builds and updates the unified SQLite database for the AgenticOS dashboard.

Idempotent: tracks last-processed file mtimes and session IDs.

Usage:
    python3 agentic-os-aggregator.py                        # incremental update
    python3 agentic-os-aggregator.py --full                  # full rebuild
    python3 agentic-os-aggregator.py --db PATH               # custom DB path
    python3 agentic-os-aggregator.py --verbose               # verbose logging

Directory structure expected:
    ~/usage-analytics/snapshots/hermes/*.jsonl       (from Mac session collector)
    ~/usage-analytics/provider_tracking.jsonl          (from Pi provider collector)
    ~/usage-analytics/costs/cost_history.json          (receipt data)
    ~/usage-analytics/aggregated/daily/*.json          (daily aggregates)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sqlite3
import time
import argparse
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(os.path.expanduser("~/usage-analytics"))
DEFAULT_DB = Path(os.path.expanduser("~/usage-dashboard/agentic_os.db"))
STATE_DIR = Path(os.path.expanduser("~/usage-analytics/aggregated/.state"))

# Input directories
SNAPSHOTS_HERMES = BASE_DIR / "snapshots" / "hermes"
PROVIDER_TRACKING = BASE_DIR / "provider_tracking.jsonl"
COST_HISTORY = BASE_DIR / "costs" / "cost_history.json"

# OpenRouter live costs cache
OPENROUTER_LIVE_COSTS = BASE_DIR / "costs" / "openrouter_live.json"

# OpenRouter API
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/auth/key"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("agentic-os-aggregator")


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Sessions (from hermes sessions export via JSONL)
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL DEFAULT 'unknown',
    model           TEXT NOT NULL DEFAULT 'unknown',
    profile         TEXT NOT NULL DEFAULT 'default',

    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    reasoning_tokens    INTEGER DEFAULT 0,

    started_at      TEXT,
    ended_at        TEXT,
    duration_seconds INTEGER DEFAULT 0,

    message_count   INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    api_call_count  INTEGER DEFAULT 0,

    billing_provider    TEXT,
    billing_mode        TEXT,
    estimated_cost_usd  REAL DEFAULT 0.0,
    actual_cost_usd     REAL,
    cost_status         TEXT DEFAULT 'estimated',
    cost_source         TEXT,

    user_id         TEXT,
    chat_id         TEXT,
    thread_id       TEXT,
    handoff_platform TEXT,
    handoff_state   TEXT,

    git_branch      TEXT,
    git_repo_root   TEXT,

    title           TEXT,
    ended_reason    TEXT,
    archived        INTEGER DEFAULT 0,
    collected_at    TEXT DEFAULT (datetime('now')),
    last_updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_model ON sessions(model);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile);

-- Provider snapshots (from JSONL tracking)
CREATE TABLE IF NOT EXISTS provider_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    tokens      INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0.0,
    billing_mode TEXT,
    sessions    INTEGER DEFAULT 1,
    source_file TEXT,
    session_id  TEXT,
    profile     TEXT DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_prov_snap_ts ON provider_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_prov_snap_provider ON provider_snapshots(provider);

-- Skills (from Hermes insights / usage data)
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
    id          TEXT PRIMARY KEY,
    name        TEXT,
    machine     TEXT DEFAULT 'mac',
    schedule    TEXT,
    description TEXT,
    skill       TEXT,
    status      TEXT DEFAULT 'active',
    created_at  TEXT,
    last_run_at     TEXT,
    last_output     TEXT,
    last_error      TEXT,
    last_duration_seconds INTEGER,
    success_count   INTEGER DEFAULT 0,
    error_count     INTEGER DEFAULT 0,
    deliver_to  TEXT
);

-- Cron runs history
CREATE TABLE IF NOT EXISTS cron_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES cron_jobs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
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
    status      TEXT DEFAULT 'todo',
    priority    TEXT DEFAULT 'medium',
    assignee    TEXT,
    profile     TEXT,
    parent_id   TEXT,
    linked_session_id TEXT,
    created_at  TEXT,
    started_at  TEXT,
    completed_at TEXT,
    due_at      TEXT,
    tags        TEXT,
    board_slug  TEXT DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_kanban_status ON kanban_tasks(status);
CREATE INDEX IF NOT EXISTS idx_kanban_assignee ON kanban_tasks(assignee);

-- Cost receipts
CREATE TABLE IF NOT EXISTS cost_receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    amount      REAL NOT NULL,
    currency    TEXT DEFAULT 'USD',
    date        TEXT NOT NULL,
    description TEXT,
    category    TEXT DEFAULT 'subscription',
    receipt_source TEXT,
    verified    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cost_receipts_provider ON cost_receipts(provider);
CREATE INDEX IF NOT EXISTS idx_cost_receipts_date ON cost_receipts(date);

-- Monthly cost rollups (computed)
CREATE TABLE IF NOT EXISTS monthly_costs (
    year_month  TEXT NOT NULL,
    provider    TEXT NOT NULL,
    billing_mode TEXT,
    session_count   INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    estimated_cost  REAL DEFAULT 0.0,
    actual_cost     REAL DEFAULT 0.0,
    receipt_cost    REAL,
    PRIMARY KEY (year_month, provider)
);

-- Daily cost snapshots
CREATE TABLE IF NOT EXISTS daily_costs (
    date        TEXT NOT NULL,
    provider    TEXT NOT NULL,
    model       TEXT,
    session_count   INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    estimated_cost  REAL DEFAULT 0.0,
    PRIMARY KEY (date, provider, model)
);

-- OpenRouter live cost snapshots (fetched from API)
CREATE TABLE IF NOT EXISTS openrouter_live_costs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at TEXT NOT NULL,
    label       TEXT,
    total_usage REAL DEFAULT 0.0,
    limit_amt   REAL,
    is_free_tier INTEGER DEFAULT 0,
    rate_limit_raw TEXT,
    raw_response TEXT,
    cost_json TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- Sync metadata
CREATE TABLE IF NOT EXISTS sync_meta (
    source      TEXT PRIMARY KEY,
    last_sync_at TEXT,
    records_count INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'ok'
);

-- Schema version
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT DEFAULT (datetime('now')),
    description TEXT
);
INSERT OR IGNORE INTO schema_version (version, description) VALUES (1, 'Initial unified AgenticOS schema');
INSERT OR IGNORE INTO schema_version (version, description) VALUES (2, 'Added session_id, profile to provider_snapshots');
""".strip()


# ── Database Helpers ──────────────────────────────────────────────────────────


def get_db(db_path: str) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(db_path: str):
    """Create tables if they don't exist."""
    conn = get_db(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    logger.info("Schema ensured in %s", db_path)


# ── Session Importer ──────────────────────────────────────────────────────────


def parse_timestamp(ts_str: str | None) -> str | None:
    """Normalize a timestamp string to ISO format with Z suffix."""
    if not ts_str:
        return None
    # If it's a unix timestamp (numeric)
    if isinstance(ts_str, (int, float)) or (isinstance(ts_str, str) and ts_str.replace('.', '', 1).isdigit()):
        try:
            return datetime.fromtimestamp(float(ts_str), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError):
            return ts_str
    # Already ISO-ish
    ts = ts_str.strip()
    if ts.endswith("Z"):
        return ts
    if "+" in ts or "-" in ts[10:]:
        # Has timezone offset
        return ts
    return ts + "Z"


def import_session_records(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Import normalized session records into the sessions table. Returns count."""
    count = 0
    cur = conn.cursor()

    for rec in records:
        sid = rec.get("id")
        if not sid:
            continue

        # Check if already exists
        cur.execute("SELECT id FROM sessions WHERE id = ?", (sid,))
        if cur.fetchone():
            # Update
            cur.execute("""
                UPDATE sessions SET
                    source=?, model=?, profile=?,
                    input_tokens=?, output_tokens=?, cache_read_tokens=?,
                    cache_write_tokens=?, reasoning_tokens=?,
                    started_at=?, ended_at=?, duration_seconds=?,
                    message_count=?, tool_call_count=?, api_call_count=?,
                    billing_provider=?, billing_mode=?,
                    estimated_cost_usd=?, actual_cost_usd=?, cost_status=?,
                    cost_source=?,
                    user_id=?, chat_id=?, thread_id=?,
                    handoff_platform=?, handoff_state=?,
                    git_branch=?, git_repo_root=?,
                    title=?, ended_reason=?, archived=?,
                    last_updated_at=datetime('now')
                WHERE id=?
            """, (
                rec.get("source", "unknown"),
                rec.get("model", "unknown"),
                rec.get("profile", "default"),
                rec.get("input_tokens", 0),
                rec.get("output_tokens", 0),
                rec.get("cache_read_tokens", 0),
                rec.get("cache_write_tokens", 0),
                rec.get("reasoning_tokens", 0),
                parse_timestamp(rec.get("started_at")),
                parse_timestamp(rec.get("ended_at")),
                rec.get("duration_seconds", 0),
                rec.get("message_count", 0),
                rec.get("tool_call_count", 0),
                rec.get("api_call_count", 0),
                rec.get("billing_provider", ""),
                rec.get("billing_mode", ""),
                rec.get("estimated_cost_usd", 0.0),
                rec.get("actual_cost_usd"),
                rec.get("cost_status", "estimated"),
                rec.get("cost_source", ""),
                rec.get("user_id", ""),
                rec.get("chat_id", ""),
                rec.get("thread_id", ""),
                rec.get("handoff_platform", ""),
                rec.get("handoff_state", ""),
                rec.get("git_branch", ""),
                rec.get("git_repo_root", ""),
                rec.get("title", ""),
                rec.get("ended_reason", ""),
                rec.get("archived", 0),
                sid,
            ))
            count += 1
        else:
            # Insert
            cur.execute("""
                INSERT OR REPLACE INTO sessions (
                    id, source, model, profile,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_write_tokens, reasoning_tokens,
                    started_at, ended_at, duration_seconds,
                    message_count, tool_call_count, api_call_count,
                    billing_provider, billing_mode,
                    estimated_cost_usd, actual_cost_usd, cost_status,
                    cost_source,
                    user_id, chat_id, thread_id,
                    handoff_platform, handoff_state,
                    git_branch, git_repo_root,
                    title, ended_reason, archived
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sid,
                rec.get("source", "unknown"),
                rec.get("model", "unknown"),
                rec.get("profile", "default"),
                rec.get("input_tokens", 0),
                rec.get("output_tokens", 0),
                rec.get("cache_read_tokens", 0),
                rec.get("cache_write_tokens", 0),
                rec.get("reasoning_tokens", 0),
                parse_timestamp(rec.get("started_at")),
                parse_timestamp(rec.get("ended_at")),
                rec.get("duration_seconds", 0),
                rec.get("message_count", 0),
                rec.get("tool_call_count", 0),
                rec.get("api_call_count", 0),
                rec.get("billing_provider", ""),
                rec.get("billing_mode", ""),
                rec.get("estimated_cost_usd", 0.0),
                rec.get("actual_cost_usd"),
                rec.get("cost_status", "estimated"),
                rec.get("cost_source", ""),
                rec.get("user_id", ""),
                rec.get("chat_id", ""),
                rec.get("thread_id", ""),
                rec.get("handoff_platform", ""),
                rec.get("handoff_state", ""),
                rec.get("git_branch", ""),
                rec.get("git_repo_root", ""),
                rec.get("title", ""),
                rec.get("ended_reason", ""),
                rec.get("archived", 0),
            ))
            count += 1

    conn.commit()
    return count


def normalize_legacy_record(rec: dict) -> dict | None:
    """Convert an old-format JSONL record (from terminal-collect.sh) to new enriched format.

    Old format example:
        {"ts": "2026-05-20T22:01:55.109956", "session_id": "20260520_213149_91c438",
         "model": "gemini-3.5-flash", "provider": "gemini", "messages": 220,
         "estimated_tokens": 93508, "has_error": true, "source": "tui"}
    """
    sid = rec.get("session_id")
    if not sid:
        return None

    total_tokens = int(rec.get("estimated_tokens", 0) or rec.get("tokens", 0) or 0)
    # Estimate ~75% input / 25% output split if not specified
    inp = int(total_tokens * 0.75)
    out = total_tokens - inp

    return {
        "id": sid,
        "source": rec.get("source", "unknown"),
        "model": rec.get("model", "unknown"),
        "profile": rec.get("profile", "default"),
        "input_tokens": int(rec.get("input_tokens", 0) or inp),
        "output_tokens": int(rec.get("output_tokens", 0) or out),
        "cache_read_tokens": int(rec.get("cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(rec.get("cache_write_tokens", 0) or 0),
        "reasoning_tokens": int(rec.get("reasoning_tokens", 0) or 0),
        "started_at": rec.get("ts") or rec.get("timestamp") or rec.get("started_at", ""),
        "ended_at": rec.get("ended_at", ""),
        "duration_seconds": int(rec.get("duration_seconds", 0) or 0),
        "message_count": int(rec.get("messages", 0) or rec.get("message_count", 0) or 0),
        "tool_call_count": int(rec.get("tool_call_count", 0) or 0),
        "api_call_count": int(rec.get("api_call_count", 0) or 0),
        "billing_provider": rec.get("provider", "") or rec.get("billing_provider", ""),
        "billing_mode": rec.get("billing_mode", ""),
        "estimated_cost_usd": float(rec.get("estimated_cost_usd", 0.0) or 0.0),
        "actual_cost_usd": rec.get("actual_cost_usd"),
        "cost_status": rec.get("cost_status", "estimated"),
        "cost_source": rec.get("cost_source", ""),
        "title": rec.get("title", ""),
        "user_id": rec.get("user_id", ""),
        "chat_id": rec.get("chat_id", ""),
        "thread_id": rec.get("thread_id", ""),
        "handoff_platform": rec.get("handoff_platform", ""),
        "handoff_state": rec.get("handoff_state", ""),
        "git_branch": rec.get("git_branch", ""),
        "git_repo_root": rec.get("git_repo_root", ""),
        "ended_reason": rec.get("ended_reason", "") or ("error" if rec.get("has_error") else ""),
        "archived": 1 if rec.get("archived") else 0,
        "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def import_jsonl_snapshots(conn: sqlite3.Connection, snap_dir: Path) -> tuple[int, int]:
    """Import all JSONL files from snapshots directory. Returns (files_processed, records_imported).

    Handles two formats:
      - NEW (enriched): From hermes-session-collect.py (has 'id', 'input_tokens', etc.)
      - OLD (legacy): From previous collectors (has 'ts', 'session_id', 'estimated_tokens', 'messages', 'provider')
    """
    if not snap_dir.exists():
        logger.warning("Snapshots directory %s not found", snap_dir)
        return (0, 0)

    jsonl_files = sorted(snap_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.info("No JSONL files in %s", snap_dir)
        return (0, 0)

    total_files = 0
    total_records = 0

    for fpath in jsonl_files:
        try:
            with open(fpath) as f:
                records = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed JSON in %s", fpath)
                        continue

            if records:
                # Convert old-format records to new format for import
                if isinstance(records[0], dict) and ('session_id' in records[0] or 'ts' in records[0]):
                    records = [normalize_legacy_record(r) for r in records]
                    records = [r for r in records if r]  # remove None
                n = import_session_records(conn, records)
                total_records += n
                total_files += 1
                logger.debug("  %s: %d records (%d new)", fpath.name, len(records), n)
        except OSError as e:
            logger.warning("Could not read %s: %s", fpath, e)

    return (total_files, total_records)


def import_provider_tracking(conn: sqlite3.Connection, filepath: Path) -> int:
    """Import provider tracking data into provider_snapshots table. Returns count."""
    if not filepath.exists():
        logger.debug("Provider tracking %s not found", filepath)
        return 0

    count = 0
    cur = conn.cursor()

    # Check for existing data from this file
    source_name = filepath.name
    cur.execute("SELECT COUNT(*) FROM provider_snapshots WHERE source_file = ?", (source_name,))
    existing = cur.fetchone()[0]

    if existing > 0:
        logger.info("  Provider tracking already imported (%d records), skipping", existing)
        return 0

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            cur.execute("""
                INSERT INTO provider_snapshots
                    (timestamp, provider, model, tokens, input_tokens, output_tokens,
                     cost_usd, billing_mode, sessions, source_file, session_id, profile)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                parse_timestamp(rec.get("timestamp")),
                rec.get("provider", "unknown"),
                rec.get("model", "unknown"),
                rec.get("tokens", 0),
                rec.get("input_tokens", 0),
                rec.get("output_tokens", 0),
                rec.get("cost_usd", 0.0),
                rec.get("billing_mode", ""),
                rec.get("sessions", 1),
                source_name,
                rec.get("session_id", ""),
                rec.get("profile", "default"),
            ))
            count += 1

    conn.commit()
    return count


def import_cost_history(conn: sqlite3.Connection, filepath: Path) -> int:
    """Import cost receipts from cost_history.json. Returns count."""
    if not filepath.exists():
        logger.debug("Cost history %s not found", filepath)
        return 0

    try:
        with open(filepath) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not load cost history: %s", e)
        return 0

    receipts = data.get("receipts", [])
    if not receipts:
        return 0

    count = 0
    cur = conn.cursor()

    for rec in receipts:
        # Check for duplicate (by description + amount)
        desc = rec.get("description", "")
        amt = rec.get("amount", 0)
        cur.execute(
            "SELECT id FROM cost_receipts WHERE description = ? AND amount = ?",
            (desc, amt),
        )
        if cur.fetchone():
            continue

        cur.execute("""
            INSERT INTO cost_receipts
                (provider, amount, currency, date, description, category, receipt_source, verified)
            VALUES (?,?,?,?,?,?,?,1)
        """, (
            rec.get("provider", "unknown"),
            rec.get("amount", 0),
            rec.get("currency", "USD"),
            rec.get("date", ""),
            desc,
            rec.get("category", "payg"),
            rec.get("receipt_source", "manual"),
        ))
        count += 1

    conn.commit()
    return count


# ── OpenRouter Live Cost Fetch ────────────────────────────────────────────────


def fetch_openrouter_live_costs(conn: sqlite3.Connection) -> dict | None:
    """Fetch live cost data from OpenRouter API and store in DB + JSON cache.

    Calls GET /api/v1/auth/key to retrieve the current total usage
    (cumulative spend) associated with the API key.

    Returns the parsed data dict, or None on failure.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        # Fallback: read from ~/.hermes/.env
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("OPENROUTER_API_KEY="):
                            api_key = line.split("=", 1)[1].strip().strip("\"'")
                            break
            except OSError:
                pass
    if not api_key:
        logger.warning("  OPENROUTER_API_KEY not set — skipping live cost fetch")
        return None

    logger.info("  Fetching live costs from OpenRouter API...")
    try:
        req = urllib.request.Request(
            OPENROUTER_CREDITS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
        logger.warning("  OpenRouter API call failed: %s", e)
        return None

    key_data = data.get("data", data) if isinstance(data, dict) else {}
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Extract fields
    label = key_data.get("label", "")
    total_usage = float(key_data.get("usage", 0) or 0)
    limit_amt = key_data.get("limit")
    if limit_amt is not None:
        limit_amt = float(limit_amt)
    is_free = 1 if key_data.get("is_free_tier", False) else 0
    rate_limit = key_data.get("rate_limit", {})

    # Store in DB
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO openrouter_live_costs
           (snapshot_at, label, total_usage, limit_amt, is_free_tier,
            rate_limit_raw, raw_response, cost_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            now_iso,
            label,
            total_usage,
            limit_amt,
            is_free,
            json.dumps(rate_limit) if rate_limit else None,
            json.dumps(data, default=str)[:5000] if data else None,
            json.dumps(key_data, default=str)[:5000] if key_data else None,
        ),
    )
    conn.commit()

    # Also persist to JSON cache for the API endpoint
    try:
        OPENROUTER_LIVE_COSTS.parent.mkdir(parents=True, exist_ok=True)
        with open(OPENROUTER_LIVE_COSTS, "w") as f:
            json.dump({
                "snapshot_at": now_iso,
                "label": label,
                "total_usage": total_usage,
                "limit_amt": limit_amt,
                "is_free_tier": bool(is_free),
            }, f, indent=2)
    except OSError as e:
        logger.warning("  Could not write live cost cache: %s", e)

    logger.info("  OpenRouter live usage: $%.4f (limit: %s)", total_usage, limit_amt or "unlimited")
    return {
        "label": label,
        "total_usage": total_usage,
        "limit_amt": limit_amt,
        "is_free_tier": bool(is_free),
        "snapshot_at": now_iso,
    }


# ── Computed Rollups ──────────────────────────────────────────────────────────


def compute_daily_costs(conn: sqlite3.Connection):
    """Compute daily cost rollups from sessions table."""
    cur = conn.cursor()
    cur.execute("DELETE FROM daily_costs")

    cur.execute("""
        INSERT OR REPLACE INTO daily_costs (date, provider, model, session_count, total_tokens, estimated_cost)
        SELECT
            date(started_at) as day,
            COALESCE(NULLIF(billing_provider, ''), 'unknown') as provider,
            model,
            COUNT(*) as session_count,
            SUM(input_tokens + output_tokens) as total_tokens,
            SUM(COALESCE(estimated_cost_usd, 0.0)) as estimated_cost
        FROM sessions
        WHERE started_at IS NOT NULL
        GROUP BY day, provider, model
    """)
    conn.commit()
    logger.debug("Daily costs computed")


def compute_monthly_costs(conn: sqlite3.Connection):
    """Compute monthly cost rollups from sessions + receipts."""
    cur = conn.cursor()
    cur.execute("DELETE FROM monthly_costs")

    # From sessions
    cur.execute("""
        INSERT OR REPLACE INTO monthly_costs (year_month, provider, billing_mode, session_count, total_tokens, estimated_cost)
        SELECT
            strftime('%Y-%m', started_at) as ym,
            COALESCE(NULLIF(billing_provider, ''), 'unknown') as provider,
            COALESCE(NULLIF(billing_mode, ''), 'unknown') as billing_mode,
            COUNT(*) as session_count,
            SUM(input_tokens + output_tokens) as total_tokens,
            SUM(COALESCE(estimated_cost_usd, 0.0)) as estimated_cost
        FROM sessions
        WHERE started_at IS NOT NULL
        GROUP BY ym, provider, billing_mode
    """)
    conn.commit()

    # Add receipt costs
    cur.execute("""
        UPDATE monthly_costs SET
            receipt_cost = (
                SELECT SUM(amount) FROM cost_receipts
                WHERE cost_receipts.provider = monthly_costs.provider
                  AND strftime('%Y-%m', cost_receipts.date) = monthly_costs.year_month
            )
    """)
    conn.commit()
    logger.debug("Monthly costs computed")


# ── Cron Jobs Population ─────────────────────────────────────────────────────


def populate_cron_jobs(conn: sqlite3.Connection, crontab_path: str = "/proc/self/fd/1") -> int:
    """Read crontab via subprocess, parse cron expressions, populate cron_jobs table.

    Args:
        conn: Database connection.
        crontab_path: Ignored on the Pi; we always read via ``crontab -l``.
    Returns:
        Number of cron jobs upserted.
    """
    import re
    # Pattern for a standard 5-field cron expression + command
    cron_re = re.compile(
        r"^\s*"
        r"(@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly))\s+"
        r"|"
        r"("
        r"(?:\*|[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?)*)"
        r"\s+"
        r"(?:\*|[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?)*)"
        r"\s+"
        r"(?:\*|[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?)*)"
        r"\s+"
        r"(?:\*|[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?)*)"
        r"\s+"
        r"(?:\*|[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?(?:\s*,\s*[0-9]+(?:-[0-9]+)?(?:/[0-9]+)?)*)"
        r")\s+"
        r"(.+)$"
    )
    cur = conn.cursor()
    count = 0
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.warning("crontab -l failed (rc=%d): %s", result.returncode, result.stderr.strip())
            return 0
        lines = result.stdout.splitlines()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("Cannot read crontab: %s", e)
        return 0

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = cron_re.match(line)
        if not m:
            continue

        # Determine schedule
        at_alias = m.group(1)
        expr_fields = m.group(2)
        command = m.group(3) or m.group(1)  # group 3 for standard, group 1 for @alias
        if at_alias:
            schedule = at_alias
        else:
            # Extract the 5 fields from the matched expression
            fields = expr_fields.strip().split()
            schedule = " ".join(fields[:5]) if len(fields) >= 5 else expr_fields.strip()

        job_id = hashlib.md5(command.encode()).hexdigest()
        # Derive a human-readable name from the script name or command prefix
        command_stripped = command.strip()
        script_name = command_stripped.split()[-1] if command_stripped.split() else command_stripped
        name = Path(script_name).stem if "/" in script_name else script_name[:40]

        cur.execute("""
            INSERT OR REPLACE INTO cron_jobs
                (id, name, machine, schedule, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?)
        """, (job_id, name, "pi", schedule, command_stripped, now_iso))
        count += 1

    conn.commit()
    logger.info("  Populated %d cron jobs from Pi crontab", count)
    return count


# ── Skills Population ────────────────────────────────────────────────────────


def populate_skills(conn: sqlite3.Connection) -> int:
    """Derive skill-like entries from sessions (unique models, sources).

    Each unique model and source in the sessions table is recorded as a
    skill entry with its session count as loaded_count.
    """
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    count = 0

    # Skills from sources (platforms)
    cur.execute("""
        SELECT source, COUNT(*) as cnt
        FROM sessions
        WHERE source IS NOT NULL AND source != ''
        GROUP BY source
        ORDER BY cnt DESC
    """)
    for row in cur.fetchall():
        source = row["source"]
        cnt = row["cnt"]
        skill_name = f"source:{source}"
        cur.execute("""
            INSERT OR REPLACE INTO skills
                (name, category, description, loaded_count, last_used_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            skill_name,
            "source",
            f"Sessions from platform {source}",
            cnt,
            now_iso,
        ))
        count += 1

    # Skills from models
    cur.execute("""
        SELECT model, COUNT(*) as cnt
        FROM sessions
        WHERE model IS NOT NULL AND model != ''
        GROUP BY model
        ORDER BY cnt DESC
    """)
    for row in cur.fetchall():
        model = row["model"]
        cnt = row["cnt"]
        skill_name = f"model:{model}"
        cur.execute("""
            INSERT OR REPLACE INTO skills
                (name, category, description, loaded_count, last_used_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            skill_name,
            "model",
            f"Sessions using model {model}",
            cnt,
            now_iso,
        ))
        count += 1

    # Try hermes insights output for additional skills (graceful if fails)
    try:
        result = subprocess.run(
            ["hermes", "insights"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                # Look for lines like "  skill_name: loaded X times" or similar
                if ":" in line and "loaded" in line.lower():
                    parts = line.split(":", 1)
                    skill_name = parts[0].strip().lower().replace(" ", "-")
                    if skill_name:
                        desc = line
                        cur.execute("""
                            INSERT OR REPLACE INTO skills
                                (name, category, description, loaded_count, last_used_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            f"hermes:{skill_name}",
                            "hermes",
                            desc,
                            0,
                            now_iso,
                        ))
                        count += 1
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("hermes insights not available: %s", e)

    conn.commit()
    logger.info("  Populated %d skill entries", count)
    return count


# ── Kanban Population ────────────────────────────────────────────────────────


def populate_kanban(conn: sqlite3.Connection, kanban_db_path: str = "~/.hermes/kanban.db") -> int:
    """Read kanban tasks from Hermes kanban.db and merge into kanban_tasks.

    Checks multiple paths:
      1. mac-data/kanban.db (synced from Mac, most up-to-date)
      2. ~/.hermes/kanban.db (Pi's local Hermes)

    If neither exists, logs a warning and skips gracefully.
    """
    # Try multiple paths
    candidates = [
        Path(os.path.expanduser("~/usage-analytics/mac-data/kanban.db")),  # synced from Mac
        Path(os.path.expanduser("~/.hermes/kanban.db")),                  # Pi local
    ]

    kanban_path = None
    for p in candidates:
        if p.exists():
            kanban_path = p
            break

    if not kanban_path:
        logger.warning("Kanban DB not found at any path — skipping")
        logger.warning("  Tried: %s", ", ".join(str(p) for p in candidates))
        return 0

    try:
        kb_conn = sqlite3.connect(str(kanban_path))
        kb_conn.row_factory = sqlite3.Row
        kb_cur = kb_conn.cursor()

        # Check if tasks table exists
        kb_cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        if not kb_cur.fetchone():
            logger.warning("No 'tasks' table in kanban DB at %s — skipping", kanban_path)
            kb_conn.close()
            return 0

        kb_cur.execute(
            "SELECT id, title, body, status, assignee, priority, "
            "created_at, completed_at FROM tasks"
        )
        rows = kb_cur.fetchall()
        kb_conn.close()
    except sqlite3.Error as e:
        logger.warning("Error reading kanban DB at %s: %s", kanban_path, e)
        return 0

    if not rows:
        logger.info("  No kanban tasks found in %s", kanban_path)
        return 0

    cur = conn.cursor()
    count = 0

    for row in rows:
        task_id = str(row["id"])
        title = row["title"] or ""
        body = row["body"] or ""
        status = row["status"] or "todo"
        assignee = row["assignee"] or ""
        priority_val = row["priority"]
        # Map integer priority: 0=low, 1=medium, 2=high, 3=critical
        if priority_val is None or priority_val == 0:
            priority_str = "medium"
        elif priority_val == 1:
            priority_str = "low"
        elif priority_val == 2:
            priority_str = "high"
        else:
            priority_str = "critical"
        # Convert unix timestamps to ISO format
        created_at = row["created_at"]
        if isinstance(created_at, (int, float)):
            created_at = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            created_at = str(created_at) if created_at else ""
        completed_at = row["completed_at"]
        if isinstance(completed_at, (int, float)):
            completed_at = datetime.fromtimestamp(completed_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            completed_at = str(completed_at) if completed_at else ""

        cur.execute("""
            INSERT OR REPLACE INTO kanban_tasks
                (id, title, body, status, priority, assignee,
                 created_at, completed_at, tags, board_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id, title, body, status, priority_str, assignee,
            created_at, completed_at, "", "default",
        ))
        count += 1

    conn.commit()
    logger.info("  Populated %d kanban tasks from %s", count, kanban_path)
    return count


def update_sync_meta(conn: sqlite3.Connection, source: str, count: int):
    """Update sync metadata after processing a source."""
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO sync_meta (source, last_sync_at, records_count, status)
        VALUES (?, datetime('now'), ?, 'ok')
    """, (source, count))
    conn.commit()


# ── Main Pipeline ─────────────────────────────────────────────────────────────


def run_pipeline(db_path: str, full: bool = False):
    """Run the full aggregation pipeline."""
    ensure_schema(db_path)
    conn = get_db(db_path)

    logger.info("=" * 60)
    logger.info("AgenticOS Aggregator Pipeline")
    logger.info("  DB: %s", db_path)
    logger.info("  Mode: %s", "FULL" if full else "incremental")
    logger.info("=" * 60)

    # 1. Import sessions from JSONL
    logger.info("\n[1/8] Importing sessions from JSONL snapshots...")
    files, records = import_jsonl_snapshots(conn, SNAPSHOTS_HERMES)
    logger.info("  Processed %d files, %d new/updated session records", files, records)
    update_sync_meta(conn, "jsonl-snapshots", records)

    # 2. Import provider tracking
    logger.info("\n[2/8] Importing provider tracking data...")
    pcount = import_provider_tracking(conn, PROVIDER_TRACKING)
    logger.info("  Imported %d provider tracking records", pcount)
    update_sync_meta(conn, "provider-tracking", pcount)

    # 3. Import cost receipts
    logger.info("\n[3/8] Importing cost receipts...")
    rcount = import_cost_history(conn, COST_HISTORY)
    logger.info("  Imported %d cost receipts", rcount)
    update_sync_meta(conn, "cost-receipts", rcount)

    # 4. Fetch OpenRouter live costs
    logger.info("\n[4/8] Fetching OpenRouter live costs...")
    or_data = fetch_openrouter_live_costs(conn)
    if or_data:
        logger.info("  OpenRouter live usage: $%.2f", or_data["total_usage"])
    update_sync_meta(conn, "openrouter-live", 1 if or_data else 0)

    # 5. Compute rollups
    logger.info("\n[5/8] Computing cost rollups...")
    compute_daily_costs(conn)
    compute_monthly_costs(conn)

    # 6. Populate cron jobs from Pi crontab
    logger.info("\n[6/8] Populating cron jobs from crontab...")
    cron_count = populate_cron_jobs(conn)
    logger.info("  Imported %d cron jobs", cron_count)
    update_sync_meta(conn, "cron-jobs", cron_count)

    # 7. Populate skills from session data
    logger.info("\n[7/8] Populating skills from session data...")
    skill_count = populate_skills(conn)
    logger.info("  Imported %d skills", skill_count)
    update_sync_meta(conn, "skills", skill_count)

    # 8. Populate kanban from Hermes kanban.db
    logger.info("\n[8/8] Populating kanban tasks...")
    kanban_count = populate_kanban(conn)
    logger.info("  Imported %d kanban tasks", kanban_count)
    update_sync_meta(conn, "kanban", kanban_count)

    # Summary
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sessions")
    total_sessions = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM provider_snapshots")
    total_provider = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM cost_receipts")
    total_receipts = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT year_month) FROM monthly_costs")
    total_months = cur.fetchone()[0]

    logger.info("\n" + "=" * 60)
    logger.info("Aggregation Complete")
    logger.info("  Sessions: %d", total_sessions)
    logger.info("  Provider snapshots: %d", total_provider)
    logger.info("  Cost receipts: %d", total_receipts)
    logger.info("  Months with data: %d", total_months)
    logger.info("=" * 60)

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="AgenticOS Data Aggregator")
    parser.add_argument("--full", action="store_true", help="Full rebuild")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    run_pipeline(args.db, full=args.full)


if __name__ == "__main__":
    main()
