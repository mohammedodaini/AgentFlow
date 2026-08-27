# System Architecture

## Bird's-eye view

```
                         ┌──────────────┐
                         │     User     │
                         └──────┬───────┘
                                │ HTTPS
                 ┌──────────────▼───────────────┐
                 │   Frontend (Next.js, TS)     │
                 │   UI, session, optimistic UX │
                 └──────────────┬───────────────┘
                                │ REST /api/v1 (JWT)
┌───────────────────────────────▼───────────────────────────────────┐
│                      FastAPI application                          │
│  middleware (request-id, CORS, rate limit)                        │
│  routes ──► services ──► repositories ──► PostgreSQL (+pgvector)  │
│                │  │                                               │
│                │  └──► integrations ──► Gmail/Slack/Notion/…      │
│                └────► enqueue job ───► Redis                      │
└───────────────────────────────┬───────────────────────────────────┘
                                │ Redis queue (arq)
                 ┌──────────────▼───────────────┐
                 │        Worker process        │
                 │  ingestion, sync, agent runs │
                 │  LangGraph graphs ──► LLM    │
                 │  embeddings ──► pgvector     │
                 └──────────────────────────────┘
```

## Layer responsibilities

| Layer | Owns | Never does |
|---|---|---|
| Frontend | rendering, client state, calling API | business rules, secrets |
| Routes | HTTP concerns: parsing, status codes, auth deps | business logic, SQL |
| Services | business rules, transactions, orchestration | HTTP details, raw SQL strings |
| Repositories | query construction for complex aggregates | business decisions |
| Integrations | external API clients, OAuth token refresh | leaking vendor types upward |
| Workers | long-running/background execution | serving HTTP |
| Agents | LLM reasoning over tools | direct DB/API access (tools wrap services) |

## Key flows

**Synchronous request** (e.g. "list my documents"):
browser → route (JWT dependency resolves user) → service → repository →
Postgres → Pydantic schema → JSON. Target: <100 ms.

**Asynchronous work** (e.g. "ingest this PDF" or "run an agent task"):
route validates + creates a `tasks` row + enqueues to Redis → returns `202`
with a task id → worker picks it up, updates task status/progress → client
polls `GET /tasks/{id}` (later: WebSocket push).
**Why:** LLM calls take seconds-to-minutes; holding HTTP connections that long
wastes workers and breaks through proxies/timeouts.

**Human-in-the-loop approval:** an agent that wants to *send* an email or
*create* a calendar event writes a `pending_approval` action row and pauses
(LangGraph interrupt). The user approves in the UI; the graph resumes from its
checkpoint. Dangerous side effects never happen without an explicit approval
record.

## Architectural decisions (with tradeoffs)

1. **Modular monolith, not microservices.** One deployable API + one worker.
   Microservices buy independent scaling/teams at the cost of distributed-
   systems complexity; at our size that cost has no payoff. The layer
   boundaries mean we *can* extract services later.
2. **pgvector, not a dedicated vector DB.** One database to operate, ACID
   joins between embeddings and business data, one backup story. Tradeoff:
   at ~10M+ vectors dedicated engines (Qdrant/Weaviate) win on speed; the
   `rag/` layer isolates that swap.
3. **arq, not Celery.** Our stack is async end-to-end; arq is Redis-native
   and asyncio-native, tiny surface area. Celery is more battle-tested and
   feature-rich (beat schedules, complex routing) but sync-first and heavy.
4. **JWT (access + refresh), not server sessions.** Stateless auth scales
   horizontally and serves SPA + future mobile clients. Tradeoff: revocation
   needs a Redis denylist, which we implement in the auth milestone.
5. **REST, not GraphQL.** Our access patterns are simple and known; REST +
   OpenAPI gives free typed clients and docs. GraphQL pays off when many
   clients need flexible queries: we don't have that problem.
