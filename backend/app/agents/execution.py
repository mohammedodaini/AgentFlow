"""Executing an approved action, whatever kind of action it is.

Layer: agents. The piece ADR-0015 promised and did not build:

    "The approval machinery is provider-agnostic; adding the second action kind
     is a `requested_action["kind"]` and an executor."

M14 adds that second kind — `email.send_draft` — and this module is where the
claim gets tested. It holds two things: the one-node graph that runs an approved
action, and the registry that maps a stored `kind` to the code that performs it.

Why the execute graph is shared and the propose graphs are not
--------------------------------------------------------------
Proposing is where the agents genuinely differ. The calendar agent parses a date;
the email agent works out a recipient, a subject and a body. Different state,
different failure modes, different traces — so `calendar/graph.py` and
`email/graph.py` each keep their own.

Executing is the same operation every time: take the dict a human approved, call
the one function that performs it, record what happened. M12 wrote that as
`build_execute_graph` inside `calendar/graph.py`, which was right when calendar
was the only kind. Copying it would mean the next fix to how side effects are
traced lands in one file and not the other.

Why dispatch is a registry and not an `if`
-------------------------------------------
`ApprovalService.approve` used to call `resume_calendar_run` by name. With two
kinds that becomes a branch, and a branch on a string is a place where an unknown
value falls through to *something* — most likely the first arm, which would mean
an approval for one kind of action executing as another.

A dict lookup has no fallthrough. An unrecognised kind raises, and the
`UNKNOWN_ACTION` message says which kind it was, because the realistic way to
reach it is a checkpoint written by an older deploy.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.calendar import tools as calendar_tools
from app.agents.email import tools as email_tools
from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.integrations import OAuthRegistry

EXECUTE = "execute"

ActionExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
"""What an executor is: approved action in, result out.

Typed as a plain callable rather than a `BaseTool` on purpose. Tools are things a
graph may call whenever it likes; this is something only the resume path may call,
and giving it the same type as `search_chunks` would invite exactly the mistake
the approval design exists to prevent.
"""

ExecutorFactory = Callable[[AsyncSession, OAuthRegistry, Settings, uuid.UUID], ActionExecutor]
"""How an executor is built: with a session, a provider registry, settings, and
**the tenant**, which is closed over rather than passed per call (ADR-0012)."""


class ExecutionState(TypedDict, total=False):
    """The state the execute graph runs on.

    Deliberately tiny, and shared by every action kind. It holds the approved
    action and the result of performing it — nothing about calendars or mailboxes,
    because a graph that knew the difference would need a branch for each kind.

    JSON-serialisable throughout, like every state in this package: it is
    reconstructed from `agent_runs.checkpoint` in a different process.
    """

    proposed_action: dict[str, Any]
    result: dict[str, Any]


StepRecorder = Callable[[dict[str, Any]], None]
ExecuteGraph = CompiledStateGraph[ExecutionState, Any, ExecutionState, ExecutionState]


@dataclass(frozen=True)
class ActionKind:
    """Everything the resume path needs to know about one kind of action.

    `agent_name` is here so a resumed run is attributed to the agent that proposed
    it rather than to a generic "executor" — `agent_runs.agent_name` is what
    somebody filters on when asking what the email agent has been doing.
    """

    kind: str
    agent_name: str
    tool_name: str
    build_executor: ExecutorFactory
    describe: Callable[[dict[str, Any]], str]


def build_execute_graph(
    execute_action: ActionExecutor, record: StepRecorder, *, tool_name: str
) -> ExecuteGraph:
    """The second half of every approval flow: do the thing a human permitted.

    A separate compiled graph rather than a branch in the propose graph, because
    the two run in different processes at different times. A single graph would
    have to re-enter at `execute`, which means either a checkpointer holding the
    position — a second store of a fact the row already holds — or a conditional
    edge that skips planning, and a skipped planning step is one nobody can prove
    did not run.

    The executor is injected rather than built here for the same reason the RAG
    graph takes its tools: this module decides *when* the side effect happens, and
    each agent's `tools.py` decides *what* it is.
    """

    async def execute(state: ExecutionState) -> ExecutionState:
        started = time.perf_counter()
        action = state.get("proposed_action")

        if not action:  # pragma: no cover — the service refuses to resume without one
            message = "Cannot execute a run with no proposed action."
            raise ValueError(message)

        result = await execute_action(action)

        record(
            {
                "node_name": EXECUTE,
                "tool_name": tool_name,
                # The whole action goes in the trace. It is small, and "what
                # exactly was performed?" is the first question anyone has about a
                # side effect that turned out to be wrong.
                "tool_input": action,
                "tool_output": result,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "tokens": 0,
            }
        )
        return {"result": result}

    graph: StateGraph[ExecutionState, Any, ExecutionState, ExecutionState] = StateGraph(
        ExecutionState
    )
    graph.add_node(EXECUTE, execute)
    graph.add_edge(START, EXECUTE)
    graph.add_edge(EXECUTE, END)

    return graph.compile()


def action_kind(kind: str) -> ActionKind:
    """Look up how to perform a stored action, or refuse.

    The registry is rebuilt per call rather than held at module level. It is five
    dict entries, it is reached once per approval — never in a loop — and a module
    constant would have to be constructed at import time, which is what turned an
    earlier draft of this file into an import cycle: `email/tools.py` imported the
    executor type from here while this module imported the tools.

    That cycle was avoidable rather than intrinsic, and the fix was to stop the
    tools modules depending on this one at all. They now name their own return type
    and know nothing about the registry that dispatches to them — which is the right
    direction for the dependency anyway: a tool should not know it is dispatchable.
    """
    kinds = {
        calendar_tools.PROPOSED_ACTION_KIND: ActionKind(
            kind=calendar_tools.PROPOSED_ACTION_KIND,
            agent_name="calendar",
            tool_name=calendar_tools.CREATE_EVENT,
            build_executor=calendar_tools.build_create_event,
            describe=calendar_tools.describe,
        ),
        email_tools.PROPOSED_ACTION_KIND: ActionKind(
            kind=email_tools.PROPOSED_ACTION_KIND,
            agent_name="email",
            tool_name=email_tools.SEND_DRAFT,
            build_executor=email_tools.build_send_draft,
            describe=email_tools.describe,
        ),
    }

    known = kinds.get(kind)

    if known is None:
        # A `ConflictError` (409) rather than a 404 or a 500: the row exists and is
        # well-formed, and what cannot be satisfied is the request to act on it
        # *now*, with this deploy. The realistic cause is a checkpoint written by a
        # version that knew a kind this one does not.
        message = f"This deployment cannot perform a '{kind}' action."
        raise ConflictError(message)

    return known
