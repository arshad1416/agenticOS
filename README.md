# AgenticOS

A self-hosted, AI-agentic operating system dashboard for monitoring and managing AI infrastructure, model usage, costs, and system health across multiple machines and providers.

## Architecture

```
┌─────────────────────┐     ┌──────────────────────────────┐
│   Mac M5 (Source)   │     │   Raspberry Pi 5 (Server)    │
│                     │     │                              │
│  hermes sessions    │ ──→ │  Aggregator (cron */10)      │
│  export → JSONL     │     │  ↓                           │
│  Shell history      │     │  SQLite DB (11 tables)       │
│  Provider tracking  │     │  ↓                           │
│                     │     │  Flask API (/api/v2/)        │
│                     │     │  ↓                           │
│                     │     │  Dashboard (Chart.js)        │
│                     │     │  ↓                           │
│                     │     │  Cloudflare Tunnel           │
│                     │     │  → usage.arshadkazi.ca       │
└─────────────────────┘     └──────────────────────────────┘
```

## Dashboard Tabs

| Tab | Route | Description |
|-----|-------|-------------|
| **Dashboard** | `/` | Provider routing overview |
| **Kanban** | `/kanban` | Task board (Hermes kanban) |
| **Cron** | `/cron` | Cron job monitoring |
| **Skills** | `/skills` | Skill registry & usage |
| **Budget** | `/budget` | Cost receipts, live costs, forecasting |
| **Routing** | `/fuel` | Model routing intelligence |
| **Tokens** | `/tokens` | Token utilization analytics |
| **Sessions** | `/sessions` | Session explorer with search/filter |
| **Health** | `/health` | System health (20+ metrics) |
| **Changelog** | `/changelog` | Git commit log |
| **Brain** | `/brain` | Second Brain / Knowledge Graph |

## API Endpoints

All endpoints live at `/api/v2/`:

| Endpoint | Description |
|----------|-------------|
| `/status` | DB health & record counts |
| `/stats` | Aggregated usage analytics |
| `/sessions` | Session list |
| `/sessions/explore` | Session search/filter/paginate |
| `/cron` | Cron job list (47 jobs) |
| `/skills` | Skills list (21 entries) |
| `/kanban` | Kanban tasks (4 tasks) |
| `/brain` | Model routing intelligence |
| `/tokens` | Token utilization metrics |
| `/costs` | Cost receipts |
| `/costs/live` | Live OpenRouter cost reconciliation |
| `/costs/monthly` | Monthly cost breakdowns |
| `/providers` | Provider routing data |
| `/health` | System health (20 metrics) |
| `/changelog` | Git changelog (30 commits) |
| `/routing/analysis` | Flat-rate vs PAYG optimizer |

## Data Pipeline

### Collection (Mac — launchd)

- `hermes-session-collect.py` — Exports Hermes sessions, writes enriched JSONL
- `mac-collect-all.sh` — Orchestrates Claude, Codex, terminal collectors
- Runs via launchd every 30 minutes

### Aggregation (Pi — cron)

- `agentic-os-aggregator.py` — Merges JSONL snapshots + provider tracking + cost receipts → SQLite
- Runs every 10 minutes via crontab

### Serving (Pi — systemd)

- `agentic_os_api.py` — Flask blueprint with 16 API endpoints
- `app.py` — Flask dashboard with template routes
- `usage-dashboard.service` — systemd service on port 8080

## Database Schema

11 tables in `agentic_os.db`:

| Table | Records | Description |
|-------|---------|-------------|
| `sessions` | 185 | AI session records |
| `provider_snapshots` | 10,196 | Provider-level usage snapshots |
| `cost_receipts` | 23 | Actual billing receipts |
| `cron_jobs` | 47 | Parsed Pi crontab entries |
| `cron_runs` | — | Cron execution history |
| `skills` | 21 | Derived skill entries |
| `kanban_tasks` | 4 | Hermes kanban tasks |
| `daily_costs` | — | Daily cost rollups |
| `monthly_costs` | 3 | Monthly cost rollups |
| `sync_meta` | — | Sync tracking |
| `schema_version` | — | Schema migrations |

## Cost Tracking

- **Live OpenRouter API** — Real-time spend fetched every 10 minutes
- **Receipt reconciliation** — 23 receipts matched against live data
- **Budget alerts** — Weekly Monday tripwire at $300/mo with Telegram notifications
- **Forecasting** — Burn rate projection for end-of-month estimates
- **Routing optimizer** — Flat-rate vs PAYG savings analysis

## System Health

20 metrics monitored:
- Pi uptime, CPU load (1/5/15 min), memory, disk
- Mac reachability (Tailscale)
- API health: OpenRouter, OpenCode Go, Claude proxy
- Cloudflare tunnel status
- Dashboard service status
- Aggregator last run time
- Database size & row counts per table
- Python process count

## Tech Stack

- **Backend:** Python 3.11, Flask, SQLite
- **Frontend:** Chart.js, Jinja2 templates, dark theme
- **Infrastructure:** Raspberry Pi 5, Cloudflare Tunnel, systemd
- **Authentication:** Flask session auth, Cloudflare Access (optional)

## Deployment

```bash
# Pi setup
ssh pi "mkdir -p ~/usage-dashboard ~/usage-analytics"
scp src/* pi:~/usage-dashboard/
ssh pi "sudo cp usage-dashboard.service /etc/systemd/system/"
ssh pi "sudo systemctl enable --now usage-dashboard.service"

# Cron jobs
ssh pi "(crontab -l 2>/dev/null; echo '*/10 * * * * python3 ~/usage-analytics/scripts/agentic-os-aggregator.py') | crontab -"

# Cloudflare Tunnel
ssh pi "cloudflared tunnel create usage-tunnel"
ssh pi "cloudflared tunnel route dns usage-tunnel usage.arshadkazi.ca"
ssh pi "cloudflared tunnel run usage-tunnel"
```

## License

MIT
