# M15 — Multi-agent: the measurement first, then the supervisor

- **Date:** 2026-08-21
- **Status:** shipped
- **ADRs:** [ADR-0018](../adr/0018-routing-by-capability-and-a-measured-precondition.md)

`docs/roadmap.md` asks for "supervisor + planner + specialists, **only where the
single agent measurably falls short**", and `docs/agents.md` is blunter:
"premature multi-agent architecture is the most common failure mode in this
space."

So M15 did not begin by writing a supervisor. It began by finding out whether one
was warranted, and the answer is a committed number rather than a paragraph.

| | routing_accuracy |
|---|---|
| Single agent (the M9–M14 world) | **0.300** |
| Supervisor (M15) | **1.000** |

Both are in `app/evaluation/baselines/routing.json`, re-measured by every
`make eval`. A change that makes routing no better than doing nothing fails the
gate, and "was the supervisor worth building?" stays answerable from the
repository.

## What was built

| Piece | Files | What it does |
|---|---|---|
| Routing golden set | `app/evaluation/data/routing.json` | 20 instructions, 5 unroutable, 2 multi-step |
| Routing dataset | `app/evaluation/routing.py` | Loads it, and refuses one that would score meaninglessly |
| Routing gate | `app/evaluation/routing_runner.py` | Scores a router *and* the single-agent control |
| Router | `app/agents/supervisor/tools.py` | `Router` protocol + `RuleRouter` |
| Planner | `app/agents/planner/graph.py` | Decomposes into an ordered plan |
| Supervisor graph | `app/agents/supervisor/graph.py` | `classify → plan`, traced, executes nothing |
| Orchestration | `app/services/supervisor_service.py` | Delegates, and records what needs permission |
| API | `POST /agent-runs/supervised` | One endpoint for anything |
| Frontend | `frontend/src/app/(app)/approvals/page.tsx` | One box instead of one form per agent |

**No migration.** `agent_runs.agent_name` is a string precisely so a new agent is
a deploy rather than an `ALTER TYPE` (M9's decision, paying out for the second
time).

## The shortfall, stated plainly

By M14 there were three agents behind three endpoints: `/agent-runs` answered
questions, `/agent-runs/calendar` proposed meetings, `/agent-runs/email` proposed
messages. **The human was the router.** They had to learn the product's internal
structure before they could use it, and no single entry point could serve "find
our expenses policy and email it to ada" at all.

## Routing by capability, not by keywords

The obvious router is a keyword table. It is wrong within a day, and the golden
set contains the example that shows why:

```
Email ada@example.com about the review on 2026-09-10 09:00 saying please confirm
```

An address *and* a date. A router scoring keywords independently sees a calendar
instruction, books a meeting, and never sends the message the user asked for — an
unrequested side effect instead of the requested one. Worse, the two tables
drift: the calendar parser gets stricter, the router's idea of a calendar
instruction does not, and the router starts handing work to an agent that will
refuse it.

So the router asks each specialist's **own parser** — `parse_draft_request` and
`parse_event_request`, the same functions that would run if the work were routed
there. A specialist that cannot parse an instruction cannot be given it, by
construction. Ties break by specificity: email first, because its parser demands
an address *and* a body, while a date can appear in any sentence.

**A router must be able to answer "nobody".** Five of twenty examples are
unroutable, and `load_routing_dataset` refuses a dataset without them — a router
forced to choose among agents always chooses one, and "order me a taxi" comes
back as a confident failure from whichever specialist scored least badly.

## Bugs this milestone found

Four, and the first was found by running the product rather than by a test.

**1. Every supervised side effect paused with no approval behind it.**
`AgentService.run_calendar_agent` pauses a run and *returns* the action; only
`ApprovalService.propose_calendar_action` turns that action into the row a human
decides on. The first supervisor called the agent directly, so a supervised
"schedule a review" produced a run stuck in `PAUSED_FOR_APPROVAL` with **nothing
in anybody's inbox** — unresumable, because resuming requires a decided approval.

Every test passed. They asserted the delegated run's *status*, which was exactly
right, and never asked whether the thing that makes that status actionable
existed. Postgres answered in one query:

```
 calendar | paused_for_approval | 0
 email    | paused_for_approval | 0
 email    | paused_for_approval | 0
```

Fixed structurally: `SupervisorService` composes `AgentService` and
`ApprovalService` and routes side effects through the one that writes rows, so
the broken path is unreachable rather than merely unused. The response schema
changed with it — the endpoint returns the **approval row**, not just the action,
because a paused run with no row behind it looked identical to a working one.
Reverting the fix now fails three tests.

