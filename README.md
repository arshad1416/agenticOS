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
| Dashboard | `/` | Provider routing overview |
| Kanban | `/kanban` | Task board |
| Cron | `/cron` | Cron job monitoring |
| Skills | `/skills` | Skill registry & usage |
| Budget | `/budget` | Cost receipts, live costs, forecasting |
| Routing | `/fuel` | Model routing intelligence |
| Tokens | `/tokens` | Token utilization analytics |
| Sessions | `/sessions` | Session explorer with search/filter |
| Health | `/health` | System health (20+ metrics) |
| Changelog | `/changelog` | Git commit log |
| Brain | `/brain` | Second Brain / Knowledge Graph |

## API Endpoints

All at `/api/v2/`:

| Endpoint | Description |
|----------|-------------|
| `/status` | DB health & record counts |
| `/stats` | Aggregated usage analytics |
| `/sessions` | Session list |
| `/sessions/explore` | Session search/filter/paginate |
| `/cron` | Cron job list |
| `/skills` | Skills list |
| `/kanban` | Kanban tasks |
| `/brain` | Model routing intelligence |
| `/tokens` | Token utilization metrics |
| `/costs` | Cost receipts |
| `/costs/live` | Live OpenRouter cost reconciliation |
| `/costs/monthly` | Monthly cost breakdowns |
| `/providers` | Provider routing data |
| `/health` | System health |
| `/changelog` | Git changelog |
| `/routing/analysis` | Flat-rate vs PAYG optimizer |

## Tech Stack

- **Backend:** Python 3.11, Flask, SQLite
- **Frontend:** Chart.js, Jinja2, dark theme
- **Infrastructure:** Raspberry Pi 5, Cloudflare Tunnel
- **Collection:** Hermes Agent, launchd, shell collectors

## Database

11 tables in `agentic_os.db`: sessions (185), provider_snapshots (10,196), cost_receipts (23), cron_jobs (47), skills (21), kanban_tasks (4), daily_costs, monthly_costs, sync_meta, schema_version.

## Cost Tracking

- Live OpenRouter API fetch every 10 minutes
- Receipt reconciliation vs live spend
- Budget alerts at $300/mo via Telegram
- Burn rate forecasting for EOM projections
- Flat-rate vs PAYG routing analysis

## License

MIT
