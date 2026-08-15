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

`CLAUDE.md` is authoritative — read it, not this paragraph, for the current
position. As of the last update: **M1–M11 shipped**, next is **M12**.

The working mode is **autonomous through M12**, at the user's explicit
instruction: *"let us do that and just finsh m6-m12 without mentor mode"*.
Mentor mode resumes at M13.

## M12 — human-in-the-loop approvals

`docs/roadmap.md`, `docs/agents.md` and `docs/database.md` are the specification.
This is the **last milestone of the autonomous run** — mentor mode resumes at M13.
In outline:

- `approvals`: `agent_run_id FK, organization_id FK, requested_action jsonb,
  status (pending|approved|rejected|expired), decided_by FK users, decided_at`.
- A LangGraph **interrupt**, so a graph can stop mid-run and resume later.
- Google Calendar **write** and an email draft, both behind an approval. M11
  deliberately requested only `calendar.readonly`; the write scope is earned here,
  which means the connect flow's scope list changes and every already-connected
  user has to re-consent. Say so plainly in the milestone note.

**The key risk, named in the roadmap: an approval that is only an in-memory
interrupt does not survive a restart.** It must be a **row** as well. A deploy, a
crash or a scale-down happens between "please approve this" and the click, and the
work must still be there afterwards — otherwise the first restart silently drops
every pending action, and nobody finds out until a customer asks why the email
they approved never went.

**This is where two things from M9 finally get used**, and both are already in the
schema: `agent_runs.checkpoint` (LangGraph's serialised state — ADR-0012 explains
why it is stored and never published) and `RunStatus.PAUSED_FOR_APPROVAL`. If
either turns out to be the wrong shape, say so and change it; they were written
before anything needed them.

**It also owns pricing.** `agent_runs.cost_usd` is `Decimal(0)` everywhere,
deliberately, because a guessed rate would appear in reports and be trusted. M12 is
where real per-token pricing arrives — and where the number must be derived from
recorded token counts rather than estimated.

**Three things worth designing rather than discovering:**

- **An approval is a decision about a *specific* action, not a general
  permission.** Store the full `requested_action` — the actual email body, the
  actual event — so what was approved is what executes. Re-deriving the action at
  execution time means a user approved a summary and something else ran.
- **Approving twice must not execute twice.** The click arrives from a browser, and
  browsers retry. The status transition is the idempotency key.
- **Rejection and expiry are normal outcomes**, not errors. A run that ends
  `cancelled` because nobody approved it in time is working correctly.

**Before you finish: `make eval` must still pass.**

**What you can and cannot verify without a key:** the approval rows, the
interrupt-and-resume across a process restart, the idempotency, the expiry sweep and
the tenancy — all testable offline. Whether a *model* asks for approval at the right
moments is not testable here, because the offline provider does not choose tools at
all (ADR-0012 is explicit that this is not a tool-calling ReAct loop). Say so
plainly, and gate the side effect in code rather than relying on the model to ask.

## After M12

**Mentor mode resumes at M13** (`CLAUDE.md`, "The mentorship contract"). Do not
build M13 autonomously. There are also two long-deferred items to raise when the
user next engages directly: the architecture quiz and the bad-`POST /documents`
exercise, both listed under "Still pending" in `CLAUDE.md`.

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
