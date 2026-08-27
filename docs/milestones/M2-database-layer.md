# M2: Database layer

**Status:** complete (2026-08-12) · **Gate:** `make check` green · **Tests:** 48 passing (was 12)

M1 proved a process could start and answer one request. M2 gives it state: a
schema, a migration that creates it, one session per request, and a readiness
probe that tells the truth about whether the database is reachable.

## What was built

| Module | Responsibility |
|---|---|
| [`app/core/ids.py`](../../backend/app/core/ids.py) | `uuid7()`: RFC 9562 §5.7 time-ordered primary keys |
| [`app/models/base.py`](../../backend/app/models/base.py) | `Base`, `NAMING_CONVENTION`, `UUIDPrimaryKeyMixin`, `TimestampMixin` |
| [`app/models/organization.py`](../../backend/app/models/organization.py) | `organizations`: the tenant |
| [`app/models/user.py`](../../backend/app/models/user.py) | `users`: login identity only |
| [`app/models/membership.py`](../../backend/app/models/membership.py) | `memberships` + `Role`: the many-to-many with a payload |
| [`app/models/__init__.py`](../../backend/app/models/__init__.py) | model registry, so autogenerate sees every table |
| [`app/db/session.py`](../../backend/app/db/session.py) | `create_engine()`, `create_session_factory()` |
| [`app/db/deps.py`](../../backend/app/db/deps.py) | `get_db()`: session per request, unit of work |
| [`app/monitoring/health.py`](../../backend/app/monitoring/health.py) | `check_database()`, `check_readiness()` |
| [`app/api/v1/routes/health.py`](../../backend/app/api/v1/routes/health.py) | `GET /health/ready`: 200 or 503 |
| [`alembic/`](../../backend/alembic/) + [`alembic.ini`](../../backend/alembic.ini) | async migrations wired to `Settings` and `Base.metadata` |

Three tables, one enum type, one migration:
`20260812_1531-5159b9b65a25_identity_and_tenancy_tables.py`.

## The five decisions worth understanding

**1. `memberships` is a table, not a column.**
The cheap version is `users.organization_id`. It caps every person at one
organization forever, and consultants, agencies and "personal workspace plus
employer" break that on day one. Retrofitting tenancy afterwards means
rewriting every query in the application, which is why this is the one thing
[docs/database.md](../database.md) insists on getting right before anything
else exists. The join table also gives `role` somewhere to live, and later the
invite state and seat billing.

**2. The constraint naming convention had to come first.**
Left alone, Postgres invents names like `organizations_slug_key`, and, worse
SQLAlchemy leaves unnamed constraints nameless in metadata, so Alembic cannot
reliably diff or drop them. `NAMING_CONVENTION` must be set before the first
table is defined; adding it later is a migration that renames every constraint
you already have. The generated migration is the proof it works: every name in
it reads `pk_organizations`, `uq_memberships_user_id`,
`fk_memberships_user_id_users`.

**3. The commit boundary is the request, not the service.**
`get_db()` yields a session, commits on success and rolls back on any
exception. Services never commit. A service that commits can never be composed
into a larger transaction: the moment two of them must succeed atomically,
every commit inside them has to be hunted down and removed. Services call
`flush()` when they need a generated id mid-request.

**4. Readiness probes Postgres, and deliberately not Redis.**
The original plan listed both. Nothing uses Redis until M5, and a readiness
probe answers "can this instance serve its traffic?", probing an unused
dependency means a Redis blip pulls a healthy API out of the load balancer.
Each probe carries a 2-second timeout, because a hung check is worse than a
failed one: without it the orchestrator's own probe expires with no answer and
every diagnosis reads "readiness timed out" instead of "the database is down".

**5. `expire_on_commit=False`.**
SQLAlchemy's default expires every attribute after a commit, so touching
`user.email` afterwards triggers a lazy refresh: a hidden database call that
under asyncio, raises `MissingGreenlet` from wherever you happened to be.
Turning it off is what lets a route serialise the row it just wrote.

## Three bugs this milestone caught

