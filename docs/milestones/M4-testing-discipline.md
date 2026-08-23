# M4: Testing discipline

**Status:** complete (2026-08-12) · **Gate:** `make check` green · **Tests:** 156 passing (was 115) · **Coverage:** 98.7%

M1–M3 wrote tests. M4 makes the suite something later milestones can be
*checked against*: isolated, fast, layered, and guarded by a coverage number
that means something.

This is the milestone with the least visible output and the longest shadow.
Every feature from M5 on is built on top of it.

## What was built

| Piece | Where |
|---|---|
| Transactional isolation (one rolled-back transaction per test) | [`tests/conftest.py`](../../backend/tests/conftest.py) |
| `get_db` override so HTTP requests join the test's transaction | same |
| Session-scoped schema creation | same |
| Test data factories | [`tests/factories.py`](../../backend/tests/factories.py) |
| Automatic `unit`/`integration`/`e2e` markers | `pytest_collection_modifyitems` |
| Coverage gate + omit list | [`pyproject.toml`](../../backend/pyproject.toml) |
| Reconciliation of the omit list against reality | [`tests/unit/test_stub_manifest.py`](../../backend/tests/unit/test_stub_manifest.py) |
| `make test-fast`, `make test-pyramid` | [`Makefile`](../../Makefile) |

## The four decisions worth understanding

**1. Isolation by rollback, not by cleanup.** Each test runs in a transaction
that is discarded when it ends. No cleanup code, nothing left behind when a
test dies halfway, and order-independence becomes structural rather than
something a person has to maintain. Full reasoning and the rejected
alternatives in
[ADR-0006](../adr/0006-tests-run-inside-a-rolled-back-transaction.md).

**2. Factories exist for readability, not typing.** A test that opens with
eight lines of setup buries its one interesting line. The rule: the factory
fills in everything the test does not care about, so the argument that *is*
named is visibly the subject of the test.

```python
# the subject is the email
await make_user(session, email="ada@example.com")

# the subject is the role rule; nothing else is named
organization, user, membership = await make_org_with_owner(session)
```

Argon2 is deliberately ~50 ms per hash, so the factory hashes
`DEFAULT_PASSWORD` **once per session** and shares the digest. Hashing per user
would have put a minute of pure CPU into a ten-second suite.

**3. Markers are derived, not written.** A marker you have to remember ends up
on two thirds of the suite. They are applied from the directory a test lives
in, which makes the pyramid measurable:

```
$ make test-pyramid
unit         83/156
integration  20/156
e2e          53/156
```

A serviceable shape: a wide, fast base, though e2e is heavier than the
classic pyramid wants. That is the right trade for now: those tests are what
prove the tenancy boundary holds, and there is no cheaper way to check it. If
they get slow, the answer is to push privilege-escalation cases down into
service-level tests, not to delete them.

**4. The coverage gate is honest about what it measures.** Roughly two thirds
of `app/` is still scaffolding. Measuring all of it would report how much of
the roadmap exists rather than how well the built part is tested: a number
that *falls* every time someone adds a stub, which teaches people to lower the
threshold. So stubs are omitted, and `fail_under = 97` applies to real code.

The interesting part is that the omit list cannot rot. An implemented module
left on the list is untested code reporting as nothing at all: the exact
failure a gate exists to prevent, and the default behaviour of every
hand-maintained exclusion list. `test_stub_manifest.py` reconciles the list
against the source tree **in both directions**, using a signal that is crude
and hard to fake: a module defining no class and no function is scaffolding.
Implement anything and the test fails until the entry is removed.

## The bug this milestone found

Coverage was **under-reporting by 11 points**, and the reason mattered more
than the number.

`organization_service.py` reported 57% while the end-to-end suite was
demonstrably exercising it. The "uncovered" lines included `return membership`,
on the hottest path in the application, run by nearly every request in the
suite.

The cause: SQLAlchemy's asyncio layer runs its work inside greenlets, and
coverage does not trace greenlets unless told to. Every line executed under an
`await session.execute(...)` was invisible.

```toml
[tool.coverage.run]
concurrency = ["greenlet", "thread"]
```

Total coverage went 85% → 96% on that one line of configuration, and the
service layer went 57% → 98%.

Worth dwelling on, because the failure mode is nasty: a gate that
under-reports is worse than no gate. It would have driven someone to write
redundant tests chasing lines that were already covered, or to conclude the
number was noise and stop looking at it.

## What the new tests actually cover

The 41 tests added were not written to move a number. Each covers a branch the
end-to-end suite structurally cannot reach:

- a token whose user was **deleted** between issue and use
- a token whose subject is not a UUID (only reachable if the signing key
  leaked, and it still must not 500)
- login against a **deactivated** account, failing with the *same* message as a
  wrong password
- a password hash with **obsolete cost parameters** being silently upgraded at
  login (tested with a genuinely weak Argon2 hash, not a fabricated string)
- the **slug allocation** loop giving up instead of hanging
- the **denylist TTL** matching the token's remaining life: the property that
  stops the key space growing forever
- a token expiring *between* being decoded and being revoked
- the error handler's unmapped-error path, its body shape, and
  `WWW-Authenticate`

## Verified

```
$ make check
All checks passed!                                   # ruff
154 files already formatted                          # ruff format
Success: no issues found in 154 source files         # mypy strict
Required test coverage of 97.0% reached.
Total coverage: 98.74%
156 passed in 9.90s

$ make test-fast
83 passed, 73 deselected in 0.66s
```

156 tests in under ten seconds, up from 115 in 13.5, more tests in less time
because rollback is cheaper than truncation and the schema is built once.

## Known gaps, deliberately left for later

- **No `pytest-xdist`.** Parallelism needs a database per worker, not per test.
  Worth doing when the suite passes ~30 seconds; it is at 10.
- **Tests build the schema from models, not migrations.** Deliberate: it keeps
  schema bugs separate from migration bugs, and `alembic check` covers the
  other question. A CI step running `alembic upgrade head` against an empty
  database would close it properly.
- **No mutation testing.** Coverage says a line ran, not that anything would
  have noticed if it were wrong. `mutmut` against `app/core/security.py` and
  `app/services/` would be the honest next step.
- **No property-based tests.** `hypothesis` on `slugify` and `uuid7` would
  likely find edge cases the example-based tests miss.
- **CI runs the whole suite in one step.** A fast `-m unit` step first would
  fail bad pushes in a second rather than a minute.
- **Coverage is line and branch, not path.** 98.7% is not "nothing is broken".

## Reproduce

```bash
make up && make migrate
make check          # the full gate, including the coverage threshold
make test-fast      # unit only, sub-second: the loop to use while writing code
make test-pyramid   # the shape of the suite
```
