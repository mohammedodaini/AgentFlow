# ADR-0019: Fail open, fail loud, and append only

- **Status:** accepted
- **Date:** 2026-08-21
- **Milestone:** M16

## Context

M16 is the production milestone: deploy, monitoring, Sentry, rate limiting, a
security hardening pass, a load test. Most of it is standard, and standard is
what it should be. Four decisions in it are not obvious, and each one has a
failure mode on both sides.

## Decision

### The rate limiter fails open

If Redis is unreachable, every request is allowed.

Failing closed is the instinct, and it is wrong here. Redis in this system is
already the *optional* dependency: the M3 refresh-token denylist degrades to "a
logged-out token still works until it expires", not to "nobody can log in". The
threat the limiter defends against is abuse and runaway cost, not an attacker
with an unlimited budget, and against abuse, ninety seconds of unmetered traffic
during a cache blip is a much smaller loss than a full outage every time Redis
restarts.

A system where an *authentication* check depended on Redis would deserve the
opposite answer, and the module says so.

### Startup fails loud

`/metrics` publishes request rates, error counts, latency, token spend and
per-agent activity. That is a reconnaissance feed: traffic patterns, which routes
exist, when a deploy happened, whether an attack is working. Production refuses to
start with metrics enabled and no token.

**Refused rather than silently disabled**, which was the tempting alternative:
turning monitoring off by default in production is the other way to lose, and it
loses quietly. The operator is told which variable to set and given the escape
hatch (`METRICS_ENABLED=false`) for a cluster where the endpoint is unreachable
from outside.

This is the fifth guard in the same validator, placeholder signing key, offline
embedder, missing model key, offline OAuth provider, and now this. Each one exists
because the failure it prevents is silent.

### The audit trail is a table, append-only, and never carries a secret

`structlog` already records requests, and it is not an audit log. Logs rotate,
they are sampled under load, they are shipped to a third party, and nobody
promises they are complete. "Who connected our Stripe account, and when?" has to
be answerable a year later, after the retention window closed and the person left.

So `events` is a table, written in the same transaction as the thing it records
an integration that connected and an event saying so commit together or not at
all. A log file cannot promise that.

**No `updated_at`.** Every other table here carries `TimestampMixin`; a column
recording when a row *changed* would imply these rows change, and an audit entry
that can be edited is not evidence. `created_at` comes from the *database's*
clock, because evidence should not be timestamped by the thing being audited.

**One writer, and it redacts.** `EventService._safe` strips credential-shaped keys
recursively, `{"grant": {"access_token": ...}}` passes a top-level check and
fails the purpose. The rule lives in one place because a rule applied at each call
site is a rule somebody skips.

And the trade that runs the other way: **recording never breaks the operation
being recorded.** An audit write that raised would turn a working feature into an
outage. This is the opposite of what a compliance-first system chooses, where an
unauditable action must not proceed, and it is written down so the day this
project needs that behaviour, the change is one function.

### Metrics are hand-rolled, and cardinality is the thing being defended

`prometheus-client` is the obvious dependency and the right one for most
projects. Two things argue against it here: the exposition format is a documented
text protocol of about fifteen lines to emit, and the library's real value
process collectors, multiprocess mode, a global default registry, is value this
deployment cannot use. One uvicorn process per container, scaled by adding
containers, needs none of it.

The global registry is the active objection. A module-level singleton means two
apps in one test process share counters, which is exactly the shape `create_app()`
exists to avoid. `MetricsRegistry` is per-application and lives on `app.state`.

Stated as a trade rather than a win: it is a hundred lines nobody else maintains,
with no summaries, no exemplars, no OpenMetrics. Take the dependency if those are
ever needed.

**What the code actually defends against is label cardinality.** A path label
containing a UUID is one time series per request; memory grows without bound and
the scrape eventually times out: the endpoint added to observe the process being
what takes it down. Routes are templated, unmatched paths share one series, and
there is a hard ceiling as a backstop.

## Consequences

**Six bugs, and five of them were only findable by running the product the way it
will be deployed.** They are listed in the milestone note; the pattern is that
none of them fails a test suite. A root-owned Docker volume, a missing lockfile
copy, a second `Server` header, a compose project name that collided with
development, pnpm's symlinks defeating Next's file tracing: every one is invisible
until something runs in a container.

**The load test measures the limiter unless told not to.** Its first run refused
341 of 400 requests and reported the agent scenario at "1335 rps, p50 4.6ms",
which was 48 rate-limit responses being served very fast: the best-looking row in
the table, and meaningless. The tool now says so above a threshold, because a
number that flatters the system is worse than no number.

**`events.ip_address` needed the frontend's cooperation to mean anything.** The
browser never talks to the API (ADR-0016), so the backend saw the web container's
address for every sign-in: one constant value in the column whose purpose is
spotting eighty failed sign-ins from one address. The frontend now forwards
`X-Forwarded-For`, which is correct behind a load balancer and honestly cannot do
better without one: Next does not expose the socket's peer address.

**Two audit helpers, and the distinction is a bug from three milestones.**
`record()` flushes and commits with the caller's transaction. `record_now()`
commits, and exists for paths that are *about to raise*: a failed sign-in is
recorded just before `raise AuthenticationError`, and `get_db` rolls back on any
exception. This is M14's `_mark_revoked` bug and M12's approval-decision bug in a
third costume, and the shared rule finally has one sentence: **a fact learned
about the outside world must survive the failure it records.**

## What is not verified, and what is not built

**This is a single-host deploy.** `docker-compose.prod.yml` is a definition of the
services and how they fit, suitable for one machine and as the source for a
Kubernetes manifest or an ECS task. It is not a highly-available topology: one
replica of each service, no TLS termination, no managed database, no backups, no
log shipping, no secret manager. Every one of those is a real deployment's
problem and none is a code change.

**The load test ran on the machine it was measuring**, so its numbers are a lower
bound on latency and an upper bound on throughput. It answers "does this fall
over": it did not, and not "what will this cost at scale".

**OpenTelemetry is not built**, though the roadmap lists it. `/metrics` plus
structured logs with a request id answer "is it up, is it slow, what is erroring"
for a single service. Distributed tracing earns its keep when there are several
services to trace *between*, and there is one.

**No penetration test.** The hardening pass here is headers, rate limiting, an
audit trail, non-root containers, a locked dependency set, and the guards that
refuse an unsafe production configuration. That is a checklist honestly worked
through, not an adversary having tried.
