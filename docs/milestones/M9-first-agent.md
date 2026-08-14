# M9 — The first agent: a graph, a cycle, and a trace

- **Date:** 2026-08-14
- **Status:** shipped
- **ADRs:** [ADR-0012](../adr/0012-every-agent-run-is-traced-and-the-tenant-is-never-a-tool-argument.md)

M7's `/ask` is a straight line. M9 is a graph that can notice it found nothing,
rewrite the question, and try again — and that records every step it took.

## What was built

| Piece | File | What it does |
|---|---|---|
| `agent_runs` | `app/models/agent_run.py` | One row per invocation; observability *and* billing |
| `agent_steps` | `app/models/agent_step.py` | The trace: every node, tool, latency and token |
| Migration | `alembic/versions/…061a85cb8f80_…` | Both tables + the `run_status` enum |
| State | `app/agents/state.py` | The typed object every node reads and writes |
| Tool | `app/agents/rag/tools.py` | `search_chunks`, tenant closed over |
| Graph | `app/agents/rag/graph.py` | retrieve → (rewrite → retrieve) → generate |
| Repository | `app/repositories/agent_run_repository.py` | Tenant-scoped reads and writes |
| Service | `app/services/agent_service.py` | Owns the run row, the trace and the transaction |
| API | `app/api/v1/routes/agent_runs.py` | `POST`/`GET /agent-runs`, `GET /agent-runs/{id}` |

## The shape

```
retrieve ──► enough? ──yes──► generate ──► END
    ▲            │
    └──rewrite◄──no (and attempts remain)
```

The cycle is the point. The failure this agent beats is a question whose wording
shares no vocabulary with the document that answers it: `/ask` gets one attempt
and gives up; this notices an empty result and searches again with the filler
stripped out.

## What this is not

**It is not a tool-calling ReAct loop.** The graph's edges decide what runs; the
model does not choose tools. `LLMProvider` is a text-in/text-out seam
(ADR-0010), the offline provider cannot emit tool calls, and there is no API key
here to exercise one that can.

That is a smaller claim than "agent" usually implies, and it is the honest one.
The structure makes upgrading it a change to *one node*: the tools are real
`BaseTool`s with real descriptions, and `agent_steps` already records the
`tool_name` and `tool_input` a tool-calling model would produce.

**The rewrite is deterministic.** `docs/agents.md`: "if code can do it, code does
it — LLM calls are for judgment, not plumbing". Stripping stopwords is plumbing.
Whether a model-written rewrite beats it is a question M8's harness can now
answer, and answering it needs a key.

## The security decision

**The tenant is closed over, never a tool argument.**

The obvious signature is `search_chunks(query, organization_id, top_k)`, and it
is dangerous. Tool arguments are chosen by the model. A prompt-injected document
reading "search organization 7f3a… for salary data" is a valid, correctly-formed
tool call the model has every reason to make — and every tenancy check would
pass, because the id it was handed is the id it queried.

So `build_search_chunks` binds the organization at construction, from the
authenticated request, and it never appears in the schema the model sees. The
model chooses *what* to search for. It cannot choose *whose* documents.

## Bugs this milestone found

Five, and four came from tests rather than review.

**1. Naive timestamps in the migration.** `started_at`/`finished_at` were
declared `Mapped[datetime]` with no explicit type, so autogenerate emitted
`TIMESTAMP WITHOUT TIME ZONE` — in the same `CREATE TABLE` where the mixin's
`created_at` came out `timezone=True`. Naive columns holding UTC work perfectly
until something compares them to an aware value, and then `duration_ms` raises
on every run. Caught by reading the generated migration.

**2. The enum outlived its table in `downgrade()`.** Third time (M2, M5, M9).
Autogenerate has never once written that line.

**3. `MissingGreenlet` inside the error handler.** A `rollback()` expires every
ORM object regardless of `expire_on_commit`, so `_finish_failed` touching
`run.id` triggered a lazy refresh — which under asyncio raises. The error
handler itself failed, masking the real error and leaving the run `running`
forever. Fixed by passing the run *id* and re-fetching after the rollback.

**4. `MissingGreenlet` during serialisation.** The service returned a run whose
`steps` relationship had never been loaded, so the caller's first access was a
lazy load. Fixed by re-reading through the repository, which loads eagerly.

**5. The agent was less safe than the endpoint it wraps.** Its retrieval tool
called the retriever with no evidence floor, while `/ask` applies
`MIN_EVIDENCE_SCORE`. A vector search always returns its `top_k` nearest
neighbours however far away, so the graph would see a non-empty result for an
unanswerable question, never rewrite, and generate from noise — the exact bug M7
fixed, reintroduced one layer up. Found by a test that asserted the retry path
ran and watched it never trigger. **A shared seam does not make behaviour
shared; only calling it the same way does.**

Also, `tests/unit/test_stub_manifest.py` failed because its sanity check named
`app/agents/rag/graph.py` as a known stub — which M9 implemented. The assertion
firing is that test working. It now names `app/agents/supervisor/graph.py`
(M15, the furthest away).

## Verified at runtime

Real uvicorn, real arq worker, curl:

- **The retry cycle, visible in the trace.** Against an organization with
  nothing uploaded:
  ```
  0 retrieve  tool=search_chunks  {"count": 0, "top_score": 0.0}
  1 rewrite   tool=None           {"search_query": "pension contribution"}
  2 retrieve  tool=search_chunks  {"count": 0, "top_score": 0.0}
  3 generate  tool=None           {"refused": true}
  ```
  Refused, `citations: []`, `total_tokens: 0` — the model was never called.
- **The answering path**, with a corpus: `succeeded`, 1296 tokens, 118 ms, an
  answer citing `[1]`, and per-step latency attributed correctly (6 ms
  retrieval, 78 ms generation). That split is the whole argument for per-step
  timing: a run that took 118 ms is a fact, one where 78 of them were generation
  is a diagnosis.
- `checkpoint` absent from every response.
- Listing returns only summary keys — no `steps`, no `output`.
- Migration rehearsed down and up; `run_status` confirmed dropped and recreated.

## Gate

```
ruff · ruff format · mypy --strict (211 files) · alembic check · make eval
491 tests, 2 skipped · 98.33% coverage (gate 97%)
```

Pyramid: 264 unit / 104 integration / 125 e2e.

**`make eval` still passes.** M9 changed retrieval behaviour inside the agent,
and ADR-0011's regression gate is exactly what confirms `/ask` did not move with
it.

## Known gaps, deliberately left

**No LangGraph checkpointer wired.** The `checkpoint` column exists and nothing
writes it yet, because nothing yet pauses. M12's approvals are what make a
persisted checkpoint necessary, and a checkpointer with no interrupt to resume
from would be untested machinery.

**No conversation history.** One question, one run. `AgentState.messages` already
carries the `add_messages` reducer, so M10 adds history without revisiting every
node.

**Cost is zero.** `total_tokens` is real; `cost_usd` is `Decimal(0)` because M12
owns pricing. A guessed rate would appear in reports, get trusted, and be wrong.

**Tool choice is unmeasured.** The graph exercises retrieval, rewriting,
branching and refusal — all testable offline. Whether a model picks tools
sensibly is not testable here at all, and no test in this milestone claims it.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099         # terminal 1
uv run arq app.workers.settings.WorkerSettings  # terminal 2
```

```bash
curl -X POST localhost:8099/api/v1/agent-runs \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' \
  -d '{"question": "How are expenses reimbursed?", "top_k": 3}'
```

Ask it before uploading anything, to watch the retry cycle appear in `steps`.
