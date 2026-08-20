"""The email agent: propose a message, stop, and let a human read it.

Layer: agents. Invoked through `services/agent_service.py`, never from a route.

    propose graph:   compose ──► propose ──► END      (run 1, ends PAUSED)
                                    │
                            [ approvals row ]
                                    │
    execute graph:  app/agents/execution.py            (run 2, after approval)

The same shape as the calendar agent, and the execute half is now literally the
same code — `build_execute_graph` moved to `app/agents/execution.py` when this
became the second action kind. What stays here is the part that genuinely differs:
turning an instruction into a *message*, and failing in email's own ways.

Why `compose` and `propose` are separate nodes
-----------------------------------------------
The same reason `plan` and `propose` are separate in the calendar agent: the trace
records what was *understood* apart from what was *requested*. A run that proposed
nothing because it found no recipient looks identical, in a one-node trace, to one
that composed a message nobody wanted.

What the trace deliberately does not contain
---------------------------------------------
**The message body never goes into a step**, and neither does the instruction it
came from. Everywhere else in this codebase the whole action is recorded, and M12
argued for it: "what exactly were they asked to approve?" is the first question
about a side effect that went wrong.

Email is the exception, and the reason is who reads each store. `agent_steps` is
operational data — read while debugging, exported into whatever observability
stack a deployment runs, kept as long as traces are kept. `approvals` is the record
of a decision, scoped to the organization and returned only through tenant-checked
endpoints. The body of somebody's email belongs in the second.

Being exact about what this does and does not achieve: **the body is still in
`agent_runs.input`**, because the user typed it there and no design removes it. It
cannot be eliminated, only kept from being copied. An earlier draft of this module
redacted the body from `propose` while `compose` recorded the whole instruction —
which contains it — and a test caught that. One copy inside the tenant boundary is
unavoidable; three is a choice.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, TypedDict

import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.email.tools import describe, parse_draft_request

logger = structlog.get_logger(__name__)

COMPOSE = "compose"
PROPOSE = "propose"

NOTHING_TO_PROPOSE = (
    "I could not work out who to write to or what to say. Tell me like this: "
    "email alice@example.com about the Q3 numbers saying the report is ready."
)
"""What the run says when the instruction could not be parsed.

A refusal that shows the user how to succeed. The parser is deliberately strict
(see `tools.py`), so this is a common outcome rather than an exceptional one —
which makes the wording part of the feature rather than an error string.
"""


class EmailState(TypedDict, total=False):
    """What flows through the email graph.

    **Every field is JSON-serialisable**, and that is a hard requirement rather
    than a style choice: this state is written to `agent_runs.checkpoint` as JSONB
    and read back by a *different process* after a human decides.
    """

    instruction: str
    organization_id: str
    user_id: str | None

    proposed_action: dict[str, Any] | None
    """The message a human will be asked to permit, or None if none was understood.

    The same dict that gets stored, displayed and sent — see `tools.py`.
    """

    summary: str
    """The one line the human reads. Rendered from the action by code."""

    refusal: str
    """Set when nothing could be proposed. Present rather than implied by
    `proposed_action is None`, so the trace and the run output can say *why*."""


StepRecorder = Callable[[dict[str, Any]], None]
EmailGraph = CompiledStateGraph[EmailState, Any, EmailState, EmailState]


def build_propose_graph(record: StepRecorder) -> EmailGraph:
    """Understand the instruction, propose a message, stop.

    It reaches `END` whether or not it proposed anything. A run that understood
    nothing is finished, not paused — there is nothing for a human to decide on,
    and creating an empty approval so the shapes match would put a row in
    somebody's inbox asking them to permit nothing.
    """

    async def compose(state: EmailState) -> EmailState:
        """Read the instruction. Touches nothing, and reaches no provider."""
        started = time.perf_counter()
        action = parse_draft_request(state["instruction"])

        record(
            {
                "node_name": COMPOSE,
                "tool_name": None,
                # **Not the instruction itself**, which the calendar agent does
                # record. An email instruction *contains the message body* — it is
                # the text a person typed intending to send it — and it is already
                # stored verbatim one join away in `agent_runs.input`. Recording it
                # here would put a second copy in the table most likely to be
                # exported to a third-party observability stack.
                #
                # An earlier draft of this milestone redacted the body from the
                # `propose` step below while this node wrote the whole instruction
                # containing it. A test caught that; one copy inside the tenant
                # boundary is unavoidable, three is a choice.
                "tool_input": {"instruction_length": len(state["instruction"])},
                "tool_output": {"understood": action is not None},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )

        if action is None:
            return {"proposed_action": None, "refusal": NOTHING_TO_PROPOSE}

        return {"proposed_action": action}

    async def propose(state: EmailState) -> EmailState:
        """Render the message for a human. Still touches nothing."""
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
                # Recipient and subject, never the body. See the module docstring:
                # `agent_steps` is operational data and the body belongs in the
                # approval row, which is tenant-scoped.
                "tool_input": {
                    "to": action["to"],
                    "subject": action["subject"],
                    "body_length": len(action["body"]),
                },
                "tool_output": {"proposed": True, "summary": summary},
                "latency_ms": _elapsed_ms(started),
                "tokens": 0,
            }
        )
        return {"summary": summary}

    graph: StateGraph[EmailState, Any, EmailState, EmailState] = StateGraph(EmailState)
    graph.add_node(COMPOSE, compose)
    graph.add_node(PROPOSE, propose)
    graph.add_edge(START, COMPOSE)
    graph.add_edge(COMPOSE, PROPOSE)
    graph.add_edge(PROPOSE, END)

    return graph.compile()


def checkpoint_of(state: EmailState) -> dict[str, Any]:
    """The subset of state worth persisting across the pause.

    A whitelist rather than the whole dict, for the same reason response schemas
    are whitelists: whatever a node adds later should not silently become part of
    the durable contract a resume — possibly running a newer version of this code —
    has to understand.
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
) -> EmailState:
    """Seed state, with ids stringified at the boundary rather than at use."""
    return EmailState(
        instruction=instruction,
        organization_id=str(organization_id),
        user_id=str(user_id) if user_id else None,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
