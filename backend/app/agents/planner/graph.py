"""The planner: decompose an instruction into the agents that must run, in order.

Layer: agents. Pure reasoning — `docs/agents.md` gives it no tools, and it has
none. It reads a string and returns a list of agent names. It touches no
database, no provider, and no model.

**Why this is a module of functions and not a compiled graph.** The stub here
said `build_graph() -> compiled StateGraph`, and every other agent in this
package is one. A graph earns its keep when there are branches to trace, cycles
to bound, or state to checkpoint — the RAG agent has all three (retrieve →
rewrite → retrieve), and the approval agents need the pause to be durable.
Planning has none: one input, one output, no I/O, no failure mode worth resuming
from. Wrapping it in a `StateGraph` would add a framework round trip and a trace
step that only ever says "the planner ran", while making it harder to call from a
test.

So the planner is invoked as a *node inside the supervisor's graph*, which is
exactly what `docs/agents.md` rule 1 asks for — "agents are nodes in one graph
passing a typed state object" — without pretending a pure function is a state
machine.

What a plan is, and what it deliberately is not
------------------------------------------------
A plan here is an **ordered list of agent names**, at most two long, produced by
rules. It is not a model deciding what to do next, and it cannot be: there is no
API key in this environment (ADR-0010), and a planner that guessed would produce
plausible-looking sequences that quietly did the wrong work.

The two-step ceiling is deliberate rather than a limitation nobody got round to
removing. Every multi-step request this product can express today is "look
something up, then act on it" — and an unbounded planner with no model behind it
would be a rule table pretending to be reasoning. When there is a real model, the
seam to widen is `Router`, not this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents import CALENDAR_AGENT, EMAIL_AGENT, RAG_AGENT

LOOKUP_LEAD = re.compile(
    r"^\s*(find|look\s+up|check|search|tell\s+me|summari[sz]e|explain|what\s+does)\b",
    re.IGNORECASE,
)
"""An instruction that *starts* by asking for information.

Anchored to the beginning on purpose. "Email ada@example.com saying please check
the handbook" contains "check", and matching it anywhere would turn a message
into a two-step plan that answers a question nobody asked and then emails the
answer. Where the verb appears is the difference between an instruction and its
content.
"""

CONJUNCTION = re.compile(r"\b(and|then)\b", re.IGNORECASE)
"""What separates the lookup from the action. Required, so that "find the
expenses policy" — a plain question — is not read as a plan with a missing second
step."""


@dataclass(frozen=True)
class Plan:
    """An ordered sequence of agents, and why.

    `steps` is empty when nothing can be done — which is a *plan*, not a failure.
    The supervisor renders it as a refusal, and having one shape for "do these
    things" and "do nothing" means no caller has to check for None.
    """

    steps: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def is_multi_step(self) -> bool:
        return len(self.steps) > 1

    @property
    def first(self) -> str | None:
        return self.steps[0] if self.steps else None


def plan_for(instruction: str, *, capable_of: str | None, is_question: bool) -> Plan:
    """Decompose `instruction`, given two judgements made elsewhere.

    Both inputs are passed in rather than re-derived here, and the first draft of
    this module got that wrong in a way worth recording. It owned its own idea of
    what a question looked like (`LOOKUP_LEAD`, which knew "what does" but not
    "how"), while the router owned a second one — so "How are expenses
    reimbursed?" was a question to the router, not a question to the planner, and
    fell through to a refusal. Three of twenty golden examples failed on the split.

    **One judgement, one owner.** `capable_of` is the agent whose *own parser*
    accepted the instruction (`supervisor/tools.py`), because the authority on
    "can the email agent do this?" is the email agent's parser. `is_question` is
    the router's, because deciding whether something is a request for information
    is routing, not planning. What is left here — the part that is genuinely
    planning — is the *order*.

    `LOOKUP_LEAD` survives for one job only: telling a two-step request from a
    one-step one. That is a question about sentence shape rather than about
    meaning, and it belongs with the code that builds the sequence.
    """
    text = instruction.strip()

    if not text:
        return Plan(steps=[], reason="There was nothing to act on.")

    leads_with_lookup = LOOKUP_LEAD.search(text) is not None

    if leads_with_lookup and capable_of in {EMAIL_AGENT, CALENDAR_AGENT}:
        if CONJUNCTION.search(text) is None:
            # "Find the meeting on 2026-09-10 09:00" — a lookup that happens to
            # mention a date, not a plan. Without this the planner would schedule
            # a meeting the user was asking a question about, which is the worst
            # available outcome: an unrequested side effect, produced by a read.
            return Plan(steps=[RAG_AGENT], reason="A question that mentions a date or an address.")

        return Plan(
            steps=[RAG_AGENT, capable_of],
            reason="Look it up first, then act on what was found.",
        )

    if capable_of is not None:
        return Plan(steps=[capable_of], reason=f"{capable_of} can perform this directly.")

    if is_question:
        return Plan(steps=[RAG_AGENT], reason="A question about the organization's documents.")

    return Plan(steps=[], reason="No agent here can do that.")
