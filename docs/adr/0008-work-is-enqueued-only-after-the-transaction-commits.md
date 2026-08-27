# ADR-0008: Background work is enqueued only after the transaction commits

- **Status:** accepted
- **Date:** 2026-08-12
- **Milestone:** M5

## Context

`POST /documents` writes a `documents` row, writes a `tasks` row, and asks a
worker to do the slow part. Returning 202 immediately is the whole point
(`docs/architecture.md`), and it introduces a race that does not exist anywhere
else in the codebase so far.

The API commits at the request edge: `get_db()` owns the transaction and
commits after the route returns (M2, ADR-0006). Every route until now could
ignore that entirely. This one cannot, because it hands a reference to
uncommitted data to *another process*.

Enqueue inside the transaction and the sequence can be:

1. API inserts the document row: not yet committed.
2. API pushes the job to Redis. Redis knows nothing about our transaction.
3. A worker: idle, milliseconds away, dequeues it and queries for the row.
4. Nothing. The row is invisible outside the API's open transaction.
5. API commits.

The document is now `pending` forever, and the only evidence is a worker log
line about a row that plainly exists by the time anyone looks. It reproduces
under load and never on a developer's laptop.

## Decision

**Nothing is enqueued until the data it refers to is committed.**

`DocumentService.upload()` deliberately does not enqueue. It returns the
document and its task row, and the route commits explicitly and then enqueues:

```python
document, task = await service.upload(...)
await session.commit()                 # durable first
await enqueue_ingestion(queue, ...)    # advertised second
```

`DELETE /documents/{id}` does the mirror image, for the mirror reason: the row
is deleted and committed before the bytes are removed. Deleting the object
first and then failing to commit leaves a surviving row pointing at bytes that
are gone: a broken invariant nothing detects until a user opens the document.
The other order risks an orphaned file, which costs money rather than
correctness.

These are the only two routes in the codebase that commit for themselves. The
commit boundary still belongs in `get_db()`; these endpoints have an ordering
requirement it cannot express, and say so in a comment at the point of
exception.

## How the first attempt failed

The first implementation used FastAPI's `BackgroundTasks`, on the documented
belief that a dependency with `yield` runs its exit code *before* the response
is sent, and background tasks *after*, which would have given the right order
for free, with no commit in the route.

On FastAPI 0.141 / Starlette 1.6 it is the other way round: the dependency exit
stack closes after the response and its background tasks have run. The enqueue
happened first, and the race was fully present.

It was caught by
`tests/e2e/test_documents_api.py::test_the_job_is_enqueued_after_the_commit`,
which records the real order of `commit` and `enqueue` and asserts on it. That
test exists precisely because the guarantee belongs to the framework rather
than to us: a comment claiming it would have gone on being wrong indefinitely,
and the symptom in production is a document stuck in `pending`: not an error
not an alert, just nothing happening.

Its first version was wrong in an instructive way too. It instrumented the
`get_db` override's teardown, so it could not see a route that commits for
itself, and it kept failing after the fix was already correct. Instrumenting
`session.commit` itself measures what the property is actually about.

## Consequences

**Good.** The race is gone. Both rows are written in one transaction and are
visible to the worker before the worker is told they exist.

**Bad.** Two routes now know about transactions, which weakens the rule that
services and routes never commit. Weakened deliberately and narrowly: it is
visible, commented, and limited to endpoints that hand work to another process.

**Bad, and unfixed.** If the process dies between the commit and the enqueue,
the document is committed as `pending` and no job exists. The same happens if
Redis is unreachable: the route logs the failure and still answers 202, because
the upload genuinely *was* accepted, and answering 500 would invite a retry that
creates a duplicate.

That gap is why `tasks` is a table rather than trusting Redis to remember. The
row is committed, says `queued`, and is exactly what a sweeper re-enqueues.
**The sweeper is not built**: a `tasks` row stuck in `queued` for more than a
few minutes is currently invisible, and closing that is M16 work. The proper end
state is the transactional outbox pattern: write the intent in the same
transaction (done), and have a separate process publish it (not done).

**Worth knowing.** arq job ids are derived from the task row
(`ingest:<task-uuid>`) rather than random, and arq treats a job id as an
idempotency key. So a retried request, or a future sweeper re-enqueueing
something that only *looked* stuck, cannot put two copies of the same work in
the queue.

## Alternatives rejected

**Enqueue inside the transaction and let the worker retry when the row is
missing.** The common shortcut. Rejected because "not committed yet" and
"deleted by the user" are indistinguishable to the worker, and they need
opposite responses, wait and retry versus stop immediately. Guessing wrong in
either direction is worse than not having the race.

**Defer the job by a few seconds (`_defer_by`).** Makes the race unlikely
rather than impossible, and buys that with slower ingestion for every upload.
A timing window is not a guarantee.

**A SQLAlchemy `after_commit` event hook.** The right shape, and it does not
fit: the event is synchronous and the enqueue is a coroutine, so it would have
to schedule a task onto the loop from inside a sync callback, more machinery
and more failure modes than one explicit `await session.commit()`.

**A full transactional outbox now.** The correct long-term answer, and it needs
a publisher process, its own polling loop, and its own failure handling. The
`tasks` table is deliberately shaped so this can be added later without a
migration; building the publisher at M5 would be infrastructure for a system
with no users.
