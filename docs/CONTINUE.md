# CONTINUE.md — instructions for an unattended session

You are a fresh Claude session started by `scripts/continue-agentflow.sh`,
probably on a schedule, with nobody watching. Read this file completely before
touching anything.

**You are not the session that wrote this.** You have no memory of the previous
run. Everything you need is in the repository: this file, `CLAUDE.md`,
`docs/roadmap.md`, `docs/adr/`, and `docs/milestones/`. Read them rather than
guessing.

## The one rule that matters

**Never guess.** If you cannot determine something from the repository, do not
invent it. Append the question to the `## Blocked on` section at the bottom of
this file, finish whatever else you can, commit, and stop. A wrong guess made
at 3am and committed is worse than an hour of waiting.

## What "done" means here

A milestone is not finished until *all* of this is true. Do not commit a
half-milestone and call it shipped.

1. `cd backend && uv run ruff format . && uv run ruff check . && uv run mypy .`
   — all clean.
2. `uv run pytest` — green, and the **97% coverage gate passes**. The gate is
   not negotiable; if coverage is short, write the missing tests rather than
   lowering it.
3. `uv run alembic check` — "No new upgrade operations detected." If the
   milestone added a migration, also rehearse `alembic downgrade -1` and
   `alembic upgrade head`, and confirm the schema afterwards.
4. **Verified at runtime**, not only in tests. Start `uvicorn` and, if the
   feature touches ingestion, `arq`; drive the real endpoints with `curl`.
   Tests that pass while the app does not start have happened in this project
   before.
5. An ADR in `docs/adr/` for each decision that was genuinely contested, and a
   milestone note in `docs/milestones/`, following the shape of the existing
   ones — including a **"Bugs this milestone found"** section. If you found no
   bugs, say so and be suspicious of yourself.
6. `CLAUDE.md` updated: the "Current position" block and a new row in the
   progress log.
7. One commit, conventional-commit subject (`feat(m7): …`), a body explaining
   *why*, and the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
   trailer. Commit only when 1–6 are all true.

## Rules specific to running unattended

- **Never `git push`.** The user reviews before anything leaves the machine.
- **Never `alembic downgrade base`**, never `DROP DATABASE`, never `docker
  compose down -v`. The development database holds data the user cares about.
- **One milestone per run.** When it is committed, stop. Do not start the next
  one, however much budget is left — a second milestone built on an unreviewed
  first one doubles the damage of a wrong turn.
- **Stop immediately if the working tree was dirty when you started.** The
  script checks this and refuses, but check again. Uncommitted work is someone
  else's in-progress thought.
- **Do not add dependencies casually.** Each one needs a line in
  `docs/packages.md` explaining why it earns its place. Note that specifiers
  here have no upper bounds, so any `uv lock` can pull a new *major* version of
  a tool — re-run the full check after adding anything, and read new errors as
  "the tool got stricter".
- **If the budget runs out mid-milestone**, that is fine and expected. Leave
  the work uncommitted, append a short note under `## Blocked on` saying exactly
  where you stopped, and exit. The next run reads it.

## Where the project is

`CLAUDE.md` is authoritative. As of the last update: **M1–M15 shipped**, next is
**M16** (production: Docker deploy, monitoring, Sentry, rate limiting, a security
hardening pass, load test). It is the last one.

**Mentor mode is suspended.** The user's words, verbatim, given right after M12
shipped: *"there is no mentor mode anymore, mentor mode will be after we get this
project working."* That replaced an earlier note in this file telling you to stop
and build nothing — read `CLAUDE.md` rather than a remembered summary of it.

So M13 → M16 are **autonomous**, to the same standard as M6–M12: full
`make check`, an ADR per genuinely contested decision, a milestone note with a
"Bugs this milestone found" section, runtime verification, one commit. The
"one milestone per run" rule above still applies.

The bar the user named is **"working"** — a product somebody can use, not a green
test suite. Where those two diverge, say so plainly in the milestone note.

## What is genuinely outstanding

Nothing is half-built. The tree is 123 implemented modules to 14 stubs. Four of
those are agent packages M15 deliberately did **not** build, each for a reason
rather than for lack of time: `evaluation/` and `memory/` are graphs that were
never needed (M8 put evaluation in a runner, M10 put extraction in a worker task
— neither has a branch to be a graph about), `research/` needs a web search tool
this environment cannot have, and `proposal/` is a template renderer nobody has
asked for. Building them to match the diagram in `docs/agents.md` would be the
exact failure that document warns about.

The rest belong to M16 (rate limiting, metrics, the audit log) plus
`app/integrations/google_drive/`, which M14 left alone because nothing in this
product reads a file from Drive.

Two things wait on the user rather than on code:

- **The architecture quiz and the bad-`POST /documents` exercise**, carried
  unanswered across six sessions. Listed under "Still pending" in `CLAUDE.md`.
- **API keys.** Every "not verified" note in the milestone docs traces to their
  absence: `refusal_accuracy` is 0.000 because the offline provider cannot refuse,
  retrieval is lexical rather than semantic, and Google is exercised against an
  in-memory authorization server rather than the real consent screen.

## House style — non-negotiable, and the reason this codebase reads as it does

- **Every module has a docstring saying what layer it is in and why it exists.**
  Every non-obvious decision has a comment explaining the *why*, not the what.
- **Comments name the failure mode.** "Sorted by index because the API may
  return results out of order, and a mis-ordered batch gives every chunk a
  neighbour's vector while raising nothing" — not "sort the results".
- **Test docstrings say what would break.** A test whose docstring restates its
  own assertion is dead weight.
- **When you are wrong about something, say so in the docs.** This repository
  contains several corrections of earlier confident claims, and they are the
  most valuable lines in it.
- Tenancy is checked in SQL, not only in Python. Enqueue only after commit
  (ADR-0008). Routes return schemas, never ORM objects.

## Blocked on

Nothing right now. Append dated entries here when you stop for a reason a
human has to resolve.

<!-- Format: - **YYYY-MM-DD** — the question, and what you did instead. -->
