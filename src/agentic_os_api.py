"""
AgenticOS Dashboard Backend — SQLite API for the Flask dashboard.
Provides all the API endpoints that read from the unified agentic_os.db.

Usage in app.py:
    from agentic_os_api import AgenticOSAPI
    api = AgenticOSAPI("/home/arshad14/usage-dashboard/agentic_os.db")
    app.register_blueprint(api.blueprint, url_prefix="/api/v2")
"""
from __future__ import annotations

import json
import os
import sqlite3
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

from flask import Blueprint, jsonify, request

logger = logging.getLogger("agentic-os-api")

DEFAULT_DB = "/home/arshad14/usage-dashboard/agentic_os.db"


class AgenticOSAPI:
    """Read-only API for the AgenticOS SQLite database."""

    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = os.path.expanduser(db_path) if "~" in db_path else db_path
        self.blueprint = Blueprint("agentic_os", __name__)
        self._register_routes()

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _register_routes(self):
        bp = self.blueprint

        # ── Sessions ────────────────────────────────────────────────────────

        @bp.route("/sessions")
        def list_sessions():
            """List sessions with pagination and filters."""
            limit = min(int(request.args.get("limit", 50)), 500)
            offset = int(request.args.get("offset", 0))
            source = request.args.get("source", "")
            model = request.args.get("model", "")
            profile = request.args.get("profile", "")
            since = request.args.get("since", "")

            conn = self._get_db()
            cur = conn.cursor()

            where = []
            params = []
            if source:
                where.append("source = ?")
                params.append(source)
            if model:
                where.append("model = ?")
                params.append(model)
            if profile:
                where.append("profile = ?")
                params.append(profile)
            if since:
                where.append("started_at >= ?")
                params.append(since)

            where_clause = " AND ".join(where) if where else "1=1"

            cur.execute(f"SELECT COUNT(*) FROM sessions WHERE {where_clause}", params)
            total = cur.fetchone()[0]

            cur.execute(f"""
                SELECT * FROM sessions
                WHERE {where_clause}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])

            sessions = [self._row_to_dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({
                "sessions": sessions,
                "total": total,
                "limit": limit,
                "offset": offset,
            })

        @bp.route("/sessions/explore")
        def explore_sessions():
            """Search and filter sessions with pagination — powers the Session Explorer tab."""
            limit = min(int(request.args.get("limit", 50)), 200)
            offset = int(request.args.get("offset", 0))
            source = request.args.get("source", "")
            model = request.args.get("model", "")
            profile = request.args.get("profile", "")
            query = request.args.get("q", "") or request.args.get("search", "")
            query = query.strip()
            since = request.args.get("since", "")
            until = request.args.get("until", "")
            sort_col = request.args.get("sort", "started_at")
            sort_dir = request.args.get("dir", "desc")

            conn = self._get_db()
            cur = conn.cursor()

            where = []
            params = []
            if source:
                where.append("source = ?")
                params.append(source)
            if model:
                where.append("model = ?")
                params.append(model)
            if since:
                where.append("started_at >= ?")
                params.append(since)
            if until:
                where.append("started_at <= ?")
                params.append(until)
            if query:
                where.append("(title LIKE ? OR id LIKE ?)")
                params.extend([f"%{query}%", f"%{query}%"])
            if profile:
                where.append("profile = ?")
                params.append(profile)

            wc = " AND ".join(where) if where else "1=1"
            # Validate sort column to prevent SQL injection
            allowed_sorts = {"started_at", "model", "source", "input_tokens", "output_tokens",
                             "message_count", "tool_call_count", "estimated_cost_usd", "title", "id"}
            if sort_col not in allowed_sorts:
                sort_col = "started_at"
            order = "ASC" if sort_dir.lower() == "asc" else "DESC"

            # Total + stats for filtered set
            cur.execute(f"SELECT COUNT(*) as c, COALESCE(SUM(input_tokens + output_tokens),0) as tok, COALESCE(SUM(estimated_cost_usd),0.0) as cost FROM sessions WHERE {wc}", params)
            stats = self._row_to_dict(cur.fetchone())

            # Source and model options for filter dropdowns
            cur.execute("SELECT DISTINCT source FROM sessions ORDER BY source")
            sources = [r[0] for r in cur.fetchall() if r[0]]
            cur.execute("SELECT DISTINCT model FROM sessions ORDER BY model")
            models = [r[0] for r in cur.fetchall() if r[0]]

            # Paginated results with server-side sorting
            cur.execute(f"SELECT * FROM sessions WHERE {wc} ORDER BY {sort_col} {order} LIMIT ? OFFSET ?", params + [limit, offset])
            sessions = [self._row_to_dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({
                "sessions": sessions,
                "total": stats["c"] if stats else 0,
                "total_tokens": stats["tok"] if stats else 0,
                "total_cost": round(stats["cost"], 4) if stats else 0,
                "limit": limit,
                "offset": offset,
                "sources": sources,
                "models": models,
            })

        @bp.route("/sessions/<session_id>")
        def get_session(session_id: str):
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                return jsonify(self._row_to_dict(row))
            return jsonify({"error": "Session not found"}), 404

        # ── Stats ───────────────────────────────────────────────────────────

        @bp.route("/stats")
        def stats():
            """Return aggregated statistics similar to /api/stats but from SQLite."""
            conn = self._get_db()
            cur = conn.cursor()

            # Time boundaries
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            week_ago = now.strftime("%Y-%m-%dT00:00:00Z")
            two_weeks_ago = now.strftime("%Y-%m-%dT00:00:00Z")

            # All-time totals
            cur.execute("""
                SELECT 
                    COUNT(*) as total_sessions,
                    COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                    COUNT(DISTINCT model) as distinct_models,
                    COUNT(DISTINCT source) as distinct_sources
                FROM sessions
            """)
            all_time = self._row_to_dict(cur.fetchone())

            # Per-model breakdown
            cur.execute("""
                SELECT model, COUNT(*) as sessions, 
                       SUM(input_tokens + output_tokens) as tokens,
                       SUM(estimated_cost_usd) as cost,
                       COUNT(DISTINCT source) as platforms
                FROM sessions
                GROUP BY model
                ORDER BY sessions DESC
            """)
            model_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            # Per-source/platform breakdown
            cur.execute("""
                SELECT source, COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens
                FROM sessions
                GROUP BY source
                ORDER BY sessions DESC
            """)
            source_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            # Per-profile breakdown
            cur.execute("""
                SELECT profile, COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens,
                       SUM(estimated_cost_usd) as cost
                FROM sessions
                GROUP BY profile
                ORDER BY sessions DESC
            """)
            profile_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            # Today's stats
            cur.execute("""
                SELECT COUNT(*) as sessions, 
                       COALESCE(SUM(input_tokens + output_tokens), 0) as tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as cost
                FROM sessions 
                WHERE date(started_at) = ?
            """, (today,))
            today_stats = self._row_to_dict(cur.fetchone())

            # Daily trend (last 14 days)
            cur.execute("""
                SELECT date(started_at) as day,
                       COUNT(*) as sessions,
                       COALESCE(SUM(input_tokens + output_tokens), 0) as tokens,
                       COALESCE(SUM(estimated_cost_usd), 0) as cost
                FROM sessions
                WHERE started_at >= date('now', '-14 days')
                GROUP BY day
                ORDER BY day ASC
            """)
            daily_trend = [self._row_to_dict(r) for r in cur.fetchall()]

            # Monthly cost breakdown
            cur.execute("""
                SELECT year_month, provider, billing_mode,
                       session_count, total_tokens,
                       estimated_cost, actual_cost, receipt_cost
                FROM monthly_costs
                ORDER BY year_month DESC, provider ASC
            """)
            monthly = [self._row_to_dict(r) for r in cur.fetchall()]

            # Provider tracking summaries (last 30 days)
            cur.execute("""
                SELECT provider, model, COUNT(*) as records,
                       SUM(tokens) as total_tokens
                FROM provider_snapshots
                WHERE timestamp >= date('now', '-30 days')
                GROUP BY provider, model
                ORDER BY records DESC
            """)
            provider_summary = [self._row_to_dict(r) for r in cur.fetchall()]

            # Sync metadata
            cur.execute("SELECT * FROM sync_meta")
            sync_info = [self._row_to_dict(r) for r in cur.fetchall()]

            conn.close()

            return jsonify({
                "all_time": all_time,
                "today": today_stats,
                "models": model_breakdown,
                "sources": source_breakdown,
                "profiles": profile_breakdown,
                "daily_trend": daily_trend,
                "monthly_costs": monthly,
                "provider_summary": provider_summary,
                "sync_info": sync_info,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── Provider Tracking ───────────────────────────────────────────────

        @bp.route("/providers")
        def list_providers():
            """List provider tracking data."""
            limit = min(int(request.args.get("limit", 100)), 1000)
            offset = int(request.args.get("offset", 0))
            provider = request.args.get("provider", "")
            since = request.args.get("since", "")

            conn = self._get_db()
            cur = conn.cursor()

            where = []
            params = []
            if provider:
                where.append("provider = ?")
                params.append(provider)
            if since:
                where.append("timestamp >= ?")
                params.append(since)

            where_clause = " AND ".join(where) if where else "1=1"
            cur.execute(f"SELECT COUNT(*) FROM provider_snapshots WHERE {where_clause}", params)
            total = cur.fetchone()[0]

            cur.execute(f"""
                SELECT * FROM provider_snapshots
                WHERE {where_clause}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])

            records = [self._row_to_dict(r) for r in cur.fetchall()]
            conn.close()

            return jsonify({"records": records, "total": total})

        # ── Cost Receipts ───────────────────────────────────────────────────

        @bp.route("/costs")
        def list_costs():
            """List cost receipts."""
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM cost_receipts
                ORDER BY date DESC
                LIMIT 50
            """)
            receipts = [self._row_to_dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT provider, SUM(amount) as total, COUNT(*) as count
                FROM cost_receipts
                GROUP BY provider
                ORDER BY total DESC
            """)
            provider_totals = [self._row_to_dict(r) for r in cur.fetchall()]

            cur.execute("SELECT SUM(amount) as grand_total FROM cost_receipts")
            grand_total = cur.fetchone()["grand_total"] or 0

            conn.close()
            return jsonify({
                "receipts": receipts,
                "provider_totals": provider_totals,
                "grand_total": round(grand_total, 2),
            })

        # ── Monthly Costs ───────────────────────────────────────────────────

        @bp.route("/costs/monthly")
        def monthly_costs():
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM monthly_costs
                ORDER BY year_month DESC, provider ASC
            """)
            rows = [self._row_to_dict(r) for r in cur.fetchall()]
            conn.close()
            return jsonify(rows)

        # ── Live OpenRouter Costs ───────────────────────────────────────────

        @bp.route("/costs/live")
        def live_costs():
            """Return the latest OpenRouter live cost snapshot plus reconciliation.

            Returns the most recent snapshot from openrouter_live_costs table
            along with estimated costs from sessions and receipt-based costs
            for the current month.
            """
            conn = self._get_db()
            cur = conn.cursor()

            # Latest live snapshot
            cur.execute("""
                SELECT * FROM openrouter_live_costs
                ORDER BY id DESC LIMIT 1
            """)
            latest = cur.fetchone()

            now = datetime.now(timezone.utc)
            current_month = now.strftime("%Y-%m")

            # Estimated costs from sessions for current month
            cur.execute("""
                SELECT
                    COALESCE(SUM(estimated_cost_usd), 0.0) as estimated_total,
                    COALESCE(SUM(CASE WHEN billing_provider IN ('openrouter', 'opencode-go')
                                  THEN estimated_cost_usd ELSE 0 END), 0.0) as estimated_openrouter
                FROM sessions
                WHERE strftime('%Y-%m', started_at) = ?
            """, (current_month,))
            estimated = self._row_to_dict(cur.fetchone()) or {}

            # Receipt costs for current month
            cur.execute("""
                SELECT
                    provider,
                    SUM(amount) as total,
                    COUNT(*) as count
                FROM cost_receipts
                WHERE strftime('%Y-%m', date) = ?
                GROUP BY provider
            """, (current_month,))
            receipt_rows = cur.fetchall()
            receipt_totals = {}
            receipt_grand_total = 0.0
            for r in receipt_rows:
                d = self._row_to_dict(r)
                receipt_totals[d["provider"]] = d["total"]
                receipt_grand_total += d["total"]

            # Live cost history (last 30 snapshots for charting)
            cur.execute("""
                SELECT id, snapshot_at, total_usage, label, created_at
                FROM openrouter_live_costs
                ORDER BY id DESC LIMIT 30
            """)
            history = [self._row_to_dict(r) for r in cur.fetchall()]
            history.reverse()  # chronological

            conn.close()

            live_total = round(float(latest["total_usage"]), 4) if latest else 0.0
            snapshot_at = latest["snapshot_at"] if latest else None

            return jsonify({
                "live": {
                    "total_usage": live_total,
                    "label": latest["label"] if latest else None,
                    "limit_amt": latest["limit_amt"] if latest else None,
                    "is_free_tier": bool(latest["is_free_tier"]) if latest else None,
                    "snapshot_at": snapshot_at,
                },
                "estimated": {
                    "total": round(float(estimated.get("estimated_total", 0)), 2),
                    "openrouter": round(float(estimated.get("estimated_openrouter", 0)), 2),
                },
                "receipts": {
                    "by_provider": {k: round(v, 2) for k, v in receipt_totals.items()},
                    "grand_total": round(receipt_grand_total, 2),
                },
                "reconciliation": {
                    "openrouter_live": live_total,
                    "openrouter_estimated": round(float(estimated.get("estimated_openrouter", 0)), 2),
                    "openrouter_receipt": round(receipt_totals.get("openrouter", 0), 2),
                    "openrouter_variance_live_vs_estimated": round(
                        live_total - float(estimated.get("estimated_openrouter", 0)), 4
                    ),
                    "openrouter_variance_live_vs_receipt": round(
                        live_total - receipt_totals.get("openrouter", 0), 4
                    ),
                    "total_estimated_vs_receipt": round(
                        float(estimated.get("estimated_total", 0)) - receipt_grand_total, 2
                    ),
                },
                "history": history,
                "month": current_month,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── Cron Jobs ───────────────────────────────────────────────────────

        @bp.route("/cron")
        def list_cron_jobs():
            """Return cron jobs matching the template format expected by cron.html.
            Format: {"total_jobs": N, "categories": [...], "overlap_count": N,
                     "jobs": [{"category":"...", "schedule":"...", "command":"...", "status":"ok"}],
                     "generated_at": "..."}
            """
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM cron_jobs ORDER BY machine, name")
            rows = cur.fetchall()
            conn.close()

            jobs = []
            categories = set()
            for r in rows:
                d = self._row_to_dict(r)
                cat = d.get("skill", "") or "agentic-os"
                categories.add(cat if cat else "Uncategorized")
                jobs.append({
                    "category": cat,
                    "schedule": d.get("schedule", ""),
                    "command": d.get("description", "") or d.get("name", ""),
                    "status": d.get("status", "ok"),
                    "overlap_with": None,
                })

            return jsonify({
                "total_jobs": len(jobs),
                "categories": sorted(categories),
                "overlap_count": 0,
                "jobs": jobs,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── Skills ──────────────────────────────────────────────────────────

        @bp.route("/skills")
        def list_skills():
            """Return skills matching the template format expected by skills.html.
            Format: {"skills": [...], "categories": [...], "total_skills": N,
                     "zero_usage": N, "duplicates": N}
            """
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM skills ORDER BY loaded_count DESC")
            rows = cur.fetchall()

            skills_list = []
            cat_set = set()
            for r in rows:
                d = self._row_to_dict(r)
                cat = d.get("category", "uncategorized")
                cat_set.add(cat)
                skills_list.append({
                    "name": d.get("name", ""),
                    "key": f"{cat}/{d.get('name', '')}",
                    "category": cat,
                    "use_count": d.get("loaded_count", 0),
                    "view_count": 0,
                    "state": "active",
                    "pinned": False,
                    "patch_count": d.get("edit_count", 0),
                    "last_used_at": d.get("last_used_at"),
                    "has_duplicates": False,
                })

            # Also add skills derived from session model usage
            cur.execute("""
                SELECT model, COUNT(*) as cnt,
                       MIN(started_at) as first_seen, MAX(started_at) as last_seen
                FROM sessions
                WHERE model IS NOT NULL AND model != ''
                GROUP BY model
                ORDER BY cnt DESC
            """)
            for r in cur.fetchall():
                model_name = f"model:{r['model']}"
                if not any(s["name"] == model_name for s in skills_list):
                    skills_list.append({
                        "name": model_name,
                        "key": f"models/{model_name}",
                        "category": "models",
                        "use_count": r["cnt"],
                        "view_count": 0,
                        "state": "active",
                        "pinned": False,
                        "patch_count": 0,
                        "last_used_at": r["last_seen"],
                        "has_duplicates": False,
                    })
                    cat_set.add("models")

            conn.close()

            duplicates = 0
            name_map = {}
            for s in skills_list:
                name_map.setdefault(s["name"], []).append(s)
            for name, matches in name_map.items():
                if len(matches) > 1:
                    duplicates += 1
                    for m in matches:
                        m["has_duplicates"] = True

            zero_usage = sum(1 for s in skills_list if s["use_count"] == 0 and s["view_count"] == 0)

            return jsonify({
                "skills": skills_list,
                "categories": sorted(list(cat_set)),
                "total_skills": len(skills_list),
                "zero_usage": zero_usage,
                "duplicates": duplicates,
            })

        # ── Kanban ──────────────────────────────────────────────────────────

        @bp.route("/kanban")
        def list_kanban():
            """Return kanban tasks matching the template format expected by kanban.html.
            Format: {"boards": [...], "tasks": [...], "current_board": "..."}
            Each task: {id, title, body, assignee, status, priority, created_at, ...}
            """
            slug = request.args.get("board", "default")
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM kanban_tasks ORDER BY created_at DESC")
            rows = cur.fetchall()

            tasks = []
            status_counts = {}
            for r in rows:
                d = self._row_to_dict(r)
                ts = d.get("status", "todo")
                status_counts[ts] = status_counts.get(ts, 0) + 1
                tasks.append({
                    "id": d.get("id", ""),
                    "title": d.get("title", ""),
                    "body": d.get("body", ""),
                    "assignee": d.get("assignee", ""),
                    "status": ts,
                    "priority": d.get("priority", "medium"),
                    "created_at": d.get("created_at"),
                    "completed_at": d.get("completed_at"),
                    "tags": (d.get("tags") or "").split(",") if d.get("tags") else [],
                })
            conn.close()

            boards = [{
                "slug": "default",
                "name": "Default Board",
                "task_count": len(tasks),
                "description": "Kanban board for tracking tasks and work items",
                "status_counts": status_counts,
            }]

            return jsonify({
                "boards": boards,
                "tasks": tasks,
                "current_board": slug,
            })

        @bp.route("/kanban/stats")
        def kanban_stats():
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT status, COUNT(*) as count FROM kanban_tasks GROUP BY status")
            by_status = {r["status"]: r["count"] for r in cur.fetchall()}
            cur.execute(
                "SELECT assignee, COUNT(*) as count FROM kanban_tasks "
                "WHERE status != 'done' GROUP BY assignee"
            )
            by_assignee = {r["assignee"]: r["count"] for r in cur.fetchall()}
            conn.close()
            return jsonify({"by_status": by_status, "by_assignee": by_assignee})

        # ── Brain / Model Routing Metrics ────────────────────────────────────

        @bp.route("/brain")
        def brain_metrics():
            """Model routing intelligence — routing patterns, switching, provider perf."""
            conn = self._get_db()
            cur = conn.cursor()

            # Model routing breakdown
            cur.execute("""
                SELECT model,
                       COUNT(*) as sessions,
                       COALESCE(SUM(input_tokens + output_tokens), 0) as tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) as cost,
                       COUNT(DISTINCT source) as platforms,
                       COUNT(DISTINCT billing_provider) as providers,
                       MIN(started_at) as first_seen, MAX(started_at) as last_seen
                FROM sessions
                GROUP BY model ORDER BY sessions DESC
            """)
            model_routing = [self._row_to_dict(r) for r in cur.fetchall()]

            # Billing mode distribution
            cur.execute("""
                SELECT COALESCE(NULLIF(billing_mode, ''), 'unknown') as mode,
                       COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens
                FROM sessions GROUP BY mode ORDER BY sessions DESC
            """)
            billing_mode_dist = [self._row_to_dict(r) for r in cur.fetchall()]

            # Provider-level breakdown
            cur.execute("""
                SELECT COALESCE(NULLIF(billing_provider, ''), 'unknown') as provider,
                       COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens,
                       SUM(COALESCE(estimated_cost_usd, 0.0)) as cost,
                       COUNT(DISTINCT model) as models_used
                FROM sessions
                GROUP BY provider ORDER BY sessions DESC
            """)
            provider_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            # Platform distribution
            cur.execute("""
                SELECT source, COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens,
                       SUM(COALESCE(estimated_cost_usd, 0.0)) as cost,
                       COUNT(DISTINCT model) as models_used
                FROM sessions
                GROUP BY source ORDER BY sessions DESC
            """)
            platform_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            # Daily model usage (last 14 days)
            cur.execute("""
                SELECT date(started_at) as day, model,
                       COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens
                FROM sessions
                WHERE started_at >= date('now', '-14 days')
                  AND started_at IS NOT NULL
                GROUP BY day, model ORDER BY day ASC, sessions DESC
            """)
            daily_model_usage = [self._row_to_dict(r) for r in cur.fetchall()]

            # Profile distribution
            cur.execute("""
                SELECT profile, COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens,
                       COUNT(DISTINCT model) as models_used
                FROM sessions
                GROUP BY profile ORDER BY sessions DESC
            """)
            profile_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            conn.close()

            return jsonify({
                "model_routing": model_routing,
                "billing_modes": billing_mode_dist,
                "providers": provider_breakdown,
                "platforms": platform_breakdown,
                "daily_model_usage": daily_model_usage,
                "profiles": profile_breakdown,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── Token Utilization Metrics ────────────────────────────────────────

        @bp.route("/tokens")
        def token_metrics():
            """Token utilization — per-model efficiency, daily trends, context usage."""
            conn = self._get_db()
            cur = conn.cursor()

            # Per-model token breakdown
            cur.execute("""
                SELECT model,
                       COUNT(*) as sessions,
                       COALESCE(SUM(input_tokens), 0) as total_input,
                       COALESCE(SUM(output_tokens), 0) as total_output,
                       COALESCE(AVG(input_tokens), 0) as avg_input,
                       COALESCE(AVG(output_tokens), 0) as avg_output,
                       COALESCE(MAX(input_tokens + output_tokens), 0) as max_tokens,
                       COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens
                FROM sessions
                GROUP BY model ORDER BY total_tokens DESC
            """)
            by_model = [self._row_to_dict(r) for r in cur.fetchall()]

            # Efficiency metrics
            cur.execute("""
                SELECT model,
                       CASE WHEN SUM(message_count) > 0
                            THEN CAST(SUM(input_tokens + output_tokens) AS REAL) / SUM(message_count)
                            ELSE 0 END as tokens_per_message,
                       CASE WHEN SUM(tool_call_count) > 0
                            THEN CAST(SUM(input_tokens + output_tokens) AS REAL) / SUM(tool_call_count)
                            ELSE 0 END as tokens_per_tool_call,
                       CASE WHEN COUNT(*) > 0
                            THEN CAST(SUM(input_tokens + output_tokens) AS REAL) / COUNT(*)
                            ELSE 0 END as tokens_per_session
                FROM sessions
                WHERE (input_tokens + output_tokens) > 0
                GROUP BY model ORDER BY tokens_per_session DESC
            """)
            efficiency = [self._row_to_dict(r) for r in cur.fetchall()]

            # Daily token consumption (last 30 days)
            cur.execute("""
                SELECT date(started_at) as day,
                       COALESCE(SUM(input_tokens), 0) as input_tokens,
                       COALESCE(SUM(output_tokens), 0) as output_tokens,
                       COUNT(*) as sessions,
                       COALESCE(SUM(input_tokens + output_tokens), 0) as total_tokens
                FROM sessions
                WHERE started_at >= date('now', '-30 days')
                  AND started_at IS NOT NULL
                GROUP BY day ORDER BY day ASC
            """)
            daily_tokens = [self._row_to_dict(r) for r in cur.fetchall()]

            # Input/output split by source
            cur.execute("""
                SELECT source,
                       COALESCE(AVG(input_tokens), 0) as avg_input,
                       COALESCE(AVG(output_tokens), 0) as avg_output,
                       COALESCE(SUM(input_tokens), 0) as total_input,
                       COALESCE(SUM(output_tokens), 0) as total_output,
                       CASE WHEN SUM(input_tokens + output_tokens) > 0
                            THEN CAST(SUM(input_tokens) AS REAL) / SUM(input_tokens + output_tokens)
                            ELSE 0 END as input_ratio
                FROM sessions
                GROUP BY source ORDER BY total_input + total_output DESC
            """)
            source_split = [self._row_to_dict(r) for r in cur.fetchall()]

            # Top token consumers (individual sessions)
            cur.execute("""
                SELECT id, model, source,
                       (input_tokens + output_tokens) as total_tokens,
                       input_tokens, output_tokens,
                       message_count, tool_call_count,
                       started_at, title
                FROM sessions
                ORDER BY (input_tokens + output_tokens) DESC
                LIMIT 10
            """)
            top_sessions = [self._row_to_dict(r) for r in cur.fetchall()]

            # Monthly token totals
            cur.execute("""
                SELECT strftime('%Y-%m', started_at) as month,
                       COALESCE(SUM(input_tokens), 0) as input_tokens,
                       COALESCE(SUM(output_tokens), 0) as output_tokens,
                       COUNT(*) as sessions
                FROM sessions
                WHERE started_at IS NOT NULL
                GROUP BY month ORDER BY month DESC
            """)
            monthly_tokens = [self._row_to_dict(r) for r in cur.fetchall()]

            conn.close()

            return jsonify({
                "by_model": by_model,
                "efficiency": efficiency,
                "daily_tokens": daily_tokens,
                "source_split": source_split,
                "top_sessions": top_sessions,
                "monthly_tokens": monthly_tokens,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── System Health ──────────────────────────────────────────────────

        # Cache: (timestamp, result)
        _health_cache = [0.0, None]

        def _run_cmd(cmd, timeout=5):
            """Run a shell command and return stdout stripped, or None on failure."""
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                return r.stdout.strip() if r.returncode == 0 else None
            except Exception:
                return None

        @bp.route("/health")
        def system_health():
            """Run comprehensive system health checks on the Pi.
            Cached for 30 seconds to avoid hammering the system.
            """
            now = time.time()
            if _health_cache[1] and now - _health_cache[0] < 30:
                return jsonify(_health_cache[1])

            result = {}

            # ── Pi uptime ───────────────────────────────────────────────────
            uptime_raw = _run_cmd(["uptime"], timeout=5)
            if uptime_raw:
                # Extract load averages from typical uptime output
                parts = uptime_raw.split("load average:")
                load_str = parts[1].strip() if len(parts) > 1 else ""
                loads = [x.strip() for x in load_str.split(",")] if load_str else []
                result["uptime"] = parts[0].strip()
                result["cpu_load"] = {
                    "1min": float(loads[0]) if len(loads) > 0 else None,
                    "5min": float(loads[1]) if len(loads) > 1 else None,
                    "15min": float(loads[2]) if len(loads) > 2 else None,
                }
            else:
                result["uptime"] = "unavailable"
                result["cpu_load"] = None

            # ── Memory usage ────────────────────────────────────────────────
            mem_raw = _run_cmd(["free", "-h"], timeout=5)
            if mem_raw:
                lines = mem_raw.split("\n")
                mem_parts = lines[1].split() if len(lines) > 1 else []
                if len(mem_parts) >= 7:
                    result["memory"] = {
                        "total": mem_parts[1],
                        "used": mem_parts[2],
                        "free": mem_parts[3],
                        "available": mem_parts[6],
                    }
                else:
                    result["memory"] = {"error": "unparseable"}
            else:
                result["memory"] = {"error": "unavailable"}

            # ── Disk usage ─────────────────────────────────────────────────
            disk_raw = _run_cmd(["df", "-h", "/"], timeout=5)
            if disk_raw:
                lines = disk_raw.split("\n")
                parts = lines[1].split() if len(lines) > 1 else []
                if len(parts) >= 6:
                    result["disk"] = {
                        "filesystem": parts[0],
                        "size": parts[1],
                        "used": parts[2],
                        "avail": parts[3],
                        "use_percent": parts[4],
                        "mount": parts[5],
                    }
                else:
                    result["disk"] = {"error": "unparseable"}
            else:
                result["disk"] = {"error": "unavailable"}

            # ── Mac reachability ────────────────────────────────────────────
            ping_result = _run_cmd(["ping", "-c1", "-W2", "mac-mini.local"], timeout=5)
            result["mac_reachable"] = ping_result is not None

            # ── API health checks ───────────────────────────────────────────
            apis = {}

            # OpenRouter
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "https://openrouter.ai/api/v1/models"],
                    capture_output=True, text=True, timeout=10,
                )
                code = r.stdout.strip()
                apis["openrouter"] = {
                    "status": "ok" if code == "200" else "degraded",
                    "http_code": code,
                }
            except subprocess.TimeoutExpired:
                apis["openrouter"] = {"status": "error", "http_code": "timeout"}
            except Exception:
                apis["openrouter"] = {"status": "error", "http_code": "unknown"}

            # OpenCode Go (external API)
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "https://opencode.ai/zen/go/v1/models"],
                    capture_output=True, text=True, timeout=10,
                )
                code = r.stdout.strip()
                apis["opencode_go"] = {
                    "status": "ok" if code == "200" else "degraded",
                    "http_code": code,
                }
            except subprocess.TimeoutExpired:
                apis["opencode_go"] = {"status": "error", "http_code": "timeout"}
            except Exception:
                apis["opencode_go"] = {"status": "error", "http_code": "unknown"}

            # Claude proxy (fcc-claude on Mac via mac-mini.local:8082)
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "http://mac-mini.local:8082/v1/messages"],
                    capture_output=True, text=True, timeout=5,
                )
                code = r.stdout.strip()
                apis["claude_proxy"] = {
                    "status": "ok" if code and code != "000" else "unknown",
                    "http_code": code or "unreachable",
                }
            except Exception:
                apis["claude_proxy"] = {"status": "unknown", "http_code": "unreachable"}

            result["apis"] = apis

            # ── Dashboard service status ───────────────────────────────────
            svc = _run_cmd(["systemctl", "is-active", "usage-dashboard"], timeout=5)
            result["dashboard_service"] = svc or "unknown"

            # ── Aggregator last run ─────────────────────────────────────────
            agg_log = _run_cmd(
                ["tail", "-5", "/home/arshad14/usage-analytics/logs/aggregator.log"],
                timeout=5,
            )
            result["aggregator"] = agg_log or "no log available"

            # ── Cloudflare tunnel ───────────────────────────────────────────
            cf_tunnel = _run_cmd(["systemctl", "is-active", "cloudflared"], timeout=5)
            if cf_tunnel is None:
                cf_tunnel = _run_cmd(["systemctl", "is-active", "cloudflared-tunnel"], timeout=5)
            result["cloudflare_tunnel"] = cf_tunnel or "unknown"

            # ── Running Python processes ────────────────────────────────────
            pyp = _run_cmd(["pgrep", "-a", "python3"], timeout=5)
            if pyp:
                lines = [l for l in pyp.split("\n") if l.strip()]
                result["python_processes"] = len(lines)
            else:
                result["python_processes"] = 0

            # ── Dashboard internal health ───────────────────────────────────
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "http://localhost:8080/api/v2/status"],
                    capture_output=True, text=True, timeout=5,
                )
                code = r.stdout.strip()
                result["dashboard_internal"] = {
                    "status": "ok" if code == "200" else "degraded",
                    "http_code": code,
                }
            except Exception:
                result["dashboard_internal"] = {"status": "error", "http_code": "unknown"}

            # ── AgenticOS DB stats ──────────────────────────────────────────
            db_path = self.db_path
            db_size_raw = _run_cmd(["ls", "-lh", db_path], timeout=5)
            if db_size_raw:
                parts = db_size_raw.split()
                result["db_size"] = parts[4] if len(parts) >= 5 else "unknown"
            else:
                result["db_size"] = "unknown"

            try:
                conn = self._get_db()
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM sessions")
                result["db_sessions"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM provider_snapshots")
                result["db_providers"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM cost_receipts")
                result["db_receipts"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM kanban_tasks")
                result["db_kanban"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM cron_jobs")
                result["db_cron_jobs"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM skills")
                result["db_skills"] = cur.fetchone()[0]
                conn.close()
            except Exception as e:
                result["db_error"] = str(e)

            result["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            result["cached_for"] = 30

            # Update cache
            _health_cache[0] = now
            _health_cache[1] = result

            return jsonify(result)

        # ── Changelog ────────────────────────────────────────────────────────

        @bp.route("/changelog")
        def changelog():
            """Return recent git commits from the ShiftLogic_HQ repo."""
            repo_paths = [
                "/home/arshad14/ShiftLogic_HQ",
                "/home/arshad14/Documents/ShiftLogic_HQ",
            ]

            repo = None
            for p in repo_paths:
                git_dir = os.path.join(p, ".git")
                if os.path.isdir(git_dir):
                    repo = p
                    break

            if not repo:
                return jsonify({
                    "repo": "ShiftLogic_HQ",
                    "commits": [],
                    "latest": None,
                    "error": "Repo not found on this host",
                    "checked": repo_paths,
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })

            try:
                # Last 30 commits with refs
                oneline = subprocess.run(
                    ["git", "log", "--oneline", "--decorate", "-30"],
                    capture_output=True, text=True, timeout=10,
                    cwd=repo,
                )
                lines = oneline.stdout.strip().split("\n") if oneline.stdout.strip() else []

                commits = []
                for line in lines:
                    parts = line.split(" ", 1)
                    if len(parts) < 2:
                        continue
                    raw_hash = parts[0]
                    rest = parts[1]

                    # Extract refs from decorator like (HEAD -> main, tag: v1.0)
                    refs = ""
                    message = rest
                    if "(" in rest and ")" in rest:
                        idx = rest.index("(")
                        idx2 = rest.index(")")
                        refs = rest[idx + 1:idx2]
                        message = rest[:idx].strip()
                        after_refs = rest[idx2 + 1:].strip()
                        if after_refs:
                            message = message + " " + after_refs if message else after_refs

                    # Full hash from rev-parse
                    hash_result = subprocess.run(
                        ["git", "rev-parse", raw_hash],
                        capture_output=True, text=True, timeout=5,
                        cwd=repo,
                    )
                    full_hash = hash_result.stdout.strip()

                    # Get date and author for this commit
                    detail = subprocess.run(
                        ["git", "log", "-1", "--format=%H|%an|%ae|%ai|%s", raw_hash],
                        capture_output=True, text=True, timeout=5,
                        cwd=repo,
                    )
                    detail_parts = detail.stdout.strip().split("|", 4)
                    author = detail_parts[1] if len(detail_parts) > 1 else ""
                    date = detail_parts[3] if len(detail_parts) > 3 else ""

                    commits.append({
                        "hash": full_hash,
                        "hash_short": raw_hash,
                        "author": author,
                        "date": date,
                        "message": message.strip(),
                        "refs": refs,
                    })

                # Latest commit details
                latest_result = subprocess.run(
                    ["git", "log", "-1", "--format=%H|%an|%ae|%ai|%s"],
                    capture_output=True, text=True, timeout=5,
                    cwd=repo,
                )
                latest_parts = latest_result.stdout.strip().split("|", 4)
                latest = {
                    "hash": latest_parts[0] if len(latest_parts) > 0 else "",
                    "author": latest_parts[1] if len(latest_parts) > 1 else "",
                    "email": latest_parts[2] if len(latest_parts) > 2 else "",
                    "date": latest_parts[3] if len(latest_parts) > 3 else "",
                    "message": latest_parts[4] if len(latest_parts) > 4 else "",
                } if latest_parts[0] else None

                return jsonify({
                    "repo": "ShiftLogic_HQ",
                    "commits": commits,
                    "latest": latest,
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })

            except subprocess.TimeoutExpired:
                return jsonify({
                    "repo": "ShiftLogic_HQ",
                    "commits": [],
                    "latest": None,
                    "error": "Git command timed out",
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }), 504
            except subprocess.CalledProcessError as e:
                return jsonify({
                    "repo": "ShiftLogic_HQ",
                    "commits": [],
                    "latest": None,
                    "error": f"Git error: {e.stderr[:200]}",
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }), 500
            except Exception as e:
                return jsonify({
                    "repo": "ShiftLogic_HQ",
                    "commits": [],
                    "latest": None,
                    "error": str(e),
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }), 500

        # ── Routing Analysis — Flat-rate vs PAYG Optimizer ──────────────────

        @bp.route("/routing/analysis")
        def routing_analysis():
            """Analyze flat-rate vs PAYG usage and suggest cost-saving routing changes."""
            conn = self._get_db()
            cur = conn.cursor()

            # Current month's sessions with cost data
            cur.execute("""
                SELECT model,
                       COUNT(*) as sessions,
                       COALESCE(SUM(input_tokens + output_tokens), 0) as tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) as estimated_cost,
                       COALESCE(NULLIF(billing_provider, ''), 'unknown') as provider,
                       COALESCE(NULLIF(billing_mode, ''), 'unknown') as mode
                FROM sessions
                WHERE started_at >= date('now', 'start of month', '-1 month')
                GROUP BY model, provider, mode
                ORDER BY tokens DESC
            """)
            by_model = [self._row_to_dict(r) for r in cur.fetchall()]

            # Provider-level aggregation
            cur.execute("""
                SELECT COALESCE(NULLIF(billing_provider, ''), 'unknown') as provider,
                       COUNT(*) as sessions,
                       SUM(input_tokens + output_tokens) as tokens,
                       SUM(COALESCE(estimated_cost_usd, 0.0)) as cost,
                       COUNT(DISTINCT model) as models
                FROM sessions
                WHERE started_at >= date('now', 'start of month', '-1 month')
                GROUP BY provider ORDER BY tokens DESC
            """)
            providers = [self._row_to_dict(r) for r in cur.fetchall()]

            # Total tokens and flat-rate tokens
            cur.execute("""
                SELECT COALESCE(NULLIF(billing_mode, ''), 'unknown') as mode,
                       SUM(input_tokens + output_tokens) as tokens,
                       COUNT(*) as sessions
                FROM sessions
                WHERE started_at >= date('now', 'start of month', '-1 month')
                GROUP BY mode
            """)
            mode_breakdown = [self._row_to_dict(r) for r in cur.fetchall()]

            conn.close()

            total_tokens = sum(p["tokens"] for p in providers)
            flat_tokens = sum(m["tokens"] for m in mode_breakdown if m["mode"] == "flat-rate")
            payg_tokens = total_tokens - flat_tokens
            flat_util = (flat_tokens / total_tokens * 100) if total_tokens > 0 else 0
            flat_rate_monthly = 30.0  # OpenCode Go flat

            # Cost per million tokens
            cost_per_provider = []
            for p in providers:
                per_m = (p["cost"] / p["tokens"] * 1_000_000) if p["tokens"] > 0 else 0
                cost_per_provider.append({
                    "provider": p["provider"],
                    "sessions": p["sessions"],
                    "tokens": p["tokens"],
                    "cost": round(p["cost"], 4),
                    "cost_per_1m_tokens": round(per_m, 4),
                })

            # Recommendations
            recommendations = []
            if flat_util < 50 and payg_tokens > 0:
                savings = (payg_tokens / 1_000_000) * 2  # assume $2/1M PAYG cost
                recommendations.append(
                    f"Flat-rate utilization is only {flat_util:.0f}%. "
                    f"Routing more PAYG traffic to OpenCode Go could save ~${savings:.2f}/mo"
                )
            if flat_util > 80:
                recommendations.append(
                    f"Flat-rate at {flat_util:.0f}% — near capacity. "
                    f"Consider a second flat-rate key to avoid spillover to PAYG."
                )

            return jsonify({
                "by_model": by_model,
                "providers": cost_per_provider,
                "flat_rate_utilization": {
                    "percent": round(flat_util, 1),
                    "flat_tokens": flat_tokens,
                    "payg_tokens": payg_tokens,
                    "total_tokens": total_tokens,
                    "flat_rate_monthly_cost": flat_rate_monthly,
                },
                "recommendations": recommendations,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })

        # ── Backend status ──────────────────────────────────────────────────

        @bp.route("/status")
        def status():
            """Return DB status information."""
            conn = self._get_db()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sessions")
            sessions = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM provider_snapshots")
            providers = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM cost_receipts")
            receipts = cur.fetchone()[0]
            cur.execute("SELECT MIN(started_at), MAX(started_at) FROM sessions")
            date_range = cur.fetchone()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            tables = [r["name"] for r in cur.fetchall()]

            conn.close()
            return jsonify({
                "db_path": self.db_path,
                "status": "ok",
                "sessions": sessions,
                "provider_snapshots": providers,
                "cost_receipts": receipts,
                "date_range": {
                    "earliest": date_range[0] if date_range else None,
                    "latest": date_range[1] if date_range else None,
                },
                "tables": tables,
            })


# ── Convenience function for inline use ────────────────────────────────────

def make_api(db_path: str = DEFAULT_DB) -> Blueprint:
    """Create and return a configured AgenticOS API blueprint."""
    return AgenticOSAPI(db_path).blueprint


import os  # noqa: E402 (needed for expanduser in __init__)