**2. The planner and the router disagreed about what a question was.** The
planner owned a `LOOKUP_LEAD` regex that knew "what does" but not "how"; the
router owned a second one that knew both. "How are expenses reimbursed?" was a
question to one and not to the other, and fell through to a refusal — three of
twenty golden examples failed in the gap. One judgement, one owner: the router
decides whether something is a question, the planner decides the order.

**3. The stub-manifest canary fired, exactly as its own comment predicted.** M9
moved it onto `app/agents/supervisor/graph.py` reasoning that M15 was "the
furthest away". M15 arrived. It now points at `app/agents/proposal/graph.py`,
which no milestone claims — a canary aimed at scheduled work has an expiry date.

**4. Every *absence* check in the browser smoke test was scanning the RSC
payload.** `page.textContent("body")` includes the contents of `<script>` tags,
which on an App Router page means the entire serialised React payload — so
`!body.includes(x)` was asserting about strings that were nowhere on screen. It
surfaced when M15's new placeholder happened to contain a date one assertion
looked for. The absence checks now use `innerText`. The one deliberate exception
is "checkpoint is never published", which *should* scan the payload: a leak into
the payload is still a leak.

## Verified at runtime

Real uvicorn, real Postgres, one endpoint, seven instructions:

```
How are expenses reimbursed?                          → rag       [rag]            succeeded
Schedule a design review on 2026-09-10 09:00          → calendar  [calendar]       paused_for_approval
Email ada@… about Q3 saying the report is ready       → email     [email]          paused_for_approval
Email ada@… about the review on 2026-09-10 09:00 …    → email     [email]          paused_for_approval
Find our expenses policy and email it to ada@…        → email     [rag, email]     paused_for_approval
Order me a taxi to the airport                        → refused   []               "I can answer questions about…"
hello                                                 → refused   []               "There is nothing here to act on."

trace:    classify {"agent": "calendar", "reason": "calendar can perform this directly."}
          plan     {"plan": ["calendar"], "steps": 1}

inbox:    5 pending, and rejecting one returns 200 rejected
```

Row four is the ambiguity case, routing to email despite containing a date. Row
five is the two-step plan. The inbox line is bug 1's fix.

**In a real browser**, `make smoke` — 23 checks, 23 passing, including two new
ones asserting that an impossible request is refused rather than queued and that
the refusal names what the product can do.

## Gate

```
ruff · ruff format · mypy --strict (246 files) · alembic check
885 tests, 2 skipped · 97.13% coverage (gate 97%)
make eval: handbook unchanged · routing 1.000 vs 0.300 control
frontend: lint · typecheck · build (10 routes) · smoke 23/23
```

The routing gate was verified to fail: planting an unbeatable baseline exits 1.

## Known gaps, deliberately left

**The router is rules, and the ceiling is real.** It cannot tell "book me
something next Tuesday afternoon" from noise, because neither can the parsers it
consults — the same limit ADR-0010 records for every model-shaped decision here.
`Router` is a protocol so a model-backed implementation can be scored against the
same twenty examples without touching the graph.

**Twenty hand-written examples is a floor, not a proof.** A perfect score means no
example regressed. The set was written by the same person who wrote the rules,
which is the weakness of every golden set — and the reason the unroutable
examples matter more than the routable ones.

**Two steps is the ceiling.** Every multi-step request this product can express is
"look something up, then act on it". An unbounded planner with no model behind it
would be a rule table pretending to be reasoning.

**Four agent packages remain stubs, each for a reason.** `evaluation/` and
`memory/` are graphs that were never needed — M8 put evaluation in a runner and
M10 put memory extraction in a worker task, both deliberately, because neither
has a branch to be a graph about. `research/` needs a web search tool this
environment cannot have. `proposal/` is a template renderer nobody has asked for.
Building them to match the diagram in `docs/agents.md` would be the exact failure
that document warns about.

**The supervisor does not use memory.** `docs/agents.md` shows `recall_memory()`
as a supervisor tool. The RAG agent already recalls (M10) and the supervisor
routes on one instruction, so there is nothing for it to remember yet. When
routing depends on context — "send it to her too" — that is the seam to widen.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app --port 8000
```

```bash
curl -X POST localhost:8000/api/v1/agent-runs/supervised \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' \
  -d '{"instruction": "Find our expenses policy and email it to ada@example.com about expenses saying here it is"}'
```

The response carries two runs — the supervisor's decision and the specialist's
work — plus the approval row somebody now has to decide on.
