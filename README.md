# 🧠 Dealership Brain — FalkorDB Company Brain

**Graph Hacks prep** (WeMakeDevs × FalkorDB, Oct 15–18 2026, $20K pool).
A multi-agent "Company Brain" for a car dealership where **FalkorDB is central**:
every decision reads from the graph, every turn writes back to it.

> ⚠️ **DEMO DATA**: The dealership "Velocity Motors" and ALL customers,
> vehicles, tickets and policies are **fictional** (`app/seed.py`). The UI
> and `/api/status` say this explicitly. Nothing is presented as real.

## 💼 SaaS mode (multi-tenant)

Dealership Brain is also a SaaS product. Any dealership can **sign up in the
UI** ("Start free trial") and gets its **own private workspace**:

- **Accounts** — `app/accounts.py`: email + PBKDF2-hashed password, opaque
  bearer-token sessions (30 days). One account = one workspace.
- **Tenancy** — every workspace is its own graph (`dealership_<slug>` on
  FalkorDB; a separate in-memory graph in serverless fallback mode). No
  workspace can see another's data — verified by tests (signup → starter
  kit → isolation → auth guards, 22 checks).
- **Starter kit** — a new workspace starts EMPTY of customers/vehicles
  (no fictional data) with the owner as staff + a standard policy pack
  (`app/seed.py: seed_workspace`). The fictional dataset exists only in
  the public **demo** workspace.
- **Trial & pricing** — 14-day free trial, no card. Plans: Care $600/mo,
  Growth $1,200/mo, one-time setup $1,500 (see the in-app Pricing view).
  Activation is manual for now.
- **Persistence note** — with a real FalkorDB (`FALKOR_HOST`, e.g. FalkorDB
  Cloud) workspaces persist. On Vercel's in-memory fallback they reset on
  cold start; `/api/status` reports the mode honestly (`saas.persistence`).

## Why this can win (3 tracks, 1 project)

| Track | How we hit it |
|---|---|
| **Company Brain** 🖥️ (M6 Mac Mini) | Knowledge graph of the whole dealership: inventory, customers, staff, policies, tickets, conversation history. Agent answers with full relational context. |
| **Agent Memory & Coordination** 🎮 (PS5 + GTA VI) | Triage → Sales/Service specialists share state via FalkorDB. Every customer remembered across sessions ("Welcome back, Ahmed!"). |
| **Agents That Act on Connected Data** 📱 (iPhone 18 Pro) | Agent ACTS on graph traversals: churn-risk escalation, auto ticket creation, budget-based recommendations. |

Wow-moments for the demo video:
1. Returning customer → agent remembers name, budget, past complaints.
2. "Churn-risk report" → graph finds premium customer with open high-priority ticket → auto-escalation.
3. New phone number → no memory (proves memory is real, per-customer).

## Architecture

```
┌──────────────┐      ┌───────────────────┐      ┌────────────────────┐
│   Chat UI    │─────▶│  Triage Agent     │─────▶│     FalkorDB       │
│ (static/     │◀─────│  (intent classify) │◀─────│  (embedded via     │
│  index.html) │      └─────────┬─────────┘      │   falkordblite OR  │
└──────────────┘                │                │   external server) │
                                ▼                └────────────────────┘
                    ┌───────────────────────┐              ▲
                    │ Sales / Service /     │──reads───────┘
                    │ Policy specialists    │──writes──────▶┘
                    └───────────────────────┘   (conversations,
                      ▲ shared graph state     tickets, memory)
```

## Setup

```bash
cd ~/workspace/graph-hacks
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# Seed fictional demo data
./venv/bin/python -m app.seed

# Run
./venv/bin/python -m app.main
# → http://localhost:8125
```

**FalkorDB modes** (both run a REAL FalkorDB engine, real Cypher):
- `embedded` (default): `falkordblite` — zero setup, data in `falkordb-data/brain.db`.
  Used for local dev/demo. Chosen because Docker Hub is unreachable from this VM.
- `external`: set `FALKOR_HOST` (+`FALKOR_PORT`, `FALKOR_PASSWORD`) to use
  FalkorDB Cloud (free tier: app.falkordb.cloud) or `docker run -p 6379:6379 falkordb/falkordb`.
  Recommended for the hackathon submission.

**LLM modes** (`LLM_PROVIDER`, see `.env.example`):
- `heuristic` (default): free, offline, pattern-based. Every reply labeled.
- `openai`: any OpenAI-compatible API. Stronger demo story for judges.

## What's real vs stubbed

| Component | Status |
|---|---|
| FalkorDB engine + Cypher queries | ✅ REAL (embedded `falkordblite`, real graph traversals) |
| Graph schema (Customer/Vehicle/Ticket/Policy/Conversation/Staff) | ✅ REAL |
| Triage → specialist routing | ✅ REAL (intent classifier + 3 specialist agents) |
| Cross-session memory | ✅ REAL (per-phone conversation history in graph) |
| Churn-risk detection + escalation | ✅ REAL (Cypher aggregation → action) |
| SLA escalation alerts (n8n) + Alert Log audit trail | ✅ REAL (webhook POST when `N8N_WEBHOOK_URL` set; every attempt logged as `(:Alert)` newest-first) |
| Promise Watch agent (Graph Hacks Day-1) | ✅ REAL (sweep: reminders for due-soon promises, churn-style escalation for overdue, dedupe via Alert Log; `GET /api/promise/tracking`, `POST /api/promise/watch`) |
| Demo dataset | ⚠️ FICTIONAL (clearly labeled everywhere) |
| LLM replies (default) | ⚠️ HEURISTIC (labeled `heuristic`; swap to `openai` via env) |

## Graph schema

```
(:Customer)-[:INTERESTED_IN|VIEWED|OWNS]->(:Vehicle)
(:Customer)-[:REPORTED]->(:Ticket)
(:Customer)-[:HAD]->(:Conversation)
(:Staff {role})  (:Policy {topic, content})
(:Alert {event, ref_id, outcome}) — audit trail of every escalation attempt
```

## 4-day build plan (Oct 15–18)

**Day 1 (Oct 15) — Graph day**
- [ ] Decide: FalkorDB Cloud free tier vs embedded (embedded already works)
- [ ] Expand schema: service history, test-drive bookings, financing applications
- [ ] Write the "new work" commit history (hackathon rule: FalkorDB work must be new)

**Day 2 (Oct 16) — Agents day**
- [x] Real LLM wired (`LLM_PROVIDER=auto` + free Groq key — see LLM_SETUP.md, no card needed)
- [ ] Add 4th agent: FollowUp agent (proactive outreach to churn-risk customers)
- [ ] Multi-agent debate trace in UI (show which agent said what)

**Day 3 (Oct 17) — Demo day**
- [ ] Polish UI: graph visualization of the customer's neighborhood (vis.js)
- [ ] Record <3 min demo video: returning-customer wow → churn escalation → manager view
- [ ] Write the blog post (side prize: AirPods 5 for best blog!)

**Day 4 (Oct 18) — Ship day**
- [ ] Public GitHub repo + README + demo video link
- [ ] Submit on wemakedevs.org/hackathons/falkordb before deadline
- [ ] Post blog, share on socials

## API quick reference

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` `{phone,name,message}` | Full agent loop + trace |
| `GET /api/churn` | Churn-risk report (manager view) |
| `GET /api/graph/stats` | Live node counts per label |
| `POST /api/seed` | Reset fictional demo data |
| `GET /api/status` | Honest capability report |