**The enum that outlives its table.** Autogenerate wrote a `downgrade()` that
drops all three tables, and leaves the `membership_role` type behind, because
Postgres types outlive the tables that use them. `downgrade` then `upgrade`
failed with `type "membership_role" already exists`, which is exactly what a
rollback rehearsal does. The migration now drops the type by hand. Verified:

```
$ alembic downgrade base
$ psql -tAc "SELECT count(*) FROM pg_type WHERE typname='membership_role'"
0
$ alembic upgrade head      # succeeds
```

**The id that isn't there yet.** The first version of `UUIDPrimaryKeyMixin`
claimed the id exists "before the INSERT". It does not: `default=` is a
*column* default, evaluated while SQLAlchemy builds the statement, so
`Organization(...).id` is `None` until `flush()`. The docstring was wrong, an
integration test caught it, and both now state the real timing.

**A redundant index**, spotted while reading the generated migration:
`ix_memberships_user_id` duplicated the leading column of
`uq_memberships_user_id`. Postgres serves `user_id` lookups from the composite
index, so the extra one was pure write cost. Removed. `organization_id` keeps
its own index because it is the *trailing* column, which a composite index
cannot serve.

## Verified at runtime, not just in tests

```
$ curl -D- -H "X-Request-ID: m2-trace" localhost:8000/api/v1/health/ready
HTTP/1.1 200 OK
content-type: application/json
x-request-id: m2-trace

{"status":"ready","checks":{"database":true}}
```

```
info  app.startup    app_name=agentflow env=development
info  http.request   duration_ms=41.57 method=GET path=/api/v1/health/ready
                     request_id=m2-trace status_code=200
```

Schema, as Postgres reports it:

```
$ psql -c '\d memberships'
Indexes:
    "pk_memberships" PRIMARY KEY, btree (id)
    "ix_memberships_organization_id" btree (organization_id)
    "uq_memberships_user_id" UNIQUE CONSTRAINT, btree (user_id, organization_id)
Foreign-key constraints:
    "fk_memberships_organization_id_organizations" FOREIGN KEY (organization_id)
        REFERENCES organizations(id) ON DELETE CASCADE
    "fk_memberships_user_id_users" FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
```

Models and database agree:

```
$ alembic check
No new upgrade operations detected.
```

## Test coverage

48 tests, up from 12:

- **unit (30)**: settings, logging, `uuid7()` bit layout and ordering, schema
  metadata (table names, constraint names, the index decision, role values),
  readiness probe behaviour including the hang-and-timeout path.
- **integration (9)**: against real Postgres: the tenancy graph, server-side
  timestamps, duplicate email rejected, one membership per (user, org), a user
  in several orgs, `ON DELETE CASCADE`, the role default, and that the enum is
  stored as `owner` rather than `OWNER`.
- **e2e (9)**: both health endpoints, including 503 with a named failing
  dependency, and that liveness stays green while readiness goes red.

Integration tests **skip** rather than fail when Postgres is unreachable, so
`pytest` on a laptop with Docker stopped reports skips instead of a wall of
connection errors. CI always has a server, so nothing is quietly lost.

## Known gaps, deliberately left for later

- **Test fixtures are minimal.** `tests/integration/conftest.py` creates tables
  with `create_all` and truncates between tests. Transactional isolation,
  factories and a coverage gate are M4's job: this is the smallest thing that
  proves M2's schema.
- **Tests build the schema from models, not migrations.** Fast, and it keeps
  schema bugs separate from migration bugs, but it means a missing migration
  would not fail the suite. `alembic check` answers that question instead.
- **`updated_at` only updates through the ORM.** A raw `UPDATE` in psql will
  not touch it. A database trigger would cover that; not worth it yet.
- **No `citext` on `users.email`.** Postgres comparison is case-sensitive, so
  `Bob@x.com` and `bob@x.com` would be two accounts that both satisfy the
  unique constraint. M3's service layer lowercases on write, which is the
  simpler half of the fix.
- **Redis is not probed**: M5, with the connection pool.

## Reproduce

```bash
make up        # Postgres 17 + pgvector, Redis
make migrate   # apply migrations
make check     # ruff + ruff format + mypy + pytest
make dev       # http://localhost:8000/api/v1/health/ready
```
