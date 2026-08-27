# M16: Production: the milestone where running it found the bugs

- **Date:** 2026-08-21
- **Status:** shipped, **the last milestone on the roadmap**
- **ADRs:** [ADR-0019](../adr/0019-production-choices-fail-open-fail-loud-and-append-only.md)

Deploy, monitoring, Sentry, rate limiting, a security hardening pass, a load
test. Most of it is standard, and standard is what it should be.

What is worth recording is that **six bugs came out of this milestone and five of
them could not have been found by a test.** They came from building the images,
running the containers, driving a browser at them, and reading the numbers a load
test produced. A green suite says the code does what it was written to do; it says
nothing about whether the thing that gets deployed works.

## What was built

| Piece | Files | What it does |
|---|---|---|
| Rate limiting | `app/middleware/rate_limit.py` | Redis window per caller, weighted by cost |
| Security headers | `app/middleware/security_headers.py` | nosniff, DENY, CSP, HSTS in production |
| Metrics | `app/monitoring/metrics.py`, `app/api/metrics.py` | RED plus tokens and spend, at `/metrics` |
| Sentry | `app/monitoring/sentry.py` | Optional, and silent without a DSN |
| Audit log | `app/models/event.py`, `app/services/event_service.py` | Append-only, redacting, tenant-scoped |
| Audit API | `app/api/v1/routes/events.py` | Read-only, owners and admins |
| Deploy | `docker-compose.prod.yml`, two Dockerfiles | The whole product in containers |
| Load test | `scripts/loadtest.py` | Percentiles, errors and refusals, kept apart |

The four decisions worth arguing about are in
[ADR-0019](../adr/0019-production-choices-fail-open-fail-loud-and-append-only.md):
the limiter fails **open**, startup fails **loud**, the audit trail is
**append-only** and never carries a secret, and the metrics registry is
hand-rolled because the library's value is value this deployment cannot use.

**This migration was the first since M2 with no enum to hand-drop.** Six
milestones running shipped a native Postgres enum whose *type* survives
`DROP TABLE`, and autogenerate never once wrote the `DROP TYPE`. `event_type` is a
plain `String(64)`: an audit vocabulary grows with every feature, which is the
wrong shape for a native enum, so the generated downgrade is complete as written.
Rehearsed both ways.

## Bugs this milestone found

**1. Every upload returned 500 in the containers.** The storage volume is
root-owned and the container runs as `appuser`; Docker seeds a fresh named volume
from the image's mountpoint *including its ownership*, and with no directory there
it makes one owned by root. `StorageError: Could not store object: Permission
denied`, on a stack where Postgres, Redis and every other route were healthy.

Invisible in development, because `make dev` runs as the developer and writes to a
directory they own. It appeared the first time the product ran the way it will be
deployed.

**2. The production image never used the lockfile.** The Dockerfile copied
`pyproject.toml` alone and ran `uv sync`, re-resolving from scratch on every
build, and this project's specifiers have no upper bounds. That is not
theoretical: `uv lock` silently moved mypy from 1.x to 2.3 during M6. Two builds
of the same commit, a week apart, could ship different major versions of
`anthropic` or `sqlalchemy`, which is a release that cannot be reproduced and a
bug that cannot be bisected. Now `COPY pyproject.toml uv.lock` and
`uv sync --frozen`.

**3. `Server: uvicorn` was still being sent.** `SecurityHeadersMiddleware` sets
`Server: agentflow`, and uvicorn writes its own at the protocol layer *after* the
application response, so both went out and the banner the middleware existed to
suppress was still there. Middleware cannot remove it; `--no-server-header` can.
Found with `curl -I` against the container.

**4. The production compose adopted the development database.** Compose derives
its project name from the directory, so `docker-compose.prod.yml` and
`docker-compose.yml` produced the same one: identical container names and, worse,
the same `pgdata` volume. It surfaced as `InvalidPasswordError`, because the
volume was initialised with the development password and `POSTGRES_PASSWORD` is
ignored on an existing data directory.

**The failure was loud. The success would not have been**: with a matching
password the production stack would have started, migrated, and served traffic out
of a developer's database. Fixed with `name: agentflow-prod`.

**5. The frontend container crash-looped on `MODULE_NOT_FOUND`.** pnpm's default
layout is a content-addressed store plus symlinks, and Next's
`output: "standalone"` traces the files it needs and *copies* them, so the copy
landed full of links to paths that were never copied. The build succeeded and the
container died on `@swc/helpers` from inside next's own require hook. Fixed with
`--config.node-linker=hoisted` in the image only, so local development keeps the
fast layout.

**6. The histogram double-counted, and a test caught that one.**
`Histogram.observe` incremented every bucket the value fell under, already
cumulative, and `render` accumulated a second time. With observations at 5ms
30ms, 200ms and 3s, the `le="0.05"` bucket reported 3 instead of 2. Nothing
raises: the histogram renders, the dashboard draws, and every quantile is wrong in
the direction that makes the service look slower than it is. That is the kind of
monitoring bug that gets a real regression dismissed as "the metrics are always
weird".

