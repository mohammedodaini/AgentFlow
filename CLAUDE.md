# CLAUDE.md — AgentFlow AI session memory

This file is Claude's persistent memory for this project. **Claude: read this
fully at session start; update the "Current position" and "Progress log"
sections whenever a milestone step completes.** The human should not have to
re-explain context, ever.

## The mentorship contract (never violate)

- Claude is a Senior Staff AI Engineer **mentor**, not a code generator. The
  user is an AI student (solid Python) becoming an agentic-AI engineer.
- Build ONE feature at a time. Per milestone, in order:
  **explain concept → build together → quiz → exercise → review the user's
  attempt → refactor together → only then continue.**
- Never skip ahead. Never dump monolithic code. Always explain WHY,
  industry best practices, and tradeoffs. Refuse to jump ahead if
  foundational pieces are missing.
- When creating any file: explain why it exists, how imports/DI/data flow
  work, and why it's structured that way.

## Decisions already made (don't relitigate; new ADR to change)

Stack: Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic ·
PostgreSQL + pgvector · Redis · **arq** (not Celery) · LangGraph · uv ·
ruff + mypy(strict) · pytest · Next.js frontend (scaffolded at M13).

Key calls (rationale in docs/): modular monolith; pgvector over dedicated
vector DB; JWT access+refresh; REST not GraphQL; multi-tenancy via
organizations/memberships from day one; single agent before multi-agent;
human approval = DB record + LangGraph interrupt.

Design docs: docs/folder-guide.md · architecture.md · database.md ·
agents.md · packages.md · roadmap.md (16 milestones) · adr/.

## Mode change (2026-08-11): HYBRID

The user explicitly changed the working agreement this session:

> **M1–M4 are built autonomously by Claude, fully documented.
> M5 onward returns to mentor mode** (the contract above applies again).

Rationale the user accepted: M1–M4 is standard FastAPI/DB/auth/testing
plumbing they already largely know; the actual agentic-AI learning is M5–M12
(RAG, evals, LangGraph, memory, approvals). Do not extend autonomous building
past M4 without asking.

## Current position

- **Phase:** M3 complete (2026-08-12). `make check` green — ruff, ruff format,
  mypy strict over 154 files, **115 tests** passing. Verified at runtime, not
  only in tests. See [docs/milestones/M3-authentication.md](docs/milestones/M3-authentication.md).
- **Next:** M4 — testing discipline (transactional fixtures, factories,
  coverage gate). **M4 is the last autonomous milestone**; mentor mode resumes
  at **M5**, starting with the deferred quiz and exercise below.

### Environment facts learned this session
- `uv` installed at `/opt/homebrew/bin/uv`; venv runs **Python 3.13.2**.
  System python is 3.12 and cannot run this project. Always invoke tools as
  `/opt/homebrew/bin/uv run <cmd>` from `backend/`.
