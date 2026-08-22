# Operations: deploying, rolling back, and what to do when it breaks

The gap a production audit found after M16: the stack could be deployed and had
no documented way back. This is that way back.

**Scope, honestly.** This describes a single-host Docker deploy — one replica of
each service, no TLS termination, no managed database, no secret manager. It is
what `docker-compose.prod.yml` actually is. Everything here is also the source a
Kubernetes or ECS runbook would be written from, because the *decisions* are the
same and only the mechanism changes.

---

## The one rule that makes rollback possible

**A migration must be readable by the version of the application before it.**

Rolling the application back does **not** roll the database back. `make
prod-rollback` runs yesterday's image against today's schema, so if today's
migration renamed a column, yesterday's code queries a column that is gone and
the rollback fails as badly as the deploy did.

So every migration is expand-then-contract, across two releases:

| | Release N | Release N+1 |
|---|---|---|
| Add a column | add it nullable, write to it, keep reading the old one | start reading it |
| Remove a column | stop reading it | drop it |
| Rename a column | add the new one, write both | drop the old one |
| Change a type | add a new column, backfill, write both | drop the old one |

This project has already broken that rule once and it is worth naming: M10's
`ALTER TYPE run_status RENAME VALUE` was not backward compatible. It was correct
then, because there was nothing deployed to be compatible with. It would not be
correct now.

**Before every deploy that includes a migration, ask: could the previous image
run against this schema?** If the answer is no, `prod-rollback` is not available
for that release and the recovery path is *restore from backup* instead. Say so
in the release notes rather than discovering it during an incident.

---

## Deploy

```bash
make check && make eval          # the gates, before anything is built
scripts/release.sh               # builds and tags agentflow/{api,web}:<sha>
```

`release.sh` refuses a dirty working tree. An image tagged with a SHA whose tree
had uncommitted changes is an image nobody can reproduce — and it is exactly the
one you will be trying to reproduce, because it is the one that broke.

```bash
make prod-backup                                 # ALWAYS, if a migration is in this release
APP_VERSION=<sha> make prod-deploy
make prod-migrate                                # only after the new image is healthy
```

Note the order: **the new image starts before the migration runs.** That is only
safe because of the expand/contract rule above — the new code must tolerate the
old schema for the seconds between. The alternative order (migrate, then deploy)
requires the *old* code to tolerate the *new* schema, which is the same
constraint pointing the other way, and leaves a window where a rollback has
nowhere to go.

Then verify, in this order, because each step is cheap and rules out the layer
below it:

```bash
curl -fsS localhost:8000/api/v1/health/live      # {"status":"ok","version":"<sha>"} — the right build?
curl -fsS localhost:8000/api/v1/health/ready     # {"status":"ready", database:true, redis:true}
curl -sI  localhost:8000/api/v1/health/live      # Server: agentflow, nosniff, DENY
make prod-logs                                   # no tracebacks in the first minute
make smoke                                       # 24 checks through a real browser
```

`make smoke` is the one that matters. The health check proves the process is up;
the smoke test proves a person can sign in, ask a question, and get an answer.

**Before a release, also run the failure drill** — separately, because it kills
the API and so cannot live inside `make smoke`:

```bash
make boundary
```

Ten checks on what a user sees when things go wrong: a mistyped URL, a session
the backend has revoked, and the API down mid-session. It exists because a
production audit found there was no error boundary anywhere in the app, so an
outage rendered Next's bare "Application error: a server-side exception has
occurred" — no explanation, no way back, no reference to quote to support.

---

## Roll back

```bash
make prod-versions                # what this host can roll back to
make prod-rollback VERSION=<sha>
```

It re-runs an image that already exists — **no rebuild**. Compiling under
pressure, from a tree that has moved on, is how a bad deploy becomes a bad
afternoon. The script waits for `/health/ready` and fails loudly if it does not
come back within 60 seconds.

### This has been rehearsed

A rollback path nobody has exercised is a hypothesis. On 2026-08-21 the drill was
run end to end against the container stack: two real builds from two real
commits, deploy the newer, roll back to the older.

```
make prod-versions            85dff8c  ·  e0e8804

APP_VERSION=85dff8c make prod-deploy
  /health/live                {"status":"ok","version":"85dff8c"}

make prod-rollback VERSION=e0e8804
  ready on e0e8804
  api     agentflow/api:e0e8804   Up (healthy)
  web     agentflow/web:e0e8804   Up
  worker  agentflow/api:e0e8804   Up
  /health/live                {"status":"ok"}     ← no version field: that build predates it
  /health/ready               {"status":"ready", database:true, redis:true}
  make smoke                  23/23
```

Two things are worth reading twice. The rolled-back build answers `/health/live`
*without* a `version` field, because the field did not exist at that commit — the
absence is the proof that this is genuinely the older image and not a cached
newer one. And **23/23 smoke checks passed on the rolled-back build against the
newer schema**, which is the expand/contract rule holding rather than being
asserted: the `events` table added by the later release was already there, and
the earlier code neither knew nor cared.

Rehearse it again whenever the deploy shape changes. The first run of the release
script failed twice — once demanding production secrets to build an image, once
polling the wrong port — and both would have been discovered during an incident
instead.

