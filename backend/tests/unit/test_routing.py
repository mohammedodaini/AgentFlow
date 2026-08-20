"""Routing and planning: who takes the work, and in what order (M15).

The golden set in `app/evaluation/data/routing.json` is the measurement; these
are the tests that say *why* each rule exists, and they are written so a failure
names the thing that would break for a user rather than the rule that changed.

The sharpest ones here are the ambiguity cases. Every routing bug that reaches
production looks like a reasonable keyword match.
"""

from __future__ import annotations

import pytest

from app.agents import CALENDAR_AGENT, EMAIL_AGENT, RAG_AGENT
from app.agents.calendar.tools import parse_event_request
from app.agents.email.tools import parse_draft_request
from app.agents.planner.graph import plan_for
from app.agents.supervisor.tools import Router, RoutingDecision, RuleRouter
from app.evaluation.routing import UNROUTABLE, load_routing_dataset
from app.evaluation.routing_runner import SingleAgentRouter, run_routing_eval


@pytest.fixture
def router() -> RuleRouter:
    return RuleRouter()


# --- the capability rule -------------------------------------------------


def test_a_question_goes_to_rag(router: RuleRouter) -> None:
    assert router.route("How are expenses reimbursed?").agent == RAG_AGENT


def test_a_datetime_goes_to_the_calendar(router: RuleRouter) -> None:
    assert router.route("Schedule a design review on 2026-09-10 09:00").agent == CALENDAR_AGENT


def test_an_address_and_a_body_go_to_email(router: RuleRouter) -> None:
    decision = router.route("Email ada@example.com about Q3 saying the report is ready")

    assert decision.agent == EMAIL_AGENT


def test_email_wins_over_a_date_in_the_same_instruction(router: RuleRouter) -> None:
    """**The example that decides the whole design.**

    This has an address *and* a date. A keyword router scoring the two
    independently sees a calendar instruction, books a meeting, and never sends
    the message the user actually asked for — a side effect nobody requested,
    instead of the one they did.

    Email wins because its parser demands more: an address *and* a body. A date
    can appear inside any sentence.
    """
    decision = router.route(
        "Email ada@example.com about the review on 2026-09-10 09:00 saying please confirm"
    )

    assert decision.agent == EMAIL_AGENT


def test_a_question_mentioning_a_meeting_is_still_a_question(router: RuleRouter) -> None:
    """The mirror of the case above. "design review" is not a date, so no
    specialist can parse it, and it falls to RAG rather than to the calendar."""
    assert router.route("What did we agree about the design review?").agent == RAG_AGENT


def test_the_router_never_disagrees_with_the_agent_it_routes_to(router: RuleRouter) -> None:
    """The property the capability rule buys, stated directly.

    A keyword router can send work to an agent whose parser will refuse it — the
    two tables drift apart, and the user gets "I could not work out when you
    wanted that" from an agent that should never have been asked. Here the router
    *is* the parser, so that gap cannot open.
    """
    for instruction in (
        "Schedule a design review on 2026-09-10 09:00",
        "Email ada@example.com about Q3 saying done",
        "Book a review on 2026-09-11 14:00",
    ):
        decision = router.route(instruction)

        if decision.agent == CALENDAR_AGENT:
            assert parse_event_request(instruction) is not None
        if decision.agent == EMAIL_AGENT:
            assert parse_draft_request(instruction) is not None


# --- refusing ------------------------------------------------------------


@pytest.mark.parametrize(
    "instruction",
    ["hello", "thanks, that's all", "asdfgh qwerty", "Order me a taxi to the airport", "   ", "ok"],
)
def test_nothing_is_routed_when_nothing_fits(router: RuleRouter, instruction: str) -> None:
    """A router forced to choose always chooses. Hand it "order me a taxi" with no
    refusal path and it picks whichever specialist scored least badly, then
    produces a confident failure."""
    assert router.route(instruction).agent is None


def test_a_refusal_says_what_the_product_can_do(router: RuleRouter) -> None:
    """ "I cannot help with that" tells a user nothing and invites them to rephrase
    the same impossible request."""
    reason = router.route("Order me a taxi to the airport").reason

    assert "documents" in reason
    assert "calendar" in reason
    assert "email" in reason


def test_an_unroutable_instruction_does_not_become_a_rag_question(router: RuleRouter) -> None:
    """Without this the RAG agent is the router's dustbin, and every impossible
    request comes back as "I could not find that in your documents" — which reads
    as a missing document rather than a missing feature."""
    assert router.route("Order me a taxi to the airport").plan == []


# --- planning ------------------------------------------------------------


