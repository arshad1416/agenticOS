# Research Brief: AgenticOS Dashboard — Architecture, Components & Build Plan

## Summary
An AgenticOS (Agentic Operating System) is an emerging architectural pattern where AI agents, orchestrated by a central LLM "brain," function as a personal OS over one's digital and AI infrastructure. The existing dashboard at `usage.arshadkazi.ca` provides foundational model-usage analytics (per-model breakdowns, monthly costs, recent sessions) but lacks critical AgenticOS components: the **brain** (model routing), **skills** registry, **kanban/task** board, **cron job** monitoring, **token utilization** metrics, **live cost reconciliation**, and **system health** views. This brief maps the complete AgenticOS dashboard architecture and identifies what to build next.

## Confidence Level
**High** — based on direct inspection of the running dashboard, Hermes Agent CLI features, the Pi's data pipeline, and deep research via Perplexity Sonar Deep Research ($0.12 cost, 14k output tokens, cited sources below).

## Current State Assessment

### What Exists (`usage.arshadkazi.ca`)
| Component | Status | Notes |
|-----------|--------|-------|
| Per-model breakdown (sessions, cost) | ✅ Works | Table + Today/All Time toggle |
| Daily trend (14 days) | ✅ Works | Sessions or Cost toggle via Chart.js |
| Weekly trend (8 weeks) | ✅ Works | Sessions or Cost toggle via Chart.js |
| Monthly cost table | ✅ Works | Shows sessions, PAYG cost, flat sub, total |
| Monthly cost by provider | ✅ Works | Per-provider (OpenRouter, OpenCode Go, Claude, Gemini) |
| Cost breakdown by model | ✅ Works | Sorted by cost descending |
| Recent 20 sessions | ✅ Works | Time, model, provider, msgs, tokens, cost |
| Live cost data from APIs | ❌ Missing | Cost data is hardcoded (receipt values, not live API) |
| Provider routing | ❌ Half-built | Provider routing template exists but not integrated fully |

### What Hermes Agent Already Provides (but not in dashboard)
| Hermes CLI Feature | Current State | Dashboard Integration |
|--------------------|--------------|----------------------|
| `hermes insights` | ✅ Working — token/session/skill/tool analysis per profile | ❌ Not piped into dashboard |
| `hermes kanban` | ✅ Working — full task board (currently 0 tasks) | ❌ Not in dashboard |
| `hermes cron` | ✅ Working — no jobs on Mac (Pi has separate system cron) | ❌ Not in dashboard |
| `hermes sessions` | ✅ Working — 44 sessions, 59.5MB SQLite store | ❌ Export exists but not consumed |
| `hermes dashboard` | ✅ Built-in web UI (briefly ran, not currently accessible) | ❌ Separate from usage.arshadkazi.ca |
| `hermes skills` | ✅ Working — 10 distinct skills tracked | ❌ Not in dashboard |

### Data Pipeline (Current)
```
Mac (collectors) → JSONL snapshots → rsync/scp to Pi → Flask app → Cloudflare Tunnel → usage.arshadkazi.ca
```

**Collectors on Mac:**
- Claude, Codex, Terminal session collectors (via launchd, every 6h)
- Output: JSONL files in `~/usage-analytics/snapshots/hermes/`

**On Pi:**
- `hermes-provider-collect-pi.py` — Hermes provider tracking
- `aggregate.py`/`aggregate.sh` — JSONL aggregation
- `app.py` (1566 lines) — Flask dashboard serving on port 8080
- `provider_routing.html` — Provider routing template (built but not fully wired)
- `seed_data.json` — Fallback historical data

### Cost Data (Current)
Hardcoded values in `app.py` (not live from APIs):
- OpenCode Go: $78.69/mo (3 accounts)
- OpenRouter: $128.10 total from 9 receipts (May-Jun)
- Claude Pro: $28/mo + $20 extra
- Gemini: $39.21/mo

---

## What an AgenticOS Dashboard Should Include

### 1. Brain Tab — Model Routing & Performance
**What to show:**
- **Model routing visualization** — Sankey diagram showing: intent → profile → model → provider → cost
- **Model performance** — Latency, error rate, tokens/session per model (from Hermes insights data)
- **Routing policies** — Current routing rules (from config.yaml), which models are flat-rate vs PAYG
- **Model switching history** — Which models were used in which sessions, why routing decisions were made
- **Fallback chain status** — How often fallbacks trigger (primary → OpenRouter → Gemini → Ollama)

**Data sources:** `~/.hermes/profiles/researcher/config.yaml` (routing), `hermes insights --days 30` (perf metrics), provider tracking JSONL

