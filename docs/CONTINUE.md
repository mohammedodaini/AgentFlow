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
position. As of the last update: **M1–M7 shipped**, next is **M8**.

The working mode is **autonomous through M12**, at the user's explicit
instruction: *"let us do that and just finsh m6-m12 without mentor mode"*.
Mentor mode resumes at M13.

## M8 — RAG evaluation

`docs/roadmap.md` is the specification. In outline:

- A golden set: questions, the chunk(s) that should be retrieved, and a
  reference answer. Small and hand-written beats large and generated — twenty
  questions someone actually cares about will find more than two hundred
  paraphrases of each other.
- Retrieval metrics first (recall@k, MRR), because they are cheap,
  deterministic, and they bound everything downstream.
- Answer scoring second, LLM-as-judge. **This is the part that needs a key**,
  so build it behind the same seam and give it a deterministic offline judge
  for the tests.
- Then, and only then, tune what M6 and M7 left as guesses: `chunk_size_tokens`
  (400), `chunk_overlap_tokens` (60), `retrieval_top_k` (5),
  `context_token_budget` (8000), and `DEFAULT_MIN_SCORE` (0.0, no floor). Each
  of those is documented as "a starting point, not a finding" — replace those
  sentences with numbers and the measurement that produced them.
- `app/evaluation/` already exists as stubs. It is currently in the coverage
  omit list in `pyproject.toml`; remove its entry as you implement, or
  `tests/unit/test_stub_manifest.py` will fail — which is what it is for.

**What you can and cannot verify without a key:** the harness, the metrics
arithmetic, the golden-set loading, the report — all testable offline against
the hashing embedder. Whether the *numbers* mean anything about a real
embedding model — not testable. Say so plainly.

## M9–M12, in order

None of these need an API key to build, and all of them need one to *evaluate*.
Same honesty rule: verify the plumbing, state the limit.

| Milestone | What it is | The key risk |
|---|---|---|
| M9 | The single agent: LangGraph, tools, `search_chunks` | Building multi-agent before single-agent works. Refuse |
| M10 | Memory: conversation + long-term, `app/memory/` | Unbounded context growth; summarisation must be tested |
| M11 | Human approval: a DB record **and** a LangGraph interrupt | An approval that is only an interrupt does not survive a restart |
| M12 | Observability: traces, token/cost accounting per run | Metrics nobody reads. Instrument what a bill or an incident needs |

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
