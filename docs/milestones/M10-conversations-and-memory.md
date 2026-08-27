# M10: Conversations and memory: a bounded window, and something that outlives it

- **Date:** 2026-08-15
- **Status:** shipped
- **ADRs:** [ADR-0013](../adr/0013-context-is-a-bounded-window-and-the-model-never-chooses-a-memorys-scope.md)

M9's agent answers one question. M10 makes it hold a conversation, and then makes
it remember something after the conversation ends.

## What was built

| Piece | File | What it does |
|---|---|---|
| `conversations` | `app/models/conversation.py` | A chat thread, owned by a user in an org |
| `messages` | `app/models/message.py` | Append-only turns; links a reply to its run |
| `memories` | `app/models/memory.py` | What the *agent* learned, vector-searchable, decayable |
| Migration | `alembic/versions/…04c00ca75710_…` | Three tables, `agent_runs.conversation_id`, an enum rename |
| Window | `app/agents/history.py` | Recency-bounded prompt history |
| Policies | `app/memory/policies.py` | Decay, reinforcement, the forgetting sweep |
| Recall | `app/memory/recall.py` | Similarity blended with importance and recency |
| Writer | `app/memory/writer.py` | Extraction, normalisation, double dedup |
| Repositories | `app/repositories/{conversation,memory}_repository.py` | Tenant- and scope-scoped access |
| Graph | `app/agents/rag/graph.py` | A new `prepare` node: recall + contextualise |
| Service | `app/services/conversation_service.py` | Turn ordering, titles, the extraction task row |
| Task | `app/workers/tasks/memory_extraction.py` | Learning, after the response is sent |
| API | `routes/{conversations,memories}.py` | Four conversation endpoints, one read-only memory endpoint |

## The shape now

```
prepare ──► retrieve ──► enough? ──yes──► generate ──► END
               ▲            │
               └──rewrite◄──no (and attempts remain)
```

`prepare` is the milestone in one node. It does the two things that must happen
*before* anything is searched: recall what is already known about the person, and
make a follow-up question stand on its own. Retrieval sees one query, not a
thread, so "how much is that?" is three words with no subject, and the best
retrieval system in the world cannot help.

## The two ideas

**A bounded window is only survivable because something else remembers.**
`history.py` is the deliberate mirror of `rag/context.py`: both fit text into a
token budget, and they disagree about which end to keep. Retrieval keeps the
top-ranked and drops the tail; a conversation keeps the *newest* and drops the
head, because turn 4 is what makes turn 5 mean anything. What falls off the back
is gone, and long-term memory is the mechanism that compensates, which is why
these two shipped together rather than a milestone apart.

**The model chooses the facts. It never chooses the scope.** Everything extracted
is `scope=user`. That is ADR-0012's rule at a second boundary: there, the tenant
is closed over so a prompt-injected document cannot reach another organization's
files; here, the scope is fixed in code so no phrasing can promote a fact out of
one person's private thread into everyone's answers. A privacy boundary is not a
field for a model to fill in.

## Bugs this milestone found

Six, and one of them was in a test I had just written.

**1. M9 stored its enum in the wrong case, and nothing had noticed.** `run_status`
was the *only* enum in the schema holding uppercase member names, `RUNNING`
`SUCCEEDED`: while `membership_role`, `document_status`, `document_source` and
`task_status` all store the lowercase value, each with a comment explaining why.
M9 omitted `values_callable`. Nothing broke, because SQLAlchemy writes and reads
by the same rule either way. The cost is invisible until somebody types SQL by
hand: `WHERE status = 'running'` returns zero rows, raises nothing, and
`docs/database.md` documents the lowercase form. Found while writing the two new
enums and deciding which convention they should follow. Fixed with `ALTER TYPE …
RENAME VALUE`, which relabels in place without moving a row.

**2. The test database silently drifts, and `create_all` cannot fix it.**
`Base.metadata.create_all` is `CREATE TABLE IF NOT EXISTS`: it builds absent
tables and never alters an existing one. So `agent_runs.conversation_id` was never
added to `agentflow_test`, and the failure arrived as an `UndefinedColumnError`
from inside an unrelated M9 test. Fixed by dropping and recreating the schema per
session, with `DROP SCHEMA public CASCADE` rather than `metadata.drop_all()`
because `drop_all` drops tables and **not** the enum types they use, which is the
same fact that has needed a hand-written line in four migrations now. Guarded so
it refuses any database whose name does not end in `_test`.

**3. The enum outlived its table in `downgrade()`.** Fourth milestone in a row
(M2, M5, M9, M10). Autogenerate has still never written that line.

