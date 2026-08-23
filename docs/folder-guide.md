# Folder Guide: why every directory exists

The repo is a **monorepo**: backend, frontend, infrastructure, and docs live
together. For a small team this maximizes velocity (one PR can change API +
client + docs atomically). Splitting into multiple repos is a scaling decision
you defer until you have multiple teams.

```
AgentFlow/
├── .github/workflows/     CI/CD pipelines
├── docs/                  Architecture docs + ADRs
├── docker/                Extra Docker assets (init scripts, prod compose overrides)
├── infra/                 Deployment config (IaC, k8s manifests), later milestone
├── scripts/               One-off developer/ops scripts (seeding, backfills)
├── frontend/              Next.js app (scaffolded at its milestone)
└── backend/
    ├── pyproject.toml     Deps + tool config (single source of truth)
    ├── Dockerfile         Production image
    ├── alembic/           Database migrations
    ├── tests/             unit/ integration/ e2e/
    └── app/               The application package
```

## `backend/app/`: layer by layer

The golden rule: **dependencies point inward and downward.** Routes call
services; services call repositories and integrations; nothing lower ever
imports from a layer above it. Circular imports are a symptom of violating
this rule, not a Python quirk to work around.

### `core/`
Cross-cutting foundations: `config.py` (Pydantic Settings reading `.env`),
`exceptions.py` (domain exception hierarchy), `security.py` (hashing, JWT
helpers). **Why:** every other module needs these; they must depend on
nothing else in the app.
*Common mistake:* letting `core/` import from `services/`: it must stay a leaf.

### `api/v1/routes/`
FastAPI routers only: parse request → call a service → shape the response.
**Why versioned:** when you must break the API, `v2/` lets old clients keep
working. *Common mistake:* business logic in route handlers. A route function
longer than ~15 lines is a smell; move logic into a service.

### `schemas/`
Pydantic models for the API boundary (`UserCreate`, `UserRead`, …).
**Why separate from `models/`:** the DB shape and the API shape evolve
independently. Returning ORM objects directly leaks columns (e.g. password
hashes) and welds your API contract to your schema migrations.

### `models/`
SQLAlchemy ORM classes: table definitions, relationships, constraints.
No behavior beyond simple properties. One file per aggregate (`user.py`,
`document.py`, …).

### `db/`
Engine, async session factory, and the FastAPI dependency that yields a
session per request. **Why:** exactly one place in the codebase knows how to
connect to the database.

### `repositories/`
Query encapsulation per aggregate (`UserRepository.get_by_email`, …).
**Why (and when):** we use it *only* where queries get complex (documents,
embeddings, agent runs). For trivial CRUD a repository is ceremony: the
brief says "repository pattern only when justified", and we honor that.

### `services/`
Business logic and transactions: "register a user" = check email uniqueness +
hash password + insert + emit event. Services orchestrate repositories,
integrations, and workers. **Why:** logic living here is callable from routes,
workers, CLI scripts, and tests alike.

### `middleware/`
ASGI middleware: request-ID injection, timing, rate limiting.
*Common mistake:* auth logic here instead of FastAPI dependencies
dependencies are testable and per-route; middleware is all-or-nothing.

### `auth/`
Authentication domain: JWT issuing/verification, password flows, and the
`get_current_user` dependency. Separated from `core/security.py` (primitives)
because auth is a *feature* with routes and services of its own.

### `integrations/`
One subpackage per external product (gmail, google_calendar, google_drive,
slack, notion, github, stripe). Each exposes a thin client class the services
layer consumes; OAuth token handling stays in each integration.
**Why:** when Slack changes its API, the blast radius is one folder.
*Common mistake:* letting integration types (e.g. a Gmail message dict) leak
into services, translate to your own domain schemas at the boundary.

### `agents/`
LangGraph graphs, one subpackage per agent (planner, research, rag, email,
calendar, proposal, memory, supervisor, evaluation). Agents *use* services and
integrations as tools; they never talk to the DB or external APIs directly.
See [agents.md](agents.md).

### `rag/`
The retrieval pipeline: ingestion (PDF parsing), chunking, embedding,
retrieval/reranking. Separate from `agents/` because RAG is infrastructure the
RAG *agent* consumes, you'll want to test and tune retrieval quality without
running any agent.

### `memory/`
Long-term agent memory: writing conversation facts, retrieving relevant
memories, decay/summarization policies. Distinct from `rag/` (documents the
*business* uploaded), memory is what the *agent* learned.

### `prompts/`
Prompt templates as files, not string literals scattered through code.
**Why:** prompts are configuration that non-engineers may edit, they need
versioning/diffing, and centralizing them enables evaluation.

### `workers/`
Background tasks (arq): document ingestion, email sync, long agent runs.
**Why:** anything slower than ~1s doesn't belong in an HTTP request: the API
enqueues, the worker executes, the client polls or gets a webhook/WebSocket.

### `logging/`, `monitoring/`
structlog configuration and processors; health checks, metrics, tracing setup.
Kept out of `core/` so observability concerns are swappable.

### `evaluation/`
Eval harness for agents and RAG: golden datasets, LLM-as-judge scoring,
regression tracking. **Why it's a first-class folder:** in AI products,
evals are your test suite for model behavior, teams that skip this ship
regressions with every prompt tweak.

### `utils/`
Small, pure, dependency-free helpers only. *Common mistake:* `utils/` as a
junk drawer. Rule: if a helper knows about your domain, it belongs in that
domain's module instead.

## `backend/tests/`
- `unit/`: no I/O, milliseconds, run constantly.
- `integration/`: real Postgres/Redis from docker-compose.
- `e2e/`: full API flows via HTTP client.
Mirrors `app/` structure so every module's tests are findable in one guess.
