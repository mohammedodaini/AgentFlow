"""Scoring a router against the routing golden set.

Layer: evaluation. The counterpart of `runner.py`, and much cheaper: routing is a
pure function of a string, so this needs no database, no ingestion, no model and
no network. It runs in milliseconds and is therefore a unit test as well as a
gate.

**The control is the point.** `docs/agents.md` says the supervisor arrives "only
when a single agent measurably fails at the breadth of tasks", so every run
scores *two* routers: the one under test, and `SingleAgentRouter` — the M9–M14
world, where every instruction went to the RAG agent because that was the only
thing a single entry point could do.

Both numbers go into the report and into the committed baseline, side by side,
permanently. A year from now the question "was the supervisor worth building?"
has an answer in the repository rather than in somebody's memory, and a change
that quietly makes routing worse than doing nothing fails the gate.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from app.agents import RAG_AGENT
from app.agents.supervisor.tools import Router, RoutingDecision
from app.evaluation.routing import UNROUTABLE, RoutingExample, load_routing_dataset


class SingleAgentRouter:
    """The world before M15: one agent, and it takes everything.

    Not a straw man — this is exactly what `POST /agent-runs` did from M9 to M14,
    and it was the right design then. It is here as a control so the shortfall is
    a measurement rather than an assertion in a design document.
    """

    def route(self, instruction: str) -> RoutingDecision:
        del instruction
        return RoutingDecision(agent=RAG_AGENT, plan=[RAG_AGENT], reason="The only agent there is.")


@dataclass
class RoutingResult:
    """What one router did with one example."""

    id: str
    instruction: str
    expected_agent: str
    actual_agent: str
    expected_plan: list[str] | None
    actual_plan: list[str]
    note: str = ""

    @property
    def agent_correct(self) -> bool:
        return self.actual_agent == self.expected_agent

    @property
    def plan_correct(self) -> bool | None:
        """None when the example says nothing about planning.

        Distinct from False on purpose: averaging "not applicable" as a zero would
        drag `plan_accuracy` down by the number of single-step examples in the
        set, so adding an ordinary question to the dataset would look like a
        planner regression.
        """
        if self.expected_plan is None:
            return None

        return self.actual_plan == self.expected_plan

    @property
    def correct(self) -> bool:
        """Right agent *and*, where the example says so, right order."""
        return self.agent_correct and self.plan_correct is not False


@dataclass
class RoutingReport:
    """One run over the routing set, for one router, plus the control.

    Deliberately shaped like `EvalReport` — `dataset`, `generated_at`,
    `settings`, `aggregate` — so `compare_to_baseline`, `load_baseline` and
    `save_baseline` work on it unchanged. One gate implementation, two datasets.
    """

    dataset: str
    generated_at: str
    settings: dict[str, object]
    results: list[RoutingResult] = field(default_factory=list)
    control: list[RoutingResult] = field(default_factory=list)

    @property
    def aggregate(self) -> dict[str, float]:
        """The headline numbers, and the ones the regression gate reads."""
        if not self.results:
            return {}

        unroutable = [r for r in self.results if r.expected_agent == UNROUTABLE]
        planned = [r for r in self.results if r.expected_plan is not None]

        return {
            "routing_accuracy": _mean([float(r.correct) for r in self.results]),
            # Reported separately for the same reason `refusal_accuracy` is: a
            # router that always picks an agent scores well on everything it is
            # *supposed* to route and hides the failure that matters, because the
            # instructions nobody should act on are a minority of any real set.
            "unroutable_accuracy": _mean([float(r.correct) for r in unroutable]),
            "plan_accuracy": _mean([float(r.plan_correct is True) for r in planned]),
            # The control. Committed beside the others so the value of the
            # supervisor stays visible, and so a change that makes routing worse
            # than the single agent cannot pass quietly.
            "single_agent_accuracy": _mean([float(r.correct) for r in self.control]),
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "dataset": self.dataset,
                "generated_at": self.generated_at,
                "settings": self.settings,
                "aggregate": self.aggregate,
                "examples": [asdict(result) for result in self.results],
            },
            indent=2,
            sort_keys=True,
        )

    @property
    def failures(self) -> list[RoutingResult]:
        return [result for result in self.results if not result.correct]


def run_routing_eval(router: Router, *, dataset_name: str = "routing") -> RoutingReport:
    """Score `router`, and the single-agent control, over the routing set."""
    dataset = load_routing_dataset(dataset_name)

    return RoutingReport(
        dataset=dataset.name,
        generated_at=datetime.now(UTC).isoformat(),
        settings={"router": type(router).__name__},
        results=[_score(router, example) for example in dataset.examples],
        control=[_score(SingleAgentRouter(), example) for example in dataset.examples],
    )


def _score(router: Router, example: RoutingExample) -> RoutingResult:
    decision = router.route(example.instruction)

    return RoutingResult(
        id=example.id,
        instruction=example.instruction,
        expected_agent=example.expected_agent,
        # `None` becomes the dataset's own label for "no agent", so the two
        # vocabularies never have to be reconciled at a comparison site.
        actual_agent=decision.agent or UNROUTABLE,
        expected_plan=example.expected_plan,
        actual_plan=list(decision.plan),
        note=example.note,
    )


def _mean(values: list[float]) -> float:
    """Zero for an empty list, not a `ZeroDivisionError`.

    Safe here because `load_routing_dataset` refuses a dataset with no examples
    and refuses one with no `none` examples — so the two slices this averages
    cannot both be empty by accident. `plan_accuracy` genuinely can be, for a
    dataset with no multi-step examples, and 0.0 is the honest reading of "no
    plans were got right".
    """
    return sum(values) / len(values) if values else 0.0