**4. Autogenerate produced doubled constraint names.**
`ck_memories_ck_memories_importance`: because `NAMING_CONVENTION` prepends
`ck_%(table_name)s_` and the model had spelled the prefix out as well.

**5. A check constraint that would have rejected every row it guarded.** The
`memories` check compares `scope = 'user'`, and without `values_callable` the
column stores `'USER'`. Both halves were written in the same file, an hour apart,
and each was correct on its own. Same shape as bug 1, and worth naming: *a
constraint and the column it constrains have to agree about representation, and
nothing checks that they do.* Caught because fixing bug 1 made me re-read it.

**6. A test that passed for the wrong reason, then failed for a worse one.** The
control for "a follow-up is answered in context" was "the same question alone is
*not* answered". It failed, because this corpus is small enough to be a single
chunk, so retrieval returns it for any query at all and the offline model quotes
its most similar sentence. The answer contained "45p" either way. Had the corpus
been one document larger the control would have *passed*, and the test above it
would have looked like proof of something it never touched. The assertion moved to
the mechanism: what query actually reached the index, read out of the trace.

## Verified at runtime

Real uvicorn, real arq worker, curl.

- **A follow-up with no subject, answered.** Turn 2 was `"How much is that?"`, and
  the trace shows what made it work:
  ```
  0 prepare   {"history_turns": 2}  → context_terms: "work berlin office approve
                                       invoices team mileage reimbursement policy"
  1 retrieve  search_chunks         → query: "…mileage reimbursement policy How much
                                       is that?"   count: 1   top_score: 0.127
  2 generate  {"conversational": true, "history_tokens": 53, "history_dropped": 0}
  ```
  Answer: *"Mileage is reimbursed at 45p per mile for the first 10,000 miles. [1]"*
- **Memory extracted asynchronously**, after the response was sent: one user-scoped
  memory, `importance 0.5`, from a sentence the person actually typed.
- **Recalled in a *different* conversation**, `prepare` on a fresh thread returned
  `["I work in the Berlin office and I approve invoices for my team"]`. That
  crossing of a thread boundary is the whole point of memory being a table rather
  than a longer window.
- `checkpoint` still absent from every response.

The trace above also shows the honest weakness: `context_terms` borrowed *every*
content word from the previous turn, including "work", "office" and "team". It
worked, and it dilutes the query. See ADR-0013.

## Gate

```
ruff · ruff format · mypy --strict (221 files) · alembic check · make eval
596 tests, 2 skipped · 98.20% coverage (gate 97%)
```

**`make eval` still passes**, with scores identical to M8's committed baseline
recall 1.000, mrr 0.864, refusal_accuracy 0.000. That is exactly what the gate is
for: M10 changed the graph, added a node, and introduced a second prompt pair, and
the baseline is what confirms `/ask` did not move with it.

## Known gaps, deliberately left

**Extraction quality is untested, and it is the biggest gap here.** The offline
provider returns first-person declarative sentences the user actually typed
never inventing, and never *judging*. Rule 1 of the extraction prompt (*is this
durable?*) is a judgement about meaning, and "I am in a meeting until three"
passes every test the offline path can apply. So the pipeline is verified and the
judgement is not. Labelled, not hidden: the same treatment M8 gave
`refusal_accuracy: 0.000`.

**Nothing writes an org-scoped memory.** The scope exists, recall honours it, tests
cover it, and promotion is a deliberate human act with no interface yet.

**No sweep runs.** `plan_maintenance` and `forget` are implemented and tested;
nothing schedules them. A cron that deletes user data unattended deserves its own
decision, and the store is empty enough that the question is not yet real.

**The decay constants are not measured.** `HALF_LIFE_DAYS = 30`,
`REINFORCEMENT = 0.15`, `FORGET_THRESHOLD = 0.05` and `NEAR_DUPLICATE_SCORE = 0.92`
are defensible starting points in exactly the sense `chunk_size_tokens` was before
M8 replaced it with numbers. Measuring them needs conversations long enough to
have something worth forgetting.

**No streaming, and no summarisation.** Both argued in ADR-0013.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099         # terminal 1
uv run arq app.workers.settings.WorkerSettings  # terminal 2
```

```bash
CONV=$(curl -s -X POST localhost:8099/api/v1/conversations \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .id)

curl -X POST localhost:8099/api/v1/conversations/$CONV/messages \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' \
  -d '{"content": "What is the mileage reimbursement policy?"}'

curl -X POST localhost:8099/api/v1/conversations/$CONV/messages \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' -d '{"content": "How much is that?"}'
```

Then `GET /api/v1/memories`: the worker will have learned something from the
first turn by the time the second finishes.