### 2. Skills Tab — Registry & Usage
**What to show:**
- **Skills registry** — All skills with name, description, category, last used date, load count
- **Skill effectiveness** — Which skills produce good outputs (feedback scoring), error rates per skill
- **Skill dependency graph** — Which skills depend on which tools/providers
- **Usage heatmap** — When each skill is most frequently loaded (time of day, day of week)
- **Skill version history** — When skills were last updated (from curator)

**Data sources:** `hermes insights` (skill loads/edits), `~/.hermes/skills/` directory scan, curator logs

### 3. Kanban / Tasks Tab
**What to show:**
- **Board view** — Columns: Backlog → Todo → Ready → Running → Blocked → Done (from `hermes kanban`)
- **Task cards** — Title, assignee, priority, age, dependencies, skills linked
- **Completion metrics** — Tasks done/week, cycle time, blocker frequency, profile distribution
- **Task ↔ Session links** — Which sessions were spawned from which tasks
- **Worker status** — Active workers per profile

**Data sources:** `hermes kanban boards list`, `hermes kanban stats`, `hermes kanban list`

### 4. Cron / Jobs Tab
**What to show:**
- **Job registry** — All cron jobs across both Mac (Hermes cron) and Pi (system cron)
- **Execution history** — Last N runs per job: status, duration, output summary
- **Reliability metrics** — Success rate, avg runtime, error rate per job
- **Job ↔ Skill links** — Which skills each job uses
- **Scheduler status** — Is cron daemon running on each machine?

**Data sources:** `hermes cron list` (Mac), Pi's `/var/log/syslog` + scraping inventory cron job

### 5. Sessions Tab (Enhanced)
**What to show:**
- **Profile filter** — Toggle between researcher, default, subagent sessions
- **Platform filter** — TUI/CLI, Telegram, cron, subagent
- **Machine filter** — Mac vs Pi sessions
- **Session details** — Expandable view with messages, tool calls, tokens, cost
- **Session search** — Full-text search across session history
- **Session → Task linking** — Which task spawned each session

**Data sources:** `hermes sessions list`, `hermes sessions export`, JSONL snapshot files

### 6. Token Utilization Tab
**What to show:**
- **Per-model token consumption** — Total, average, max per session
- **Input/Output split** — How much is input vs output per model (utilization efficiency)
- **Context window utilization** — % of context window used per session (flag near-full sessions)
- **Token efficiency** — Tokens per task, tokens per tool call, tokens per message
- **Wasted token analysis** — Sessions that consumed many tokens but produced little output

**Data sources:** Session records (input/output token counts from JSONL), Hermes insights

### 7. Cost Analysis Tab (Replacement for Current)
**What to show:**
- **Live cost vs estimated** — Real API costs (from OpenRouter API, receipts) vs token-based estimates
- **Flat-rate ROI** — For OpenCode Go: "you paid $X flat, would have cost $Y PAYG — saving $Z"
- **Per-account breakdown** — Three OpenCode Go subscriptions tracked separately
- **Daily cost trend** — Cost per day across all providers (not just 14 days)
- **Forecast** — Projected month-end cost based on current burn rate
- **Budget alerts** — Highlight when approaching thresholds

**Data sources:** OpenRouter `/api/v1/usage` (live), hardcoded receipts, `hermes insights` cost estimates

### 8. System Health Tab
**What to show:**
- **Machine status** — Mac M5 online/offline, Pi online/offline, Tailscale status
- **API status** — OpenRouter reachable, OpenCode Go reachable, Claude Proxy reachable, Gemini reachable
- **Cron health** — Last successful run per cron job, staleness detection
- **Disk/Storage** — Dashboard data directory size, growth rate
- **Error rate** — Failed sessions, failed cron jobs, failed API calls (last 24h)
- **Tunnel status** — Cloudflare Tunnel connected/disconnected, traffic

**Data sources:** SSH reachability pings, API health checks, cron execution logs

---

## Architecture Recommendations

### Data Pipeline (Proposed)
```
Mac Hermes DB ─→ hermes sessions export → JSONL ─┐
Mac insights  ─→ hermes insights --json → JSON  ──┤
Mac collectors ─→ JSONL snapshots ────────────────┤
Pi collectors  ─→ Provider tracking JSONL ────────┤
OpenRouter API ─→ /api/v1/usage → JSON ──────────┤
                                                   ↓
                                             Flask Aggregator
                                             (aggregate.py)
                                                   ↓
                                           SQLite Database
                                           (usage.db)
                                                   ↓
                                             Flask API
                                             (REST endpoints)
                                                   ↓
                                  +---------------++---------------+
                                  |                                |
                          JSON API (port 8081)           Dashboard (port 8080)
                          For external consumers         Render HTML + Chart.js
                                  |                +--------------+
                                  |                |
                     Cloudflare Tunnel ───── usage.arshadkazi.ca
```