**7. Agent spend was counted and never emitted.** `observe_agent_run` was written
and unit-tested while nothing called it: a dashboard panel that never fills
where a reader cannot tell "no agent ran" from "this is not measured". It is the
same mistake this milestone's own `EventType` docstring argues against, made two
files away. Every terminal state now goes through one `AgentService.finish`, so a
sixth call site cannot forget the counter.

**And one thing that was not a bug but was worse than one.** `events.ip_address`
recorded `172.19.0.6` for every sign-in: the *web container's* address, because
the browser never talks to the API (ADR-0016). A column holding one constant value
looks like data and is useless for the thing it exists for, which is noticing
eighty failed sign-ins from one address in a minute. The frontend now forwards
`X-Forwarded-For`, which is correct behind a load balancer and honestly cannot do
better without one.

## The load test measured the rate limiter, and said so

Its first run against the container stack refused **341 of 400** requests and
reported the agent scenario at `1335.6 rps, p50 4.6ms`, 48 rate-limit responses
served very quickly, and the best-looking row in the table.

The tool now warns above a refusal threshold, because a number that flatters the
system is worse than no number. With the limiter raised, the honest figures from
one container on this laptop, 16 concurrent clients:

```
  health (no auth, no db)      400 reqs    717.0 rps  p50   9.6ms  p95   69.0ms  p99   94.6ms
  readiness (probes both)      400 reqs    501.7 rps  p50  19.9ms  p95   77.2ms  p99  281.7ms
  list runs (auth + query)     400 reqs    183.4 rps  p50  68.9ms  p95  170.1ms  p99  448.6ms
  supervised agent run          48 reqs     37.8 rps  p50 207.1ms  p95  272.6ms  p99  286.6ms

  no server errors under load
```

`list runs` went from a fake 370 rps to a real 183. The client shared a machine
with the server, so these are a lower bound on latency and an upper bound on
throughput, which is the honest way round.

## Verified at runtime

The whole product, in containers, driven by a real browser:

```
docker compose -f docker-compose.prod.yml up -d     4 services: postgres, redis, api, worker, web
alembic upgrade head                                 events table created
/api/v1/health/ready                                 {"status":"ready", database:true, redis:true}
curl -I                                              nosniff · DENY · CSP · Server: agentflow
/metrics without a token                             401
/metrics with the token                              agentflow_uptime_seconds 30.6
make smoke against :3001                             23/23
```

And production's guards, verified **in a container** rather than in a unit test:
starting the stack without an embedding key stopped the deploy with
`EMBEDDING_PROVIDER is 'openai' but OPENAI_API_KEY is empty`: the M6 guard doing
its job in the deploy path.

The audit trail after that browser session, read straight from Postgres:

```
 approval.requested   | 4 |
 approval.rejected    | 4 |
 organization.created | 4 |
 user.registered      | 4 | 172.19.0.6

 payload: {"reason": "Already booked.", "approval_id": "01a02179-…"}
```

Four rejections recorded with their reasons, and no message bodies, tokens or
document contents anywhere in a payload.

## Gate

```
ruff · ruff format · mypy --strict (255 files) · alembic check (rehearsed both ways)
925 tests, 2 skipped · 97.04% coverage (gate 97%)
make eval: handbook unchanged · routing 1.000 vs 0.300 control
frontend: lint · typecheck · build · smoke 23/23 against the container stack
```

One dependency added: `sentry-sdk[fastapi]`, optional at runtime: no DSN, no init.

## Known gaps, deliberately left

**This is a single-host deploy.** One replica of each service, no TLS
termination, no managed database, no backups, no log shipping, no secret manager.
`docker-compose.prod.yml` is a definition of the services and how they fit
suitable for one machine, and the source a Kubernetes manifest or an ECS task
would be written from. Every one of those gaps is a real deployment's problem and
none is a code change.

**OpenTelemetry is not built**, though the roadmap lists it. `/metrics` plus
structured logs carrying a request id answer "is it up, is it slow, what is
erroring" for a single service. Distributed tracing earns its keep when there are
several services to trace *between*.

**No penetration test.** The hardening here is headers, rate limiting, an audit
trail, non-root containers, a locked dependency set, and five startup guards that
refuse an unsafe production configuration. That is a checklist honestly worked
through, not an adversary having tried.

**The limiter is a fixed window.** At a boundary a caller can spend two windows'
budget in a couple of seconds. A sliding log fixes it and costs an entry per
request; for a limit whose purpose is bounding abuse, that is not the trade worth
paying on every request.

## Reproduce

```bash
cp .env.example .env      # then fill in the six the compose file refuses to default
make prod-up
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
make loadtest
```

Then open http://localhost:3000. The product runs entirely in containers, as
non-root, with a locked dependency set, an audit trail, and a limiter in front of
it.
