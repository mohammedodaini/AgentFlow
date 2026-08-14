"""The typed state every node in a graph reads and writes.

Layer: agents. `docs/agents.md` rule 1: agents communicate through **this typed
object**, never free-text messages passed between each other. The reason is that
free text between components is a parsing problem disguised as an architecture —
one agent writes "found 3 results", the next has to decide what that means, and
the contract lives in nobody's type checker.

A `TypedDict` rather than a dataclass or Pydantic model because that is what
LangGraph expects: it merges the partial updates nodes return, which requires a
plain mapping. A node returns `{"answer": ...}` and LangGraph applies it;
returning a whole dataclass would mean every node reconstructing every field.

Which fields are *reducers* matters
-----------------------------------
`messages` is annotated with `add_messages`, so a node returning one message
appends rather than replaces. Every other field replaces on write. That
distinction is the easiest thing here to get wrong: annotating `retrieved` as
appending would silently accumulate chunks across a retry, and the second
attempt would answer from the union of both retrievals while appearing to work.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """What flows through the graph.

    `total=False` because nodes populate it progressively — `answer` does not
    exist until the generate node runs, and requiring every key up front would
    mean seeding the state with placeholders indistinguishable from real values.
    """

    question: str
    """The user's question, unmodified. Kept separate from `messages` so a retry
    can re-read the original rather than a rewritten one — otherwise a query
    rewrite compounds on itself."""

    organization_id: uuid.UUID
    """The tenant. Carried in state rather than closed over by the tools,
    because a tool that captures its tenant at construction time is a tool that
    silently serves the wrong one when the graph is reused across requests."""

    user_id: uuid.UUID | None

    messages: Annotated[list[AnyMessage], add_messages]
    """The conversation as LangChain messages. Appended to, never replaced.

    Barely used at M9 — one question, one answer — and present from the start
    because M10 adds real conversation history, and retrofitting the reducer
    later would mean revisiting every node.
    """

    search_query: str
    """What retrieval was actually asked, which may differ from `question` after
    a rewrite. Recorded so the trace shows the query that produced the chunks
    rather than the one the user typed."""

    retrieved: list[dict[str, Any]]
    """Serialised `ScoredChunk`s — plain dicts, not ORM objects or dataclasses.

    They have to survive being written to `agent_runs.checkpoint` as JSONB and
    read back after a restart, and an object that cannot round-trip through JSON
    cannot be checkpointed. That is the constraint LangGraph's persistence puts
    on state, and it is far easier to honour from the first node than to
    discover at M12, when a run resumes and half the state is gone.
    """

    answer: str
    citations: list[dict[str, Any]]

    usage: dict[str, int]
    """Token counts accumulated across the run, keyed `input` and `output`. A
    plain dict rather than a nested model, for the same JSON round-trip
    reason."""

    attempts: int
    """How many times retrieval has run. The graph's only loop bound — a
    conditional edge that can route back to `retrieve` needs something that
    strictly increases, or a bad query becomes an infinite graph."""
