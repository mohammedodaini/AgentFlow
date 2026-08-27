# ADR-0018: Routing by capability, and a precondition that had to be measured

- **Status:** accepted
- **Date:** 2026-08-21
- **Milestone:** M15

## Context

`docs/agents.md` has said since the first week that the supervisor arrives "only
when a single agent measurably fails at the breadth of tasks", and adds:
"premature multi-agent architecture is the most common failure mode in this
space." `docs/roadmap.md` repeats the condition, *only where the single agent
measurably falls short*.

"Measurably" is a word with an obligation attached, and the honest reading is
that M15 could not begin by writing a supervisor. It had to begin by finding out
whether one was warranted.

The shortfall was real and easy to state. By M14 there were three agents behind
three endpoints: `/agent-runs` answered questions, `/agent-runs/calendar`
proposed meetings, `/agent-runs/email` proposed messages. **The human was the
router.** They had to know the product's internal structure before they could use
it, and no single entry point could serve "find our expenses policy and email it
to ada" at all.

## Decision

**Measure first, and commit the measurement.** `app/evaluation/data/routing.json`
is twenty hand-written instructions, questions, meetings, messages, two
multi-step requests, and five that no agent should take: each labelled with the
agent that should receive it. `routing_runner.py` scores two routers on every
run: the one under test, and `SingleAgentRouter`, which is the M9–M14 world where
everything went to the RAG agent.

Both numbers are in the committed baseline, permanently:

| | routing_accuracy |
|---|---|
| Single agent (M9–M14) | **0.300** |
| Supervisor (M15) | **1.000** |

A year from now "was the supervisor worth building?" has an answer in the
repository rather than in somebody's memory, and a change that makes routing no
better than doing nothing fails the gate. `make eval` runs it first, because it
needs no database, no ingestion and no model: a run that will fail on routing
should say so before spending a minute embedding a corpus.

**Route by capability, not by keywords.** The obvious router is a keyword table.
It is wrong within a day, and the golden set holds the example that shows why:

    Email ada@example.com about the review on 2026-09-10 09:00 saying please confirm

An address *and* a date. A router scoring keywords independently sees a calendar
instruction, books a meeting, and never sends the message: an unrequested side
effect instead of the requested one. Worse, the two tables drift: the calendar
parser gets stricter, the router's idea of a calendar instruction does not, and
the router starts handing work to an agent that will refuse it.

So the router asks each specialist's **own parser**, `parse_draft_request`
`parse_event_request`, the same functions that would run if the work were routed
there. A specialist that cannot parse an instruction cannot be given it, by
construction, and the router cannot hold an opinion that disagrees with the agent
it routes to. Ties break by specificity: email first, because its parser demands
an address *and* a body, while a date can appear in any sentence.

**A router must be able to answer "nobody".** Five of the twenty examples are
unroutable, and `load_routing_dataset` refuses a dataset without them. A router
forced to choose among agents always chooses one: hand it "order me a taxi" and
it picks whichever specialist scored least badly, then produces a confident
failure. The refusal names what the product *can* do, because "I cannot help with
that" invites the user to rephrase the same impossible request.

**Adding an entry point must not add a capability.** The supervisor classifies and
delegates to the same methods a client could call directly. A calendar or email
action still reaches a provider only through an `approvals` row a human decided
on. `test_routing_adds_no_capability` sends the instruction that would cause a
side effect and asserts nothing reached a provider.

**The supervisor executes nothing itself.** It writes down what should happen;
`SupervisorService` then calls the specialists, each opening its own run. That
keeps the call graph a tree (`docs/agents.md` rule 2), and it means the routing
decision is traced *before* any work happens, two nodes, `classify` and `plan`
so a run that did the wrong thing and a run that understood the wrong thing are
distinguishable. They need different fixes.

**The planner is a function, not a graph.** The stub said
`build_graph() -> StateGraph` and every other agent here is one. A graph earns
its keep when there are branches to trace, cycles to bound or state to
checkpoint; planning has none. It is invoked as a node inside the supervisor's
graph, which is what `docs/agents.md` rule 1 actually asks for, without
pretending a pure function is a state machine.

## Consequences

**A paused run is only half a mechanism, and the first version shipped the other
half missing.** `AgentService.run_calendar_agent` pauses a run and *returns* the
action; `ApprovalService.propose_calendar_action` is what turns that action into
the row a human decides on. The supervisor originally called the agent directly,
so every supervised side effect produced a run stuck in `PAUSED_FOR_APPROVAL`
with **nothing in anybody's inbox**, unresumable, because resuming requires a
decided approval.

Every test passed. They asserted the delegated run's *status*, which was exactly
right, and never asked whether the thing that makes that status actionable
existed. Postgres answered in one query: three paused runs, zero approvals.

The fix is structural rather than careful. `SupervisorService` composes
`AgentService` and `ApprovalService` and routes side effects through the one that
writes rows, so the broken path is not merely unused: it is unreachable. This is
M12's lesson from a third direction, and the response schema changed with it: the
endpoint returns the **approval row**, not just the action it permits, because a
paused run with no row behind it looked identical to a working one.

**The dependency direction forced a third service.** `ApprovalService` already
imports `AgentService`, so `AgentService` cannot reach back. A supervisor cannot
live in `ApprovalService` either: it answers questions, which have nothing to do
with approvals. Something that composes both is what the arrows were always going
to require.

**Two agent-service helpers became public.** `reload` and `finish_failed` now
have a second caller, which makes them part of that service's API rather than its
internals.

**The stub-manifest canary fired, exactly as its own comment predicted.** M9 moved
it onto `app/agents/supervisor/graph.py`, reasoning that M15 was "the furthest
away". M15 arrived. It now points at `app/agents/proposal/graph.py`, which no
milestone claims: a canary aimed at scheduled work has an expiry date.

**Four agent packages stay stubs, each for a reason rather than for lack of
time.** `evaluation/` and `memory/` are graphs that were never needed, M8 put
evaluation in a runner and M10 put memory extraction in a worker task, both
deliberately, because neither has a branch to be a graph about. `research/` needs
a web search tool this environment cannot have. `proposal/` is a template renderer
nobody has asked for. Building them to match a diagram would be the exact failure
`docs/agents.md` warns about.

## What is not verified

**The router is rules, and the ceiling is real.** It cannot tell "book me
something next Tuesday afternoon" from noise, because neither can the parsers it
consults. That is the same limit ADR-0010 records for every model-shaped decision
in this project: there is no API key here, and `Router` is a protocol precisely
so a model-backed implementation can be measured against the same twenty examples
without touching the graph.

**Twenty hand-written examples is a floor, not a proof.** A perfect score means no
example regressed, not that routing is solved. The set was written by the same
person who wrote the rules, which is the weakness of every golden set and the
reason the unroutable examples matter more than the routable ones.

**The two-step ceiling is deliberate.** Every multi-step request this product can
express is "look something up, then act on it". An unbounded planner with no model
behind it would be a rule table pretending to be reasoning.
