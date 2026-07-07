#!/usr/bin/env python3
"""
Hermes Session Collector — Mac-side
Exports Hermes session data via `hermes sessions export` and writes to
the shared JSONL snapshots directory for the Pi to aggregate.

Runs via launchd (every 30 min). Outputs to ~/usage-analytics/snapshots/hermes/

Usage:
    python3 hermes-session-collect.py                    # default: export new sessions
    python3 hermes-session-collect.py --full             # full export (first time)
    python3 hermes-session-collect.py --output PATH      # custom output dir
"""
import json
import os
import sys
import subprocess
import datetime
import hashlib
import pathlib
from datetime import timezone

# ── Config ────────────────────────────────────────────────────────────────────

SNAPSHOTS_DIR = os.path.expanduser("~/usage-analytics/snapshots/hermes")
STATE_FILE = os.path.expanduser("~/usage-analytics/.hermes-collector-state.json")

# ── Helpers ───────────────────────────────────────────────────────────────────


def get_last_synced_ids():
    """Return set of session IDs that have already been synced."""
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        return set(state.get("exported_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_synced_ids(session_ids):
    """Persist the set of synced session IDs."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {
        "exported_ids": sorted(session_ids),
        "last_export": datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def export_sessions(full=False):
    """Export sessions via `hermes sessions export` and return parsed records."""
    cmd = ["hermes", "sessions", "export", "-"]
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        print("ERROR: hermes sessions export timed out after 60s", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("ERROR: 'hermes' not found in PATH", file=sys.stderr)
        return []

    if result.returncode != 0:
        print(f"ERROR: hermes returned code {result.returncode}", file=sys.stderr)
        print(result.stderr[:500], file=sys.stderr)
        return []

    lines = [l.strip() for l in result.stdout.split("\n") if l.strip()]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return records


def normalize_record(rec):
    """Normalize a Hermes session export record into a standard format.

    Returns a dict with known fields, or None if record is invalid.
    """
    sid = rec.get("id")
    if not sid:
        return None

    started = rec.get("started_at") or rec.get("start_time") or ""
    ended = rec.get("ended_at") or rec.get("end_time") or ""

    # Calculate duration
    duration = rec.get("duration_seconds", 0)
    if not duration and started and ended:
        try:
            from datetime import datetime as dt
            fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
            for s_fmt in [fmt, "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"]:
                try:
                    s = dt.strptime(started[:26].rstrip("Z") + ("Z" if "Z" in started else ""), s_fmt)
                    e = dt.strptime(ended[:26].rstrip("Z") + ("Z" if "Z" in ended else ""), s_fmt)
                    duration = int((e - s).total_seconds())
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    # Token counts
    inp = rec.get("input_tokens", 0) or 0
    out = rec.get("output_tokens", 0) or 0
    cache_r = rec.get("cache_read_tokens", 0) or 0
    cache_w = rec.get("cache_write_tokens", 0) or 0
    reasoning = rec.get("reasoning_tokens", 0) or 0

    return {
        "id": sid,
        "source": rec.get("source", "unknown"),
        "model": rec.get("model", "unknown"),
        "profile": rec.get("profile", "default"),
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_r,
        "cache_write_tokens": cache_w,
        "reasoning_tokens": reasoning,
        "started_at": started,
        "ended_at": ended,
        "duration_seconds": duration,
        "message_count": rec.get("message_count", 0) or 0,
        "tool_call_count": rec.get("tool_call_count", 0) or 0,
        "api_call_count": rec.get("api_call_count", 0) or 0,
        "billing_provider": rec.get("billing_provider", ""),
        "billing_mode": rec.get("billing_mode", ""),
        "estimated_cost_usd": rec.get("estimated_cost_usd", 0.0) or 0.0,
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
        "ended_reason": rec.get("end_reason", ""),
        "archived": 1 if rec.get("archived") else 0,
        "collected_at": datetime.datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_snapshot(records, out_dir):
    """Write records to a dated JSONL file."""
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.date.today().isoformat()
    out_path = os.path.join(out_dir, f"{today}.jsonl")

    # Read existing IDs in the file to avoid duplicates
    existing_ids = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    existing_ids.add(r.get("id"))
                except json.JSONDecodeError:
                    continue

    new_count = 0
    mode = "a" if os.path.exists(out_path) else "w"
    with open(out_path, mode) as f:
        for rec in records:
            if rec["id"] not in existing_ids:
                f.write(json.dumps(rec, default=str) + "\n")
                existing_ids.add(rec["id"])
                new_count += 1

    return out_path, new_count


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Session Collector")
    parser.add_argument("--full", action="store_true", help="Full export (ignore state)")
    parser.add_argument("--output", default=SNAPSHOTS_DIR, help="Output directory")
    cfg = parser.parse_args()

    out_dir = cfg.output

    print(f"Hermes Session Collector", flush=True)
    print(f"  Output: {out_dir}", flush=True)

    # Export all sessions
    records = export_sessions(full=cfg.full)
    if not records:
        print("  No sessions exported.", flush=True)
        return

    print(f"  Exported {len(records)} raw session records.", flush=True)

    # Normalize
    normalized = []
    for rec in records:
        n = normalize_record(rec)
        if n:
            normalized.append(n)

    print(f"  Normalized {len(normalized)} records.", flush=True)

    # Write incremental snapshot
    out_path, new_count = write_snapshot(normalized, out_dir)
    print(f"  Wrote {new_count} new records to {out_path}", flush=True)

    # Update state
    synced = get_last_synced_ids()
    for n in normalized:
        synced.add(n["id"])
    save_synced_ids(synced)
    print(f"  State: {len(synced)} total tracked IDs.", flush=True)

    print("Done.", flush=True)


if __name__ == "__main__":
    main()
