"""The calendar agent: a graph that stops, and a row that remembers it stopped.

Layer: agents. Invoked through `services/agent_service.py`, never from a route.

The shape
---------
    propose graph:   plan ──► propose ──► END        (run 1, ends PAUSED)
                                  │
                          [ approvals row ]
                                  │
    execute graph:            execute ──► END        (run 2, after a human decides)

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
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.calendar.tools import CREATE_EVENT, describe, parse_event_request

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

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
"""What `build_create_event` returns. Typed as a plain callable rather than a
`BaseTool` on purpose — see `tools.py`."""

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


def build_execute_graph(execute_action: Executor, record: StepRecorder) -> CalendarGraph:
    """The second half: do the thing a human permitted.

    A separate compiled graph rather than a branch in the first one, because the two
    run in different processes at different times. A single graph would have to
    re-enter at `execute`, which means either a checkpointer holding the position (a
    second store of a fact the row already holds) or a conditional edge that skips
    `plan` — and a skipped planning step is one nobody can prove did not run.

    The executor is injected rather than built here for the same reason the RAG
    graph takes its tools: this module decides *when* the side effect happens, and
    `tools.py` decides *what* it is.
    """

    async def execute(state: CalendarState) -> CalendarState:
        started = time.perf_counter()
        action = state.get("proposed_action")

        if action is None:  # pragma: no cover — the service refuses to resume
            message = "Cannot execute a run with no proposed action."
            raise ValueError(message)

        result = await execute_action(action)

        record(
            {
                "node_name": EXECUTE,
                "tool_name": CREATE_EVENT,
                "tool_input": action,
                "tool_output": result,
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )
        return {"result": result}

    graph: StateGraph[CalendarState, Any, CalendarState, CalendarState] = StateGraph(CalendarState)
    graph.add_node(EXECUTE, execute)
    graph.add_edge(START, EXECUTE)
    graph.add_edge(EXECUTE, END)

    return graph.compile()


def checkpoint_of(state: CalendarState) -> dict[str, Any]:
    """The subset of state worth persisting across the pause.

    A whitelist rather than the whole dict, for the same reason response schemas are
    whitelists: whatever a node adds later should not silently become part of the
    durable contract that a resume — possibly running a newer version of this code —
    has to understand.
    """
    return {
        "instruction": state.get("instruction", ""),
        "organization_id": state.get("organization_id", ""),
        "user_id": state.get("user_id"),
        "proposed_action": state.get("proposed_action"),
        "summary": state.get("summary", ""),
    }


def state_from_checkpoint(checkpoint: dict[str, Any]) -> CalendarState:
    """Rebuild state for the resume path.

    Ids come back as strings and stay strings; nothing here parses them into
    `uuid.UUID`, because the executor closes over the tenant from the *request*
    rather than reading it from state. A checkpoint that could nominate its own
    organization would be an injection surface that survives restarts.
    """
    return CalendarState(
        instruction=str(checkpoint.get("instruction", "")),
        organization_id=str(checkpoint.get("organization_id", "")),
        user_id=checkpoint.get("user_id"),
        proposed_action=checkpoint.get("proposed_action"),
        summary=str(checkpoint.get("summary", "")),
    )


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
