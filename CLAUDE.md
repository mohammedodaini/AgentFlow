# CLAUDE.md — AgentFlow AI session memory

This file is Claude's persistent memory for this project. **Claude: read this
fully at session start; update the "Current position" and "Progress log"
sections whenever a milestone step completes.** The human should not have to
re-explain context, ever.

## The mentorship contract (SUSPENDED — see "Current position")

> **2026-08-16: the user suspended this.** *"there is no mentor mode anymore,
> mentor mode will be after we get this project working."* The contract below
> is the agreement to return to *once the product works*; until then M13–M16
> are built autonomously. Do not follow it now, and do not delete it.

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

- **Phase:** **M1–M12 complete (2026-08-16) — the autonomous run is finished.**
  `make check` green — ruff, ruff format, mypy strict over **232 files**,
  **713 tests**, **97.14% coverage** (gate at 97%). See
  [docs/milestones/M12-human-in-the-loop.md](docs/milestones/M12-human-in-the-loop.md).
- **Pushed.** The repository is now at
  https://github.com/mohammedodaini/AgentFlow (private, `master`). Before
  2026-08-16 there was no remote at all and seven milestones existed only on
  this laptop.
- **`make eval` is now a gate.** It exits non-zero on a regression against
  `backend/app/evaluation/baselines/handbook.json`. Per `docs/agents.md`, no
  prompt or retrieval change ships without it passing. `make eval-baseline`
  accepts new scores — only after a human has read the report.
- **Mode (2026-08-16, supersedes everything above): MENTOR MODE IS SUSPENDED
  UNTIL THE PRODUCT WORKS.** The user's words, verbatim: *"there is no mentor
  mode anymore, mentor mode will be after we get this project working"*. That
  was said immediately after M12 shipped and after I had written the opposite
  into this file, so it is a deliberate correction rather than a passing remark.
- **What that licenses:** build M13 → M16 autonomously, to the same standard as
  M6–M12 — full `make check`, an ADR per contested decision, a milestone note
  with a "Bugs this milestone found" section, runtime verification, one commit
  per milestone. Do not ask permission per milestone.
- **What it does not license:** skipping the gate, or declaring something done
  that has not been run. "Working" is the bar the user named, and a green test
  suite is not the same as a product somebody can use.
- **The deferred quiz and exercise wait for mentor mode.** They are listed under
  "Still pending" below. Do not raise them now — the user has explicitly parked
  that mode; raising them would be relitigating a decision they just made.
- **No API keys exist in this environment** (`OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY` both unset). Every milestone from M6 ships with a
  deterministic offline provider behind a protocol, and `Settings` refuses each
  one in production. Keep doing that, and keep saying plainly in each milestone
  note what is verified (plumbing) and what is not (model quality).

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
- **Dependency specifiers are unpinned upper-bound-free (`mypy>=1.13` etc.), so
  any `uv lock` can bring a new major version of a *tool*.** M6 got mypy 2.3
  this way. Re-run `make check` after adding any dependency, and read new
  errors as "the tool got stricter", not "I broke something".
- **From M5 the app needs two processes.** `make dev` alone leaves every upload
  stuck at `status=pending`; `make worker`
  (`arq app.workers.settings.WorkerSettings`) is what moves them. The Makefile
  target used to point at `app.workers.main`, a module that never existed.
- Uploaded bytes go to `backend/var/storage/` (gitignored). Tests get a
  `tmp_path` instead, via `STORAGE_LOCAL_PATH` in the autouse env fixture.
- GateGuard's edit/write gate fires on the **first attempt** at each new file
  and allows the **retry**, so a multi-file milestone costs roughly two tool
  calls per file. It denied 38 times across M5.