### Recommended Tech Stack
| Layer | Current | Recommended | Rationale |
|-------|---------|-------------|-----------|
| **Backend** | Flask (1566-line app) | Flask (refactored) | Already works, no rewrite needed |
| **Frontend** | Inline HTML + JS | Flask templates + Chart.js + Alpine.js | Keep it simple, avoid npm build step on Pi |
| **Database** | JSONL files | SQLite (`usage.db`) | Queryable, atomic writes, concurrent access |
| **Auth** | None on usage.arshadkazi.ca | Cloudflare Access (Zero Trust) + Dashboard basic auth | Free for <50 users, enforces identity |
| **Deployment** | Flask on Pi :8080 | Flask behind Cloudflare Tunnel | Already working, just add auth |
| **Data Collection** | JSONL push from Mac | `hermes sessions export` + cron-driven sync | More reliable, includes full session data |

### Key Thing to Build in Phase 1
1. **Consolidate data sources** — Pipe `hermes insights` output, `hermes sessions export`, `hermes kanban stats`, `hermes cron list` all into the same database
2. **Add SQLite storage** — Replace JSONL ad-hoc loading with an SQLite DB that gets built incrementally from collectors
3. **New dashboard tabs** — Brain, Skills, Kanban, Cron, Health tabs alongside the existing cost analytics
4. **Live cost data** — Fetch OpenRouter usage API ($0 cost) instead of hardcoded receipt values
5. **Auth** — Deploy Cloudflare Access in front of usage.arshadkazi.ca

### Key Thing to Build in Phase 2
1. **Real-time WebSocket updates** — Push cron job completions, session starts, and kanban changes live
2. **Multi-machine views** — Show Mac + Pi data side by side
3. **Drill-down** — Click from cost summary → model detail → session list → single session trace
4. **Alerting** — Budget alerts, cron failure alerts, system health alerts via Telegram
5. **Export/Share** — Dashboard pages as shareable links

---

## Sources
- [Hermes Agent Documentation](https://hermes-agent.nousresearch.com/docs) — Nous Research, current as of July 2026
- [Hermes Agent Dashboard](https://hermes-agent.nousresearch.com/docs/features/dashboard) — Dashboard configuration and auth docs
- [LangSmith Platform](https://www.langchain.com/langsmith) — LangChain, 2025-2026. LLM observability and agent engineering platform
- [Langfuse Open Source](https://langfuse.com/) — Open-source AI engineering platform
- [LangGraph Framework](https://langchain-ai.github.io/langgraph/) — LangChain, 2025-2026. Agent runtime and orchestration
- [AgentOS by Agno](https://github.com/agno-agi/agentos) — Pre-built FastAPI app for agentic systems
- [OpenTelemetry Tracing](https://opentelemetry.io/docs/concepts/signals/traces/) — CNCF, distributed tracing standards
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — Secure service exposure
- [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/) — Zero Trust access control
- [AWS Multi-tenant LLM Analytics](https://aws.amazon.com/blogs/industries/architecting-multi-tenant-llm-analytics-systems-with-row-level-security/) — AWS, 2025. Reference architecture
- "Personal Agentic Operating System" talk series — Various AI conferences, 2025-2026 (synthesized from deep research, not directly URL-accessible)

## Gaps & Limitations
1. **No public open-source reference implementation** of a complete AgenticOS dashboard exists — this is an emerging pattern with no canonical implementation to follow
2. **LangSmith pricing** is per-workspace (not per-user), may be overkill for a single-person AgenticOS; Langfuse open-source is a better fit but adds deployment complexity
3. **Live API cost data** from OpenRouter requires an API endpoint that may have rate limits or may not provide per-session cost breakdowns (only aggregate)
4. **Hermes kanban is unused** currently (0 tasks) — no historical data to validate the kanban view design
5. **Pi system cron** jobs are separate from `hermes cron` — need a bridge to monitor Pi cron jobs from within Hermes
6. **Session token data** varies by record format — some records have full token breakdowns (input/output), others only total tokens. Estimation logic may be inaccurate
7. **Deep research sources** from Perplexity Sonar Deep Research were synthesized from the model's web search capabilities; URLs are not individually citable since the model returns synthesized answers, not document-level citations

---

**Next Action:** The research brief above maps the complete AgenticOS dashboard. The user should indicate which tab/section to build first. Recommended priority:
1. **Phase 1a: SQLite consolidation + Hermes data integration** (foundation for everything else)
2. **Phase 1b: Cron job tracking** (immediate value — currently no monitoring)
3. **Phase 1c: Skills tab** (low effort, high value — data already in `hermes insights`)
4. **Phase 1d: Kanban tab** (visualize task board — data already in `hermes kanban`)
5. **Phase 2: Brain/Metrics, System Health, Token Utilization, Live Cost**
