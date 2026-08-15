# ADR-0015: An approval is a row, and the action it permits is stored whole

- **Status:** accepted
- **Date:** 2026-08-16
- **Milestone:** M12

## Context

Every tool the agent has had until now *read*. `search_chunks` (M9) returns
passages, recall returns memories, `list_events` (M11) reads a calendar. Nothing an
agent did could be observed from outside this system.

M12 gives it a tool that writes to somebody's diary. Two problems arrive with that,
and the roadmap names the first: *"an approval that is only an in-memory interrupt
does not survive a restart. It must be a row as well."*

**The gap between asking and answering is hours.** Lunch, a meeting, a weekend. A
deploy or a crash in that window is not an edge case; over any real period it is the
expected case.

**What a human approves and what later executes can drift apart.** They are produced
at different times, by code that may have changed in between.

## Decision

**The pause is a graph reaching `END`; the durability is a row.** M9 added
`agent_runs.checkpoint` and `RunStatus.PAUSED_FOR_APPROVAL` and deliberately left
both unwritten. This is what they were for. The propose graph stops, its state is
written to the checkpoint column, and resuming means invoking a *second* compiled
graph with the state read back out.

**Two graphs over shared node functions, not one graph with a checkpointer.** Once
the state is in a row, a LangGraph checkpointer would be a second store of the same
fact — two things to keep consistent, plus a dependency on
`langgraph-checkpoint-postgres` to hold the copy that matters less.

Stated plainly, because the roadmap says "LangGraph interrupts": this is **not**
`interrupt()` with a checkpointer. It is a graph that stops and a row that
remembers, which is the property the roadmap actually asks for.

**The requested action is stored whole, and it is what executes.** Not a plan id,
not a tool name plus arguments to be re-derived. The dict written to
`approvals.requested_action` is the dict shown to the human and the dict handed to
the executor — the same object all three times, so "what was approved" and "what
ran" are identical by construction rather than by care.

**The summary a human reads is rendered from that action by code.** A model-written
description would be a second account that might not match, and the person clicking
approve reads the sentence, not the JSON.

**Proposing and executing are different functions, and only one is reachable
freely.** `parse_event_request` touches nothing. `build_create_event` is constructed
only on the resume path. The alternative — one tool with `if approved:` inside — puts
the decision and the effect in the same function, where a refactor or a second caller
can separate them.

**The decision is committed before the side effect runs.** Not flushed: committed. A
failure afterwards must leave a record saying a human authorised this and the attempt
failed.

**The status transition is the idempotency key.** Only a pending approval can be
decided; deciding twice is a 409. The click arrives from a browser, and browsers
retry.

**Expiry is a safety property.** The action was composed against facts true at the
time. `list_pending` filters by the clock and `_decidable` refuses an expired row, so
an unswept approval is invisible and unactionable rather than dangerous.

**Rejection cancels the run.** `RunStatus.CANCELLED` gets its first writer here. A
run left `paused_for_approval` after a decision would still satisfy the resume guard,
and be genuinely resumable by anyone who found it.

## Consequences

**Pricing arrives, and still refuses to guess.** M9 wrote that "a guessed rate would
appear in reports, get trusted, and be wrong". M12 owns pricing and keeps that:
`app/llm/pricing.py` ships the arithmetic and `Settings` holds the rates, defaulting
to zero. A `cost_usd` of `0.000000` now means "nobody has told this system what it
pays" rather than "not built yet" — and the docstrings say so.

**The calendar scope widened, and existing users must reconnect.** M11 requested
`calendar.readonly` precisely so a write scope would not sit unused for a milestone.
M12 earns it. Google issues tokens for the scopes granted at consent time, so every
account connected under M11 keeps a read-only credential; the write fails with 403,
which `post_json` turns into "reconnect it to grant write access". That is a real
migration cost, and the correct one to pay.

**`add_steps` had to learn that a run can have two batches of steps.** M9 numbered
steps with `enumerate`, which is right for one invocation and collides on
`uq_agent_steps_agent_run_id` the moment a run resumes and appends more. Caught by a
test. The assumption was invisible until a run had two halves.

**Assigning `None` to a JSONB column writes JSON `null`, not SQL NULL.** Found by
looking at Postgres, not through the ORM — SQLAlchemy reads both back as `None`, so
every ORM assertion passed while `checkpoint IS NULL` was false. NULL is what
"nothing to resume" *means*, so an operator sweeping for stuck runs with `WHERE
checkpoint IS NOT NULL` would have found every cancelled run in the system. Fixed
with `none_as_null=True`, and the regression test asserts in SQL because the ORM
cannot see the difference.

**The email half of the milestone is not built, and that is stated rather than
implied.** `docs/roadmap.md` pairs "Calendar write" with "Email draft/send behind
approval". There is no Gmail integration to draft into — building one is M14's OAuth
work — so `app/agents/email/` remains a stub. The approval machinery is
provider-agnostic; adding the second action kind is a `requested_action["kind"]` and
an executor.

**What is verified and what is not.** The rows, the pause, the resume across a
genuinely restarted process, the idempotency, the expiry, the cancellation, the
pricing arithmetic and the tenancy are all exercised. What is *not* verified is
whether a model asks for approval at sensible moments — the offline provider does not
choose tools at all (ADR-0012), and the parser here is deterministic. The gate is in
code rather than in the model's judgement, which is the only arrangement that would
be safe even if that judgement were being tested.