- **Docker runtime = Colima** (user's choice; no Docker Desktop). Start it
  with `colima start` if `docker` errors — the VM does not auto-start on
  boot. `~/.docker/config.json` has `cliPluginsExtraDirs` pointing at
  `/opt/homebrew/lib/docker/cli-plugins` so `docker compose` resolves.
- `make up` verified working: **Postgres 17 + pgvector 0.8.6** and **Redis**
  both healthy from the repo's `docker-compose.yml`.
- The user's IDE points at `/opt/anaconda3` python 3.12 — import errors shown
  there are IDE misconfiguration, not real. It should point at
  `backend/.venv/bin/python`.
- GateGuard hooks demand a "facts" preamble before the first write to each
  file, and again before any destructive shell command (~17 denials in the M1
  session, 30 in the M2 session — it allows **one new file per turn**, so it
  roughly doubles the turn count of any multi-file milestone).
  `ECC_GATEGUARD=off`, or `ECC_DISABLED_HOOKS=pre:edit-write:gateguard-fact-force`,
  disables it if the friction outweighs the value.
- **Test database:** `agentflow_test`, created automatically by
  `tests/integration/conftest.py` (it connects to the `postgres` maintenance DB
  and issues `CREATE DATABASE` if missing). CI already sets `DATABASE_URL` to
  it. Integration tests **skip**, not fail, when Postgres is unreachable.
- `uv run alembic check` is the fast "do the models and the database agree?"
  question — cheaper than generating a migration to find out.

### Still pending (deferred, NOT cancelled — resume at M5)
1. Quiz (unanswered — re-ask when mentor mode resumes):
   - Q1: Why separate `schemas/` from `models/`? What breaks if a route
     returns an ORM object?
   - Q2: Trace "summarize this 40-page PDF" through the layers; why `202`?
   - Q3: Why is `memberships` a table with a `role` column instead of a
     `users.organization_id` column?
2. Exercise issued and unanswered: find ≥5 violations in the bad `POST
   /documents` route (route doing service work; 60s synchronous hold instead
   of 202 + task row; returning an ORM object; missing `organization_id`).
3. Open invitation: challenge arq-vs-Celery or pgvector → new ADR if so.

## Progress log

| Date | What happened |
|---|---|
| 2026-07-10 | Full scaffold created: folder tree, configs (pyproject, compose, Makefile, CI, pre-commit, Dockerfile, .env.example), all design docs. Quiz issued, unanswered. Repo not yet under git. |
| 2026-07-12 | User requested named stubs: all ~110 backend modules created with docstrings (purpose/layer/rules), real imports, and milestone-tagged TODOs (M1–M16). No implementations — bodies are TODO comments; each stub carries `# ruff: noqa: F401`, removed when implemented. Quiz still unanswered; repo still not under git. |
| 2026-08-12 | **M3 implemented and shipped**: Argon2id + JWT primitives, stateless 30-min access tokens vs rotating revocable 7-day refresh tokens (Redis `jti` denylist with self-expiring entries), `AuthService` (register creates user + personal org + owner membership atomically), `CurrentUser`/`CurrentMembership`/`require_role` dependencies, `OrganizationService` with every role rule (admin cannot grant or touch ownership; last owner cannot be demoted or leave), central domain-error→HTTP mapping, 12 endpoints. **Redis moved M5→M3** because logout must actually revoke; readiness gained a Redis probe by the same "probe what you use" rule. Added `pydantic[email]`. 115 tests. Wrote [ADR-0004](docs/adr/0004-stateless-access-tokens-with-rotating-refresh-tokens.md), [ADR-0005](docs/adr/0005-organization-scope-travels-in-a-header.md), [docs/milestones/M3-authentication.md](docs/milestones/M3-authentication.md). |
| 2026-08-12 | **M2 implemented and shipped**: `uuid7()` (RFC 9562 §5.7, hand-rolled — 3.13 has no `uuid.uuid7`), `Base` + naming convention + UUID/timestamp mixins, `organizations`/`users`/`memberships` + `Role` enum, async engine + session-per-request with the commit boundary in `get_db()`, `/health/ready` (Postgres only — Redis waits for M5), Alembic wired async to `Settings` and `Base.metadata` with ruff post-write hooks. 48 tests (30 unit, 9 integration, 9 e2e). Caught 3 real bugs: the Postgres enum that survives `DROP TABLE`, a wrong claim that ids exist before flush, and an index redundant with a composite unique constraint. Wrote [ADR-0003](docs/adr/0003-uuidv7-primary-keys.md) and [docs/milestones/M2-database-layer.md](docs/milestones/M2-database-layer.md). |
| 2026-08-11 | Mode switched to HYBRID (see above). `git init` + baseline commit. Installed `uv`, resolved deps on Python 3.13.2. **M1 implemented and shipped**: config, exceptions, structlog + request-id processor, request-id and timing middleware, `/api/v1/health/live`, v1 router, `create_app()` factory. 12 tests. Full `make check` green. Wrote [ADR-0002](docs/adr/0002-unimplemented-stubs-are-excluded-from-mypy.md) (stub modules get `# mypy: ignore-errors` so the gate can pass before M16) and [docs/milestones/M1-skeleton-that-runs.md](docs/milestones/M1-skeleton-that-runs.md). Also installed 24 `addyosmani/agent-skills` globally and disabled 7 unused marketplace plugin families. |

## Quiz & exercise history

| Milestone | Quiz result | Exercise result | Notes |
|---|---|---|---|
| Architecture | pending | pending | re-ask at next session start |
