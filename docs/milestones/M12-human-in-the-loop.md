# M12 — Human in the loop: the first agent that changes something

- **Date:** 2026-08-16
- **Status:** shipped
- **ADRs:** [ADR-0015](../adr/0015-an-approval-is-a-row-and-the-action-it-permits-is-stored-whole.md)

Every tool before this one read. M12 gives the agent one that writes to somebody's
diary — and then makes sure it cannot use it without being told to.

This is also the milestone that fills in `app/agents/`. Until now that package held
one implemented agent (`rag/`) and eight stubs; `calendar/` is the second.

## What was built

| Piece | File | What it does |
|---|---|---|
| `approvals` | `app/models/approval.py` | The request, the decision, and who made it |
| Migration | `alembic/versions/…041c27489c76_…` | The table and its enum |
| Calendar agent | `app/agents/calendar/{graph,tools}.py` | Propose → pause → execute |
| Pause / resume | `app/services/agent_service.py` | `checkpoint` + `PAUSED_FOR_APPROVAL`, finally written |
| Workflow | `app/services/approval_service.py` | Approve, reject, expire, cancel |
| Pricing | `app/llm/pricing.py` | `cost_usd` from measured tokens |
| Calendar write | `app/integrations/google_calendar/client.py` | `create_event`, and the wider scope |
| API | `app/api/v1/routes/approvals.py` | Propose, inbox, read, approve, reject |

## The shape

```
  propose graph:   plan ──► propose ──► END          run 1 → paused_for_approval
                               │
                       [ approvals row ]  ← a human decides, hours later
                               │
  execute graph:           execute ──► END           run 2 → succeeded
```

Two compiled graphs over one set of node functions. The pause is a graph reaching
`END`; the durability is `agent_runs.checkpoint`. **This is not LangGraph's
`interrupt()`** — stated plainly because the roadmap asks for "LangGraph interrupts",
and what it actually asks for is that the pause survive a restart. A checkpointer
would have been a second store of a fact the row already holds.

## The three ideas

**An approval is a row, not a pause.** The gap between asking and answering is hours,
so a deploy in that window is the expected case. Verified by killing the server: the
request was still in the inbox of a brand-new process, and approving resumed from the
checkpoint in Postgres.

**The action is stored whole, and it is what executes.** Not a plan id to be
re-derived — the same dict is written, displayed and executed, so "what was approved"
and "what ran" are identical by construction. Re-deriving would mean a user approved
a summary and something else ran.

**Proposing and executing are different functions.** `parse_event_request` touches
nothing; `build_create_event` is constructed only on the resume path. There is no `if
approved:` branch to get wrong — the effect is unreachable without going through the
row.

## Bugs this milestone found

Four, and the last two were invisible to the tests that should have caught them.

**1. The enum outlived its table in `downgrade()`.** Sixth milestone running (M2, M5,
M9, M10, M11, M12). Autogenerate has still never written that line.

**2. `add_steps` assumed one batch of steps per run.** M9 numbered them with
`enumerate`, which is correct for a single invocation and collides on
`uq_agent_steps_agent_run_id` the moment a run resumes and appends more. Caught by a
test; the assumption was invisible until a run had two halves.

**3. A rollback silently undid a human's decision.** `approve()` flushed the
`APPROVED` status and then resumed the run. When execution failed,
`AgentService._finish_failed` rolled the session back — as it must — and took the
decision with it, leaving the run `FAILED` and the approval `PENDING`: an inbox item
nobody could ever action, because resuming checks the run's status and would refuse
it forever. Caught by a test asserting the documented behaviour. Fixed by
*committing* the decision before executing — the same lesson M9 learned about run
rows, reached from the other direction.

**4. `None` assigned to a JSONB column stores JSON `null`, not SQL NULL.** Found by
querying Postgres, not through the ORM. SQLAlchemy reads both back as `None`, so the
test asserting a cancelled run's checkpoint was cleared *passed* while the database
held `'null'` and `checkpoint IS NULL` was false. NULL is what "nothing to resume"
means — an operator sweeping for stuck runs with `WHERE checkpoint IS NOT NULL` would
have found every cancelled run in the system. Fixed with `none_as_null=True`; the
regression test asserts in SQL, because the ORM cannot see the difference. Verified
by reverting the fix and watching the test fail.

## Verified at runtime

Real uvicorn, curl, and a real restart:

```
1. propose  → paused_for_approval
              "Create a calendar event 'a design review' on Thursday 20 August 2026 at 09:00 UTC"
              action: {kind: calendar.create_event, starts_at: 2026-08-20T09:00:00+00:00, …}
2. Postgres → status=paused_for_approval, checkpoint present
3. inbox    → one pending request
4. trace    → plan {understood: true} · propose {proposed: true}
5. "tomorrow afternoon" → succeeded, approval: null, and a message saying how to succeed
6. reject   → rejected; rejecting again → 409
7. Postgres → run cancelled, checkpoint NULL, error "The action was rejected."

   ——— server killed, process gone, new process started ———

8. inbox    → the *other* request still pending, in a brand-new process
9. approve  → resumed from the checkpoint; execution failed (no calendar connected)
10. Postgres → approval=approved, decided_by set, run=failed
               "No active google_calendar integration for this organization."
```

Step 8 is the milestone. Step 10 is bug 3's fix: the decision stood even though what
it authorised failed.

## Gate

```
ruff · ruff format · mypy --strict (232 files) · alembic check · make eval
713 tests, 2 skipped · 97.14% coverage (gate 97%)
```

`make eval` unchanged against M8's baseline — M12 touches no prompt and no retrieval
path.

## Known gaps, deliberately left

**The email half is not built.** `docs/roadmap.md` pairs "Calendar write" with "Email
draft/send behind approval". There is no Gmail integration to draft into, and building
one is M14's OAuth work — so `app/agents/email/` is still a stub. The approval
machinery is provider-agnostic: a second action kind is a `requested_action["kind"]`
and an executor.

**The parser is deterministic and strict.** It reads `YYYY-MM-DD HH:MM` and refuses
everything looser. "Next Tuesday afternoon" is exactly the judgement a model should
make, and there is no key here to make it — so it refuses rather than guessing,
because a half-understood date puts a meeting in a diary at the wrong time and raises
nothing.

**Nothing sweeps expired approvals on a timer.** `expire_overdue` exists and is
tested; no scheduler calls it. It matters less than it looks: `list_pending` filters
by the clock and deciding on an expired row is refused, so an unswept approval is
invisible and unactionable — merely untidy.

**Whether a model asks for approval at the right moments is untested**, and cannot be
tested here: the offline provider does not choose tools at all. The gate is in code
rather than in the model's judgement — which is the only arrangement that would be
safe even if that judgement *were* being tested.

**Existing Google connections must be reconnected.** The scope widened from
`calendar.readonly` to `calendar.events`, and Google issues tokens for the scopes
granted at consent time. A write on an M11-era credential returns 403, which the
client turns into "reconnect it to grant write access".

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099
```

```bash
curl -X POST localhost:8099/api/v1/agent-runs/calendar \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' \
  -d '{"instruction": "Schedule a design review on 2026-08-20 09:00"}'

curl localhost:8099/api/v1/approvals \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG"
```

Then stop the server, start it again, and list the inbox. The request is still there —
which is the whole point.
