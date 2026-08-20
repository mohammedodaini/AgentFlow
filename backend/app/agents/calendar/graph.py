"""The calendar agent: a graph that stops, and a row that remembers it stopped.

Layer: agents. Invoked through `services/agent_service.py`, never from a route.

The shape
---------
    propose graph:   plan ──► propose ──► END        (run 1, ends PAUSED)
                                  │
                          [ approvals row ]
                                  │
    execute graph:  app/agents/execution.py          (run 2, after a human decides)

**The execute half no longer lives here.** M12 wrote it in this module, which was
right while calendar was the only kind of approved action. M14 added a second
(`email.send_draft`) and the two were identical — take the approved dict, call the
one function that performs it, record what happened — so it moved to
`app/agents/execution.py`. What stays is the part that is genuinely about
calendars.

**Two compiled graphs over one set of node functions, not one graph with a
checkpointer.** That is the load-bearing decision in this module, so it is worth
being exact about why.

LangGraph can pause a graph in-process and resume it. That pause lives in memory,
and the gap between "please approve this" and somebody clicking is hours — lunch, a
meeting, a weekend. A deploy or a crash in that window is not an edge case, it is
the expected case. Any durable design therefore needs the state written somewhere
that survives a process, and `agent_runs.checkpoint` already exists for exactly
this (M9 added the column and deliberately left it unwritten).

Once the state is in a row, a LangGraph checkpointer would be a *second* store of
the same fact — two things to keep consistent, and a dependency on
`langgraph-checkpoint-postgres` to hold the copy that matters less. So the pause is
"this graph reached END with an approval outstanding", the durability is the row,
and resuming is invoking the second graph with the persisted state.

Stated plainly, because the roadmap says "LangGraph interrupts": this is **not**
`interrupt()` with a checkpointer. It is a graph that stops and a row that
remembers — which is the property the roadmap actually asks for ("an approval that
is only an in-memory interrupt does not survive a restart; it must be a row as
well"). Nothing here pretends the framework is doing the remembering.

Why `plan` and `propose` are separate nodes
-------------------------------------------
They could be one function. Keeping them apart means the trace records *what was
understood* separately from *what was requested*, and those fail differently: a run
that proposed nothing because it could not parse a date looks identical, in a
one-node trace, to one that parsed a date nobody wanted.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.calendar.tools import describe, parse_event_request

logger = structlog.get_logger(__name__)

PLAN = "plan"
PROPOSE = "propose"
EXECUTE = "execute"

NOTHING_TO_PROPOSE = (
    "I could not work out when you wanted that. Give me a date and time like "
    "2026-08-20 09:00 and I will draft it for you."
)
"""What the run says when the instruction could not be parsed.