def test_a_lookup_then_an_action_is_a_two_step_plan(router: RuleRouter) -> None:
    decision = router.route(
        "Find our expenses policy and email it to ada@example.com about expenses saying here it is"
    )

    assert decision.plan == [RAG_AGENT, EMAIL_AGENT]
    assert decision.is_multi_step


def test_the_second_step_is_not_hard_coded(router: RuleRouter) -> None:
    """A planner that always plans rag→email passes the email example and fails
    this one."""
    decision = router.route(
        "Check what the handbook says about reviews and schedule one on 2026-09-15 10:00"
    )

    assert decision.plan == [RAG_AGENT, CALENDAR_AGENT]


def test_a_lookup_with_no_conjunction_is_not_a_plan(router: RuleRouter) -> None:
    """**The dangerous case.**

    "Find the meeting on 2026-09-10 09:00" is a question that happens to contain a
    date. Read as a plan it would *schedule* a meeting the user was asking about —
    an unrequested side effect produced by what the user thought was a read, which
    is the worst outcome available to a router.
    """
    decision = router.route("Find the meeting on 2026-09-10 09:00")

    assert decision.plan == [RAG_AGENT]
    assert not decision.is_multi_step


def test_a_lookup_verb_inside_a_message_does_not_create_a_plan(router: RuleRouter) -> None:
    """ "check" appears in the body. `LOOKUP_LEAD` is anchored to the start for
    this reason — where the verb appears is the difference between an instruction
    and its content."""
    decision = router.route(
        "Email ada@example.com about the handbook saying please check the expenses policy"
    )

    assert decision.plan == [EMAIL_AGENT]


def test_the_planner_does_not_re_derive_capability() -> None:
    """`plan_for` is told which specialist accepted the instruction rather than
    working it out again. Two opinions on "can the email agent do this?" is how
    a router and an agent drift apart."""
    plan = plan_for("Find X and email it", capable_of=EMAIL_AGENT, is_question=True)

    assert plan.steps == [RAG_AGENT, EMAIL_AGENT]


def test_an_empty_instruction_plans_nothing() -> None:
    assert plan_for("   ", capable_of=None, is_question=False).steps == []


def test_a_question_with_no_capable_specialist_plans_rag() -> None:
    plan = plan_for("What is the policy?", capable_of=None, is_question=True)

    assert plan.steps == [RAG_AGENT]
    assert plan.first == RAG_AGENT
    assert not plan.is_multi_step


# --- the seam ------------------------------------------------------------


def test_both_routers_satisfy_the_protocol() -> None:
    """`runtime_checkable`, so a router missing `route` fails a test rather than a
    request."""
    assert isinstance(RuleRouter(), Router)
    assert isinstance(SingleAgentRouter(), Router)


def test_a_substitute_router_can_be_scored() -> None:
    """The seam exists so a model-backed router can be measured against the same
    set without touching the graph."""

    class AlwaysEmail:
        def route(self, instruction: str) -> RoutingDecision:
            del instruction
            return RoutingDecision(agent=EMAIL_AGENT, plan=[EMAIL_AGENT], reason="always")

    report = run_routing_eval(AlwaysEmail())

    assert report.aggregate["routing_accuracy"] < 0.5
    assert report.aggregate["unroutable_accuracy"] == 0.0


# --- the measurement itself ----------------------------------------------


def test_the_supervisor_beats_the_single_agent() -> None:
    """**The precondition `docs/agents.md` sets for this whole package.**

    "The supervisor and specialists arrive only when a single agent measurably
    fails at the breadth of tasks." This is that measurement, as an assertion:
    if a change ever makes routing no better than doing nothing, M15 stops being
    justified and this fails.
    """
    report = run_routing_eval(RuleRouter())

    assert report.aggregate["routing_accuracy"] > report.aggregate["single_agent_accuracy"]
    assert report.aggregate["single_agent_accuracy"] < 0.5


def test_the_router_gets_every_golden_example_right() -> None:
    """Not a claim that routing is solved — the set is twenty hand-written
    examples and the router is rules. It is the floor: a change that breaks one of
    them has to say so out loud."""
    report = run_routing_eval(RuleRouter())

    assert report.failures == [], [failure.id for failure in report.failures]


def test_every_dataset_example_names_a_real_agent() -> None:
    """Checked against `AGENT_NAMES`, so renaming an agent breaks the dataset
    loudly instead of scoring every example against a label nothing produces."""
    dataset = load_routing_dataset()

    assert len(dataset.examples) >= 20
    assert any(example.expected_agent == UNROUTABLE for example in dataset.examples)
    assert len(dataset.multi_step) >= 2
