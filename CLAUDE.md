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

- **Phase:** **M1–M5 complete (2026-08-12).** `make check` green — ruff, ruff
  format, mypy strict over **158 files**, **236 tests**, **98.9% coverage**
  (gate at 97%). Pyramid: unit 128 / integration 41 / e2e 67.
  See [docs/milestones/M5-document-upload.md](docs/milestones/M5-document-upload.md).
- **M5 was built autonomously at the user's explicit request** ("continue with
  m5 and finish it"), which overrode the mentor-mode resumption recorded here
  after M4. **That override was for M5 only.**
- **Next: M6 (RAG pipeline) — ASK FIRST.** Do not assume the M5 override
  extends to it. Ask whether M6 should be mentor mode (the contract at the top
  of this file) or another autonomous build.
- **The deferred quiz and exercise are still unanswered** — see "Still pending"
  below. They have now been carried across three sessions. Re-ask them at the
  start of the next one, before any M6 work.

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
- **From M5 the app needs two processes.** `make dev` alone leaves every upload
  stuck at `status=pending`; `make worker`
  (`arq app.workers.settings.WorkerSettings`) is what moves them. The Makefile
  target used to point at `app.workers.main`, a module that never existed.
- Uploaded bytes go to `backend/var/storage/` (gitignored). Tests get a
  `tmp_path` instead, via `STORAGE_LOCAL_PATH` in the autouse env fixture.
- GateGuard's edit/write gate fires on the **first attempt** at each new file
  and allows the **retry**, so a multi-file milestone costs roughly two tool
  calls per file. It denied 38 times across M5.

### Still pending — ASK THESE FIRST at the start of the M5 session
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
| 2026-08-12 | **M5 implemented and shipped** (autonomous, at the user's request): `documents` + `tasks` tables, `app/storage/` (an `ObjectStorage` protocol + filesystem backend, tenant-first keys), pypdf/text extraction, tenant-scoped `DocumentRepository`, `DocumentService` (allowlisted MIME types, size cap enforced *while reading*, orphaned-object cleanup), the arq producer/worker pair, and four endpoints on the 202-then-poll pattern. **The load-bearing decision: nothing is enqueued until its transaction commits.** The first attempt used `BackgroundTasks` on the belief that dependency teardown precedes background tasks — on FastAPI 0.141 it does not, and the e2e ordering test recorded `['enqueue','commit']` on its first run, reproducing the race immediately. Four more real bugs found by tests, not review: `utf-8` before `utf-8-sig` (BOM survived as U+FEFF), cp1252 accepting binary as text, `mkdir` outside the `try` in the atomic write (raw `OSError` escaping instead of `StorageError`), and the Postgres enums outliving their tables in `downgrade()` again. Also corrected an over-claim inherited from M2: UUIDv7 order is *not* chronological within a single millisecond. 236 tests, 98.86%. Wrote [ADR-0007](docs/adr/0007-object-storage-behind-a-protocol.md), [ADR-0008](docs/adr/0008-work-is-enqueued-only-after-the-transaction-commits.md), [docs/milestones/M5-document-upload.md](docs/milestones/M5-document-upload.md). Verified at runtime with a real uvicorn + arq worker + curl, not only in tests. |
| 2026-07-10 | Full scaffold created: folder tree, configs (pyproject, compose, Makefile, CI, pre-commit, Dockerfile, .env.example), all design docs. Quiz issued, unanswered. Repo not yet under git. |
| 2026-07-12 | User requested named stubs: all ~110 backend modules created with docstrings (purpose/layer/rules), real imports, and milestone-tagged TODOs (M1–M16). No implementations — bodies are TODO comments; each stub carries `# ruff: noqa: F401`, removed when implemented. Quiz still unanswered; repo still not under git. |
| 2026-08-12 | **M4 implemented and shipped**: transactional test isolation (one rolled-back transaction per test; `get_db` overridden so HTTP requests join it; `join_transaction_mode="create_savepoint"` so app-level `commit()` still behaves as in production), session-scoped schema creation, `tests/factories.py` (Argon2 hashed once per session — per-user hashing would add a minute of CPU), directory-derived `unit`/`integration`/`e2e` markers, coverage gate at 97%. **Found and fixed an 11-point coverage under-report**: SQLAlchemy's asyncio layer runs in greenlets, which coverage does not trace without `concurrency = ["greenlet", "thread"]` — the service layer read 57% while e2e tests were exercising it. The omit list for stub modules is reconciled against the source tree in both directions by `tests/unit/test_stub_manifest.py`, so it cannot rot. 156 tests, 98.7%. Wrote [ADR-0006](docs/adr/0006-tests-run-inside-a-rolled-back-transaction.md) and [docs/milestones/M4-testing-discipline.md](docs/milestones/M4-testing-discipline.md). |
| 2026-08-12 | **M3 implemented and shipped**: Argon2id + JWT primitives, stateless 30-min access tokens vs rotating revocable 7-day refresh tokens (Redis `jti` denylist with self-expiring entries), `AuthService` (register creates user + personal org + owner membership atomically), `CurrentUser`/`CurrentMembership`/`require_role` dependencies, `OrganizationService` with every role rule (admin cannot grant or touch ownership; last owner cannot be demoted or leave), central domain-error→HTTP mapping, 12 endpoints. **Redis moved M5→M3** because logout must actually revoke; readiness gained a Redis probe by the same "probe what you use" rule. Added `pydantic[email]`. 115 tests. Wrote [ADR-0004](docs/adr/0004-stateless-access-tokens-with-rotating-refresh-tokens.md), [ADR-0005](docs/adr/0005-organization-scope-travels-in-a-header.md), [docs/milestones/M3-authentication.md](docs/milestones/M3-authentication.md). |
| 2026-08-12 | **M2 implemented and shipped**: `uuid7()` (RFC 9562 §5.7, hand-rolled — 3.13 has no `uuid.uuid7`), `Base` + naming convention + UUID/timestamp mixins, `organizations`/`users`/`memberships` + `Role` enum, async engine + session-per-request with the commit boundary in `get_db()`, `/health/ready` (Postgres only — Redis waits for M5), Alembic wired async to `Settings` and `Base.metadata` with ruff post-write hooks. 48 tests (30 unit, 9 integration, 9 e2e). Caught 3 real bugs: the Postgres enum that survives `DROP TABLE`, a wrong claim that ids exist before flush, and an index redundant with a composite unique constraint. Wrote [ADR-0003](docs/adr/0003-uuidv7-primary-keys.md) and [docs/milestones/M2-database-layer.md](docs/milestones/M2-database-layer.md). |
| 2026-08-11 | Mode switched to HYBRID (see above). `git init` + baseline commit. Installed `uv`, resolved deps on Python 3.13.2. **M1 implemented and shipped**: config, exceptions, structlog + request-id processor, request-id and timing middleware, `/api/v1/health/live`, v1 router, `create_app()` factory. 12 tests. Full `make check` green. Wrote [ADR-0002](docs/adr/0002-unimplemented-stubs-are-excluded-from-mypy.md) (stub modules get `# mypy: ignore-errors` so the gate can pass before M16) and [docs/milestones/M1-skeleton-that-runs.md](docs/milestones/M1-skeleton-that-runs.md). Also installed 24 `addyosmani/agent-skills` globally and disabled 7 unused marketplace plugin families. |

## Quiz & exercise history

| Milestone | Quiz result | Exercise result | Notes |
|---|---|---|---|
| Architecture | pending | pending | re-ask at next session start |