A refusal that tells the user how to succeed, rather than a generic failure. The
parser is deliberately strict (see `tools.py`), so this is a *common* outcome
rather than an exceptional one — which makes the wording part of the feature.
"""


class CalendarState(TypedDict, total=False):
    """What flows through the calendar graph.

    **Every field is JSON-serialisable, and that is a hard requirement rather than
    a style choice.** This state is written to `agent_runs.checkpoint` as JSONB and
    read back by a *different process* after a human decides. Ids are therefore
    `str`, not `uuid.UUID`, and times are ISO strings — M9 learned the same lesson
    for `retrieved` and wrote it into `AgentState`; here it is not merely tidy, it
    is what makes resuming possible at all.

    A separate state from `AgentState` because the two graphs share nothing: this
    one has no question, no retrieval, no citations. `docs/agents.md` rule 1 says
    agents communicate through a typed state object *within a run*, and these are
    different runs of different agents.
    """

    instruction: str
    organization_id: str
    user_id: str | None

    proposed_action: dict[str, Any] | None
    """The action a human will be asked to permit, or None if none was understood.

    The same dict that gets stored, displayed and executed — see `tools.py`.
    """

    summary: str
    """The one line the human reads. Rendered from the action by code."""

    refusal: str
    """Set when nothing could be proposed. Present rather than implied by
    `proposed_action is None`, so the trace and the run output can say *why*."""

    result: dict[str, Any]
    """What executing actually did — the created event's id and URL. Only ever set
    on the resume path."""


StepRecorder = Callable[[dict[str, Any]], None]
"""Called once per node with what that node did. The same contract as the RAG
graph's (M9): our semantics, not the framework's."""

CalendarGraph = CompiledStateGraph[CalendarState, Any, CalendarState, CalendarState]


def build_propose_graph(record: StepRecorder) -> CalendarGraph:
    """The first half: understand the instruction, propose an action, stop.

    It reaches `END` whether or not it proposed anything. A run that understood
    nothing is finished, not paused — there is nothing for a human to decide on, and
    creating an empty approval so the shapes match would put a row in somebody's
    inbox asking them to permit nothing.
    """

    async def plan(state: CalendarState) -> CalendarState:
        """Read the instruction. Touches nothing."""
        started = time.perf_counter()
        action = parse_event_request(state["instruction"])

        record(
            {
                "node_name": PLAN,
                "tool_name": None,
                "tool_input": {"instruction": state["instruction"]},
                "tool_output": {"understood": action is not None},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )

        if action is None:
            return {"proposed_action": None, "refusal": NOTHING_TO_PROPOSE}

        return {"proposed_action": action}

    async def propose(state: CalendarState) -> CalendarState:
        """Render the action for a human. Still touches nothing."""
        started = time.perf_counter()
        action = state.get("proposed_action")

        if action is None:
            record(
                {
                    "node_name": PROPOSE,
                    "tool_name": None,
                    "tool_input": {},
                    "tool_output": {"proposed": False},
                    "latency_ms": _elapsed_ms(started),
                    "tokens": 0,
                }
            )
            return {}

        summary = describe(action)

        record(
            {
                "node_name": PROPOSE,
                "tool_name": None,
                # The whole action goes in the trace, unlike retrieval, where only
                # the shape of the result is stored. It is small, and "what exactly
                # were they asked to approve?" is the first question anyone will
                # have about a side effect that turned out to be wrong.
                "tool_input": action,
                "tool_output": {"proposed": True, "summary": summary},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )
        return {"summary": summary}

    graph: StateGraph[CalendarState, Any, CalendarState, CalendarState] = StateGraph(CalendarState)
    graph.add_node(PLAN, plan)
    graph.add_node(PROPOSE, propose)
    graph.add_edge(START, PLAN)
    graph.add_edge(PLAN, PROPOSE)
    graph.add_edge(PROPOSE, END)

    return graph.compile()


def checkpoint_of(state: CalendarState) -> dict[str, Any]:
    """The subset of state worth persisting across the pause.

    A whitelist rather than the whole dict, for the same reason response schemas are
    whitelists: whatever a node adds later should not silently become part of the
    durable contract that a resume — possibly running a newer version of this code —
    has to understand.

    Its counterpart `state_from_checkpoint` was deleted at M14. It existed to rebuild
    a full `CalendarState` for the execute graph, and that graph now runs on
    `ExecutionState` — which holds the approved action and nothing else. The rule it
    was written to enforce did not go with it: the tenant still comes from the
    request rather than from the checkpoint, and `AgentService.resume_approved_run`
    is where that is now visible.
    """
    return {
        "instruction": state.get("instruction", ""),
        "organization_id": state.get("organization_id", ""),
        "user_id": state.get("user_id"),
        "proposed_action": state.get("proposed_action"),
        "summary": state.get("summary", ""),
    }


def initial_state(
    *, instruction: str, organization_id: uuid.UUID, user_id: uuid.UUID | None
) -> CalendarState:
    """Seed state, with ids stringified at the boundary rather than at use."""
    return CalendarState(
        instruction=instruction,
        organization_id=str(organization_id),
        user_id=str(user_id) if user_id else None,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