### Still pending — PARKED until mentor mode returns (do not raise now)
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
| 2026-08-16 | **M12 implemented and shipped** (autonomous — the last of the M6–M12 run): `approvals`, the **calendar agent** (`app/agents/calendar/`, the second implemented agent), pause/resume writing `agent_runs.checkpoint` and `PAUSED_FOR_APPROVAL` for the first time since M9 added them, real `cost_usd`, the Google Calendar **write** scope, and five endpoints. **The three ideas (ADR-0015): an approval is a row, not a pause** — the gap between asking and clicking is hours, so a deploy in that window is the expected case; **the action is stored whole and is what executes** — not a plan id re-derived later, so "what was approved" and "what ran" are identical by construction; **and proposing and executing are different functions**, with the executor built only on the resume path, so there is no `if approved:` branch to get wrong. Stated plainly: this is **not** LangGraph's `interrupt()` — two graphs over shared nodes, with `checkpoint` as the durable store, because a checkpointer would be a second copy of a fact the row already holds. Four bugs: the enum outliving its table in `downgrade()` for the **sixth** milestone running; **`add_steps` assumed one batch of steps per run** and collided on `uq_agent_steps_agent_run_id` the moment a run resumed; **a rollback silently undid a human's decision** — `_finish_failed` rolls back, taking the flushed `APPROVED` with it and leaving the run FAILED beside a PENDING approval nobody could ever action (fixed by *committing* the decision before executing, M9's run-row lesson from the other direction); and **`None` assigned to a JSONB column stores JSON `null`, not SQL NULL** — found by querying Postgres, invisible through the ORM, and it meant `WHERE checkpoint IS NULL` was false for every cancelled run. 713 tests, 97.14%; `make eval` unchanged. Wrote [ADR-0015](docs/adr/0015-an-approval-is-a-row-and-the-action-it-permits-is-stored-whole.md) and [docs/milestones/M12-human-in-the-loop.md](docs/milestones/M12-human-in-the-loop.md). **Verified across a real restart**: killed the server, found the approval still pending in a brand-new process, resumed it from the checkpoint in Postgres. **The email half is deliberately not built** — there is no Gmail integration to draft into, and that is M14's work. Also: the repository got its first remote and was pushed. |
| 2026-08-16 | **M11 implemented and shipped** (autonomous): `integrations` + `oauth_tokens`, Fernet encryption at rest, an `OAuthProvider` seam with a real Google implementation and an **in-memory authorization server** for offline work, tenant-scoped repository and service, and five endpoints — one of them deliberately unauthenticated. **The two decisions (ADR-0014): the callback's only credential is `state`** — Google redirects the *browser*, so no header we issued survives, which means `state` must be unguessable, carry the tenant binding, expire, and work exactly once (`GETDEL`); without the first two, an attacker's crafted callback URL connects *their* Google account to a victim's organization. **And a token is a liability from the moment it is written** — Fernet ciphertext in a *separate table*, because that is what makes least privilege expressible, with encryption explicit at the call site rather than hidden in a `TypeDecorator`. `TOKEN_ENCRYPTION_KEY` is separate from `SECRET_KEY` and production refuses both the published placeholder and the two being equal. Four bugs: the enum outliving its table in `downgrade()` for the **fifth** milestone running; **`MissingGreenlet` for the sixth time** — the repository's documented eager-load contract had a hole on the *create* path, where a freshly flushed object's first `.tokens` access is a lazy SELECT (a documented contract with one path that ignores it is not a contract); **a provider outage answering 500, found only by curl** because every test asserted on the exception rather than the status code — `OAuthError` now maps to 502; and `OAuthRevokedError` inheriting that 502 and inviting a retry that can never succeed, so the service converts it to a 404 saying "reconnect it". Added `cryptography` explicitly rather than relying on authlib's transitive copy. 667 tests, 97.77%; `make eval` unchanged, as it should be — M11 touches no prompt. Wrote [ADR-0014](docs/adr/0014-the-state-parameter-is-the-callbacks-only-credential.md) and [docs/milestones/M11-first-oauth-integration.md](docs/milestones/M11-first-oauth-integration.md). Verified at runtime: a callback with **no auth header at all** succeeding, a replay and a forged state both 401, and `gAAAAABq…` ciphertext in `oauth_tokens`. |
| 2026-08-15 | **M10 implemented and shipped** (autonomous): `conversations` + `messages` + `memories`, the `agent_runs.conversation_id` M9 deferred, a recency-bounded prompt window, decay/reinforcement policies, blended recall, extraction as an arq task, a `prepare` node on the graph, and five endpoints. **The two decisions (ADR-0013): a bounded window is only survivable because something else remembers** — `history.py` is the deliberate mirror of `context.py`, keeping the *newest* turns where retrieval keeps the top-ranked, and long-term memory is what compensates for what falls off the back; **and the model never chooses a memory's scope** — everything extracted is `scope=user`, ADR-0012's rule at a second boundary, because a privacy boundary is not a field for a model to fill in. Six bugs. **The largest was inherited: M9's `run_status` was the only enum in the schema storing uppercase member *names*** (it omitted `values_callable`), so a hand-written `WHERE status = 'running'` silently returned zero rows and contradicted `docs/database.md`; fixed with `ALTER TYPE … RENAME VALUE`. Also: **`create_all` never migrates**, so `agentflow_test` had drifted and the new column was missing — the fixture now does `DROP SCHEMA public CASCADE` (not `drop_all`, which leaves enum *types* behind — the same fact for the fourth migration running), guarded to refuse any database not ending in `_test`. Plus doubled constraint names from autogenerate, a check constraint that compared `'user'` against a column storing `'USER'`, and **a control test that passed for the wrong reason** — the corpus was one chunk, so retrieval returned it for any query and the offline model quoted the same sentence either way; the assertion moved to *what query reached the index*, read from the trace. 596 tests, 98.20%; `make eval` unchanged against M8's baseline, which is exactly what it is for. Wrote [ADR-0013](docs/adr/0013-context-is-a-bounded-window-and-the-model-never-chooses-a-memorys-scope.md) and [docs/milestones/M10-conversations-and-memory.md](docs/milestones/M10-conversations-and-memory.md). Verified at runtime: a follow-up with no subject ("How much is that?") answered via `context_terms` in a real trace, memory extracted by the worker after the response, and recalled in a *different* conversation. |
| 2026-08-14 | **M9 implemented and shipped** (autonomous): `agent_runs` + `agent_steps` (the run row is *both* the observability and the billing unit), a real LangGraph graph with a bounded cycle (`retrieve → rewrite → retrieve → generate`, `MAX_ATTEMPTS=2`), `search_chunks` as a real `BaseTool`, a tenant-scoped repository, `AgentService` owning the run/trace/transaction, and three endpoints. **The security decision (ADR-0012): the organization is closed over when the tool is built and never appears in the schema the model sees** — tool arguments are model-chosen, so a prompt-injected "search organization 7f3a…" must be unrepresentable rather than merely checked. Five bugs: naive `TIMESTAMP` from autogenerate (the mixin's columns were `timezone=True` in the *same* CREATE TABLE); the enum outliving its table in `downgrade()` for the **third** time (M2, M5, M9); **`MissingGreenlet` inside the error handler** — `rollback()` expires ORM objects regardless of `expire_on_commit`, so touching `run.id` there masked the real failure and left the run `running` forever; the same error during serialisation because `steps` was never eagerly loaded; and **the agent was less safe than `/ask`** — its tool omitted `MIN_EVIDENCE_SCORE`, so it would have answered unanswerable questions from zero-similarity chunks (a shared seam does not make behaviour shared; only calling it the same way does). **Honest limit recorded: this is not a tool-calling ReAct loop** — the edges decide, not the model, because `LLMProvider` is text-in/text-out and there is no key. 491 tests, 98.33%; `make eval` still green. Wrote [ADR-0012](docs/adr/0012-every-agent-run-is-traced-and-the-tenant-is-never-a-tool-argument.md) and [docs/milestones/M9-first-agent.md](docs/milestones/M9-first-agent.md). |
| 2026-08-13 | **M8 implemented and shipped** (autonomous): deterministic retrieval metrics, a hand-written golden set (4 documents, 15 questions, **4 deliberately unanswerable**), an LLM-as-judge with a heuristic offline counterpart, and a runner that ingests the dataset's own corpus, asks every question through the real `Generator`, and compares against a **committed baseline**. `make eval` exits non-zero on a regression — the exit code is the product (ADR-0011). **The finding contradicted M6's assumption**: a similarity threshold cannot implement refusal. The score distributions overlap outright (lowest answerable 0.069, highest unanswerable 0.262), so *no* non-zero `min_score` beats zero — every threshold catching a refusal discards more real answers than it saves. Refusal is a judgement about meaning, and cosine similarity does not encode meaning. Recorded in `retrieval.py`'s docstring with the sweep table. First baseline: recall 1.000, mrr 0.864, **refusal_accuracy 0.000** (the extractive offline provider cannot refuse — a labelled gap, not hidden). 456 tests, 98.35%. Wrote [ADR-0011](docs/adr/0011-evaluation-is-a-committed-baseline-not-a-dashboard.md) and [docs/milestones/M8-evaluation.md](docs/milestones/M8-evaluation.md). Verified the gate really fails by planting an unbeatable baseline. |
| 2026-08-13 | **M7 implemented and shipped** (autonomous): a new top-level `app/llm/` package (`LLMProvider` protocol + `AnthropicLLM` + an extractive `OfflineLLM`), the prompt loader with templates as *files*, token-budgeted context assembly that builds the citation map in the same loop, and `POST /ask` plus `POST /ask/stream` (SSE). **The load-bearing decision: no context, no call** — with nothing usable retrieved, `Generator` refuses locally and never contacts the model, because a model handed an empty context invents a fluent, confident, uncited answer and returns 200. The test asserts this with a provider that raises if invoked. Three bugs: the offline model answered by *repeating the question* (the block regex swallowed the trailing `Question:` line, which then perfectly matched itself); quoted answers began with the filename (a title has no sentence-ending punctuation); and — found at runtime with curl, invisible to every test — **a refusal came back with three citations attached**, because a vector search always returns `top_k` neighbours however far away, so "nothing relevant" is not a state pgvector can report. Fixed with `MIN_EVIDENCE_SCORE` (zero-similarity chunks are not evidence; *not* the tuned floor M8 owns). 395 tests, 98.25%. Wrote [ADR-0010](docs/adr/0010-answers-are-grounded-refusals-are-local.md) and [docs/milestones/M7-generation.md](docs/milestones/M7-generation.md). Also added `docs/CONTINUE.md`, `scripts/continue-agentflow.sh` and a launchd plist for unattended runs (commit `9125b43`) — **not installed; the user must `launchctl load` it.** |
| 2026-08-13 | **M6 implemented and shipped** (autonomous, per the M6–M12 instruction): `document_chunks` with a `vector(1536)` column and an HNSW index, paragraph-aware token-bounded chunking, an `EmbeddingProvider` seam with a *real* offline implementation, org-scoped cosine search, and `POST /search` returning chunks with citations. **The load-bearing pgvector fact: HNSW filters *after* the vector scan**, so a tenancy filter can silently return fewer than `top_k` rows — `SET LOCAL hnsw.iterative_scan = 'strict_order'` fixes it. Six bugs found by tests, not review: autogenerate emitted the migration with no `CREATE EXTENSION` and **no HNSW index**; `alembic check` then wanted to *drop* that index because it was absent from `Base.metadata`; the `"\n\n"` separator was uncounted against the chunk budget; **token counts are not additive under concatenation** (BPE merges across a join, so a `chunk_size=8` test produced a 9-token chunk); the offline embedder crashed on two words that hash to one coordinate with opposite signs (`math.log(0)`), found by the new e2e test after every shorter text had missed it; and `EmbeddingProvider` lacked `@runtime_checkable`. Separately, **`uv lock` silently upgraded mypy 1.x → 2.3.0** (pyproject asks only for `>=1.13`), surfacing six pre-existing type errors in `tests/unit/test_models.py` — fixed rather than pinned back. 316 tests, 98.01%. Wrote [ADR-0009](docs/adr/0009-embeddings-behind-a-protocol-with-a-real-offline-implementation.md), [docs/milestones/M6-rag-pipeline.md](docs/milestones/M6-rag-pipeline.md), and `backend/tests/worker_harness.py`. Verified with a real uvicorn + arq worker + curl, including ranking *between* chunks and a rehearsed migration rollback. |
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
