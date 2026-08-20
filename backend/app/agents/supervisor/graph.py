"""The supervisor: one entry point, and a decision recorded before anything runs.

Layer: agents. Invoked through `services/agent_service.py`, never from a route.

    classify ──► plan ──► END
        │          │
        │          └── an ordered list of agents
        └── which specialist, or none

**What the supervisor is not.** It does not execute anything. It reads an
instruction and writes down what should happen; `AgentService.run_supervisor`
then calls the existing agents, each of which opens its own run. That split is
deliberate and it is what keeps `docs/agents.md` rule 2 true — "specialists never
call each other directly; the call graph stays a tree". A supervisor that
executed would need a session, a provider registry and an approval service, and
would become the thing every agent depends on rather than the thing that orders
them.

It also means the routing decision is **traced before any work happens**. The two
nodes exist so the trace records what was *understood* apart from what was
*sequenced* — the same reason `plan` and `propose` are separate in the calendar
agent. A run that did the wrong thing and a run that understood the wrong thing
look identical in a one-node trace, and they need different fixes.

Why this milestone was allowed to happen at all
------------------------------------------------
`docs/agents.md`: "the supervisor and specialists arrive only when a single agent
measurably fails at the breadth of tasks. Premature multi-agent architecture is
the most common failure mode in this space."

That is a precondition with a number attached, and the number is in
`app/evaluation/data/routing.json` and its committed baseline: over twenty
hand-written instructions, the single-agent world scores **0.300** and the
supervisor scores **1.000**. Both are re-measured on every `make eval`, so the
justification for this package stays checkable rather than becoming folklore.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.supervisor.tools import Router

CLASSIFY = "classify"
PLAN = "plan"


class SupervisorState(TypedDict, total=False):
    """What flows through the supervisor graph.

    JSON-serialisable throughout, like every state in this package — ids are
    `str`, never `uuid.UUID`. Nothing checkpoints a supervisor run today (the
    decision is cheap to recompute and nothing pauses here), and the constraint is
    kept anyway: the moment one of these states is written to `agent_runs.
    checkpoint`, a `UUID` in it becomes a serialisation failure in a code path
    nobody tested.
    """

    instruction: str
    organization_id: str
    user_id: str | None

    agent: str | None
    """Which specialist should act, or None when none should."""

    plan: list[str]
    """The agents to run, in order. Empty when the answer is "nothing"."""

    reason: str
    """Why — written into the trace, so "why was my question refused?" is
    answerable from the run rather than by reading a regex."""


StepRecorder = Callable[[dict[str, Any]], None]
SupervisorGraph = CompiledStateGraph[SupervisorState, Any, SupervisorState, SupervisorState]


def build_graph(router: Router, record: StepRecorder) -> SupervisorGraph:
    """Classify an instruction and write down the plan. Executes nothing.

    The router is injected rather than constructed here, for the same reason the
    RAG graph takes its tools: this module decides *when* a decision is made and
    what is recorded about it, and `tools.py` decides how the decision is reached.
    It is also what lets the evaluation harness score a router without building a
    graph at all.
    """

    async def classify(state: SupervisorState) -> SupervisorState:
        """Decide who should act. Touches nothing."""
        started = time.perf_counter()
        decision = router.route(state["instruction"])

        record(
            {
                "node_name": CLASSIFY,
                "tool_name": None,
                "tool_input": {"instruction": state["instruction"]},
                "tool_output": {"agent": decision.agent, "reason": decision.reason},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )
        return {"agent": decision.agent, "plan": list(decision.plan), "reason": decision.reason}

    async def plan(state: SupervisorState) -> SupervisorState:
        """Record the sequence, separately from the choice that produced it."""
        started = time.perf_counter()
        steps = state.get("plan", [])

        record(
            {
                "node_name": PLAN,
                "tool_name": None,
                "tool_input": {},
                # The whole plan, not its length. "What was it going to do?" is the
                # first question about a run that did the wrong thing, and a count
                # cannot answer it.
                "tool_output": {"plan": steps, "steps": len(steps)},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )
        return {}

    graph: StateGraph[SupervisorState, Any, SupervisorState, SupervisorState] = StateGraph(
        SupervisorState
    )
    graph.add_node(CLASSIFY, classify)
    graph.add_node(PLAN, plan)
    graph.add_edge(START, CLASSIFY)
    graph.add_edge(CLASSIFY, PLAN)
    graph.add_edge(PLAN, END)

    return graph.compile()


def initial_state(
    *, instruction: str, organization_id: uuid.UUID, user_id: uuid.UUID | None
) -> SupervisorState:
    """Seed state, with ids stringified at the boundary rather than at use."""
    return SupervisorState(
        instruction=instruction,
        organization_id=str(organization_id),
        user_id=str(user_id) if user_id else None,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
