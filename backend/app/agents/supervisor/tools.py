"""Routing: which agent should take this instruction?

Layer: agents. The supervisor's only real decision, and the one M15 exists to
make well enough to measure.

**Routing by capability, not by keywords.**
-------------------------------------------
The obvious router is a keyword table — "email" means the email agent, a date
means the calendar agent. It is wrong within a day, and the golden set contains
the example that shows why:

    Email ada@example.com about the review on 2026-09-10 09:00 saying please confirm

That has an address *and* a date. A router scoring keywords independently sees a
calendar instruction and books a meeting, and the message the user asked for is
never sent. Worse, the two tables drift: the calendar agent's parser gets
stricter, the router's idea of a calendar instruction does not, and the router
starts confidently handing work to an agent that will refuse it.

So the router does not guess. It **asks each specialist's own parser** whether it
can do the work — `parse_draft_request` and `parse_event_request`, the same
functions that would run if the work were routed there. A specialist that cannot
parse an instruction cannot be given it, by construction, and the router cannot
hold an opinion that disagrees with the agent it is routing to.

Order is the tie-break, and email comes first
----------------------------------------------
Both parsers accept the example above. Email wins, because its parser demands
*more*: an address and a body. A date can appear inside any sentence; "saying"
plus an address is a request to write to somebody. Precedence by specificity, and
the golden set is where that claim is checked rather than asserted.

The RAG fallback, and why it is not "everything else"
------------------------------------------------------
Anything neither specialist can execute is a question **only if it looks like
one**. The remainder is `None`, which the supervisor renders as a refusal.

Leaving out that last case is the most common routing bug there is. A router
forced to choose among agents will always choose one — hand it "order me a taxi"
and it picks whichever specialist scored least badly, then produces a confident
failure. `routing.json` carries five `none` examples for exactly this reason, and
`load_routing_dataset` refuses a dataset that has none of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.agents import CALENDAR_AGENT, EMAIL_AGENT
from app.agents.calendar.tools import parse_event_request
from app.agents.email.tools import parse_draft_request
from app.agents.planner.graph import Plan, plan_for

QUESTION_LEAD = re.compile(
    r"^\s*(what|how|when|where|why|who|which|does|do|is|are|can|should|could|"
    r"tell\s+me|find|look\s+up|check|search|summari[sz]e|explain|describe)\b",
    re.IGNORECASE,
)
"""What a request for information looks like when it is not punctuated as one.

`Tell me the policy on stolen laptops` has no question mark, and people write
like that constantly — a router keyed on punctuation alone sends every one of
them to the refusal path.
"""

PLEASANTRY = re.compile(
    r"^\s*(hi|hey|hello|thanks|thank\s+you|ta|cheers|bye|goodbye|ok|okay|yes|no|sure)\b",
    re.IGNORECASE,
)
"""Conversational filler that no agent should be woken up for.

Checked *before* everything else, because "no" and "ok" would otherwise fall
through to the question test on their leading word.
"""

MINIMUM_WORDS = 2
"""Below this there is nothing to route. A single word is a greeting, a typo, or
somebody testing the box."""


@dataclass(frozen=True)
class RoutingDecision:
    """Where the work goes, and why.

    `reason` is not decoration: it is written into the trace, so "why did my
    question get refused?" is answerable from the run rather than by reasoning
    about a regex at three in the morning.
    """

    agent: str | None
    plan: list[str]
    reason: str

    @property
    def is_multi_step(self) -> bool:
        return len(self.plan) > 1


@runtime_checkable
class Router(Protocol):
    """What the supervisor needs in order to route.

    A protocol for the same reason `LLMProvider` and `EmbeddingProvider` are: the
    real implementation of this is a model deciding, and there is no key in this
    environment. `RuleRouter` is the offline counterpart — deterministic, free,
    and honest about being rules rather than judgement.

    `runtime_checkable` so conformance is asserted in a test rather than
    discovered as an `AttributeError` mid-request.
    """

    def route(self, instruction: str) -> RoutingDecision:
        """Decide which agent, or agents in order, should act."""
        ...


class RuleRouter:
    """Deterministic routing, by asking each specialist's parser.

    Free, reproducible, and with no opinion of its own about what an email looks
    like — see the module docstring. Its ceiling is real: it cannot tell "book me
    something next Tuesday afternoon" from noise, because neither can the parsers
    it consults, and that limit is measured in `routing.json` rather than hidden.
    """

    def route(self, instruction: str) -> RoutingDecision:
        text = instruction.strip()

        if len(text.split()) < MINIMUM_WORDS or PLEASANTRY.match(text):
            return _nothing("There is nothing here to act on.")

        plan = plan_for(
            text,
            capable_of=self._capable_of(text),
            # Decided here rather than in the planner. Whether something is a
            # request for information is a routing judgement; the planner's job is
            # the *order* of what follows. Splitting it produced two regexes with
            # two different ideas of what a question was, and three golden examples
            # failed in the gap — see `planner/graph.py`.
            is_question=self._is_question(text),
        )

        if not plan.steps:
            # The refusal names what this product *can* do. "I cannot help with
            # that" tells a user nothing and invites them to rephrase the same
            # impossible request; listing the three capabilities is the only
            # response that lets them succeed on the next try.
            return _nothing(
                "I can answer questions about your documents, schedule calendar "
                "events, and draft email. That is not one of those."
            )

        return RoutingDecision(agent=plan.first, plan=list(plan.steps), reason=plan.reason)

    def _is_question(self, text: str) -> bool:
        """Whether this reads as a request for information.

        Punctuation *or* an opening interrogative, because both occur and neither
        is reliable alone: "Tell me the policy on stolen laptops" has no question
        mark, and "can you do this by Friday?" has one without being a lookup the
        RAG agent should take — which the capability check upstream has already
        handled by the time this is asked.
        """
        return text.endswith("?") or QUESTION_LEAD.match(text) is not None

    def _capable_of(self, text: str) -> str | None:
        """Which specialist's own parser accepts this instruction.

        Email before calendar: its parser demands an address *and* a body, so it
        is the more specific claim. See the module docstring for the example that
        decides this ordering, and `routing.json` for where it is checked.
        """
        if parse_draft_request(text) is not None:
            return EMAIL_AGENT

        if parse_event_request(text) is not None:
            return CALENDAR_AGENT

        return None


def _nothing(reason: str) -> RoutingDecision:
    return RoutingDecision(agent=None, plan=[], reason=reason)


__all__ = [
    "Plan",
    "RoutingDecision",
    "RuleRouter",
    "Router",
]
