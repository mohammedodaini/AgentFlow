# ADR-0006: Tests run inside a transaction that is rolled back

- **Status:** accepted
- **Date:** 2026-08-12
- **Milestone:** M4

## Context

Tests that share a database have to answer one question: how does test *n+1*
start from a clean state?

Getting it wrong produces the two worst failure modes a suite can have. Tests
that pass alone and fail together, which people "fix" by re-running until
green. And tests that pass in the order they were written and fail when a new
one is inserted above them — at which point the suite stops being evidence and
becomes a ritual.

M2 and M3 used `TRUNCATE ... CASCADE` after each test. It worked, and it had
two costs that grow: a round trip per table per test (three tables now, twenty
by M12), and no protection at all when a test fails *before* its cleanup runs.

By M4 the suite was 115 tests and about to become the thing every later
milestone is checked against, so it was worth doing properly once.

## Decision

Each test opens a connection, begins a transaction, and rolls it back when the
test ends. Everything the test does happens inside that transaction.

Three pieces make it work:

**The session joins the test's transaction.** `AsyncSession` is bound to the
already-open connection with `join_transaction_mode="create_savepoint"`, so
application code that calls `commit()` — as it should, because that is what it
does in production — releases a SAVEPOINT rather than committing for real. The
outer transaction stays open and undoes everything.

**HTTP requests join it too.** The `app` fixture overrides the `get_db`
dependency to yield that same session. Without this, end-to-end tests would let
the application open its own connections and commit for real, leaving rows the
rollback never sees.

**The schema is built once per session.** A synchronous, session-scoped fixture
runs `create_all` via `asyncio.run` before any test's event loop exists — which
also sidesteps the question of which loop a session-scoped async fixture
belongs to.

Redis is exempt: it has no transactions. It is flushed once per test, and the
suite uses database 1 while development uses 0.

## Consequences

**Good.** Isolation is total and needs no cleanup code, so a test that fails
halfway through leaves nothing behind — the case truncation handled worst.
Order-independence is structural rather than a property somebody has to
maintain. The suite got faster while gaining 41 tests: 13.5s to under 10s, and
`make test-fast` (unit only) runs in 0.66s.

The `get_db` override buys something beyond speed: a test can assert directly
against the database and see exactly what an HTTP request wrote, in the same
transaction, before anything is committed.

**Bad.** The fixtures are noticeably harder to understand than `TRUNCATE`.
`join_transaction_mode="create_savepoint"` is a genuinely obscure setting, and
someone will eventually spend an afternoon working out why their `commit()` did
not persist. That cost is paid once, in `tests/conftest.py`, and the docstrings
there carry the explanation.

**Also bad.** Code that manages transactions itself cannot be tested this way —
anything calling `begin()` on its own connection, or relying on a real commit
being visible to a *different* connection. Nothing does today. When something
does (a worker holding an advisory lock, say), it will need the truncation
approach, and the two will have to coexist.

**Worth knowing:** because the schema is created with `create_all` rather than
by running migrations, a missing migration would not fail the suite. That is
deliberate — it keeps schema bugs and migration bugs separable — and
`alembic check` is the tool that answers the other question.

## Alternatives rejected

**TRUNCATE between tests** (what M2 and M3 did). Simple and obvious. Rejected
on the two costs above: it scales with the number of tables rather than the
number of rows, and it does not run when a test dies.

**Drop and recreate the schema per test.** Total isolation, trivially correct,
and far too slow — seconds per test against a schema that will keep growing.

**A separate database per test.** Isolation without any of the savepoint
subtlety. Rejected as strictly more expensive than a transaction for the same
guarantee. Worth revisiting only if the suite moves to `pytest-xdist`, where
each *worker* will need its own database — a per-worker suffix on the database
name, not a per-test one.

**Mocking the database entirely.** Fast, and it tests nothing that matters
here. A `UNIQUE` constraint, an `ON DELETE CASCADE` and a server-side `now()`
are enforced by Postgres; a mock would happily accept a duplicate email and
report success.