**If the release contained a migration that was not backward compatible**, this
is the wrong tool. Restore instead:

```bash
make prod-down
docker volume rm agentflow-prod_pgdata
APP_VERSION=<previous sha> make prod-deploy
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U agentflow agentflow < backups/agentflow-<timestamp>.sql
```

That loses every write since the backup. It is the reason the expand/contract
rule is a rule and not a preference.

---

## When something is wrong

Work down the list. Each step tells you whether to keep going.

**1. Is it up?**
```bash
docker compose -f docker-compose.prod.yml ps
```
`api` restarting means it is failing at startup. Almost always configuration:
`Settings` refuses five specific unsafe production values and names the variable
each time. `make prod-logs` shows which.

**2. Is it up but unhealthy?**
```bash
curl -s localhost:8000/api/v1/health/ready
```
`database:false` — Postgres is down, or the password changed, or the volume was
recreated. `redis:false` — Redis is down; the API keeps serving, sign-out
revocation and background jobs do not.

**3. Is it slow?**
```bash
curl -s -H "Authorization: Bearer $METRICS_TOKEN" localhost:8000/metrics \
  | grep -E 'duration_seconds_(sum|count)|requests_total'
```
Divide sum by count per route for a mean, and read the buckets for the tail. If
one route dominates, that is the one to look at. If everything is slow at once,
suspect CPU: the load test and `agentflow_agent_runs_total` will say whether it is
agent traffic.

**4. Is something being refused?**
429s in `agentflow_http_requests_total` mean the limiter is working. That is
either abuse — check the audit trail — or the limit is too low for real traffic,
in which case raise `RATE_LIMIT_PER_MINUTE` rather than switching it off.

**5. Is someone attacking it?**
```sql
SELECT ip_address, count(*) FROM events
WHERE event_type = 'user.sign_in_failed' AND created_at > now() - interval '1 hour'
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```
The audit trail records every failed sign-in with its address.

An account now locks for fifteen minutes after fifteen failures — `reason:
"locked"` in the payload marks attempts refused by it. That counts per
**account**, not per address, because credential stuffing sprays one account from
thousands of addresses and every one of them fits inside a per-IP budget.

The trade, stated: somebody who knows a user's email address can lock that user
out for fifteen minutes. That is accepted — the alternative is leaving the account
guessable — and it is why the window is short and a successful sign-in clears the
count.

---

## Backups

`make prod-backup` writes a `pg_dump` to `backups/`, which is gitignored.

A `backup` service now runs in the stack. Every six hours it writes a compressed
dump to `backups/` on the **host** — a bind mount, not a named volume, because a
named volume lives inside Docker's storage and `docker volume prune` would take
the backups along with the database they exist to protect. Dumps older than
fourteen days are pruned.

Each dump is written to `.partial` and renamed only on success. A dump interrupted
halfway otherwise leaves a file that looks exactly like a backup and restores into
a half-populated database; the atomic rename means every `*.sql.gz` is complete.

### Restoring

```bash
scripts/restore.sh                                   # lists what is available
scripts/restore.sh backups/agentflow-<stamp>.sql.gz  # restores that one
```

**There is no default, and a drill is what taught that.** The first version
restored the newest backup when given no argument, which is precisely wrong in the
situation the script exists for: backups run on a schedule, so by the time anybody
notices the problem the scheduler has already captured it. The rehearsal wiped
every user, ran `restore.sh` with no argument, and it faithfully restored the
empty database — and reported success.

So answer "when did this start?" *first*, then name the file from before it.

### This has been rehearsed

```
6 users, 7 approvals, 25 events
  -> backup taken
  -> every user, approval and event removed
  -> 0 users, 0 approvals, 0 events
  -> scripts/restore.sh backups/agentflow-20260822T011951Z.sql.gz
  -> 6 users, 7 approvals, 25 events
  -> the named drill user is back, and the approval it created, summary intact
  -> make smoke  24/24
```

The last line is the one that matters. A restore that repopulates tables but
leaves the product broken is not a recovery.

### What is still missing

**No off-host copy.** `backups/` is a directory on the same machine as the
database. That survives a bad migration, a bad deploy and a dropped table; it does
not survive a lost disk. Point `BACKUP_DIR` at a mounted network volume, or rsync
the directory elsewhere on a timer — either is a deployment decision rather than a
code change, and until one is made this is a single point of failure.

**No encryption at rest.** The dumps hold Fernet ciphertext for OAuth tokens (so
those stay protected by `TOKEN_ENCRYPTION_KEY`), but everything else — addresses,
documents, message bodies — is plaintext SQL in a directory.

**Nothing verifies a backup except restoring it.** The drill above was run by
hand. A weekly automated restore into a scratch database is the standard answer
and is not built.

---

## What is deliberately not here

**No blue/green or canary.** One replica means a deploy is a restart, with a few
seconds of downtime while the new container becomes healthy. `restart:
unless-stopped` and the healthcheck keep it from staying down.

**No secret rotation procedure.** `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY` are
separate for a reason (ADR-0014) and rotating either has consequences the code
does not currently handle: rotating the signing key logs everybody out, and
rotating the encryption key makes every stored OAuth token undecryptable. Neither
has a migration path yet.

**No on-call rotation or escalation.** There is one operator.
