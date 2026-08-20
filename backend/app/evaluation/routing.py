"""The routing golden set: does the right agent get the work?

Layer: evaluation. A second dataset shape beside `datasets.py`, and deliberately
not the same one.

**Why a separate type rather than a field on `GoldenDataset`.** That dataset is a
*corpus plus questions*: it validates that every example names a document in the
corpus, because an example pointing at a missing document scores zero recall and
looks like a retrieval regression. A routing example has no corpus at all — the
question is which agent should be asked, not what is in the index — so reusing
that type would mean either loosening its validation for everybody or shipping a
fake document to satisfy it.

**Why this exists at all.** `docs/agents.md` says the supervisor and specialists
"arrive only when a single agent measurably fails at the breadth of tasks", and
`docs/roadmap.md` repeats it: multi-agent, *only where the single agent measurably
falls short*. "Measurably" is a word with an obligation attached. This file and
`data/routing.json` are how that obligation is met — the shortfall is a committed
number, taken before the supervisor existed, not an assertion in a design doc.

The number, for the record: the single RAG agent routes **1.000** of the questions
it is suited to and **0.000** of everything else, because it has no other
behaviour. That is not a criticism of it. It is what "falls short at breadth"
means, stated in a form that can be re-measured after a change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.agents import AGENT_NAMES
from app.evaluation.datasets import DatasetError

DATA_ROOT = Path(__file__).parent / "data"

UNROUTABLE = "none"
"""The expected label for an instruction no agent should take.

The counterpart of `expect_refusal` in the RAG dataset, and left out of routing
evaluations just as often. A router with no "none" is a router that always picks
something: give it "what is the meaning of life" and it will confidently hand the
work to the calendar agent, because that was the least-bad of the options it was
forced to choose between.
"""


@dataclass(frozen=True)
class RoutingExample:
    """One instruction, and the agent that should receive it."""

    id: str
    instruction: str

    expected_agent: str
    """An `AGENT_NAMES` value, or `UNROUTABLE`."""

    expected_plan: list[str] | None = None
    """For a multi-step request, the agents in the order they must run.

    `None` for a single-step example — which is *not* the same as an empty list.
    None means "this example says nothing about planning"; an empty list would
    mean "the correct plan is to do nothing", and scoring the two identically
    would let a planner that never plans score perfectly on every single-step
    example in the set.
    """

    note: str = ""
    """Why this example is in the set, for whoever reads a failure."""


@dataclass(frozen=True)
class RoutingDataset:
    """Instructions and the agents that should handle them."""

    name: str
    description: str
    examples: list[RoutingExample]

    @property
    def multi_step(self) -> list[RoutingExample]:
        """Examples that need more than one agent, in order."""
        return [example for example in self.examples if example.expected_plan]


@lru_cache(maxsize=4)
def load_routing_dataset(name: str = "routing") -> RoutingDataset:
    """Read and validate `data/<name>.json`.

    Validated rather than trusted, for the reason `load_dataset` gives: every
    check below otherwise fails silently at run time, and an empty dataset scores
    1.000 from zero measurements — indistinguishable in CI from a perfect run.
    """
    path = DATA_ROOT / f"{name}.json"

    if not path.is_file():
        message = f"No routing dataset named {name!r} at {path}"
        raise DatasetError(message)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        message = f"routing dataset {name!r} is not valid JSON: {error}"
        raise DatasetError(message) from error

    dataset = RoutingDataset(
        name=raw.get("name", name),
        description=raw.get("description", ""),
        examples=[
            RoutingExample(
                id=item["id"],
                instruction=item["instruction"],
                expected_agent=item["expected_agent"],
                expected_plan=item.get("expected_plan"),
                note=item.get("note", ""),
            )
            for item in raw.get("examples", [])
        ],
    )

    _validate(dataset)
    return dataset


def _validate(dataset: RoutingDataset) -> None:
    """Refuse a dataset that would produce a meaningless score."""
    if not dataset.examples:
        message = (
            f"routing dataset {dataset.name!r} has no examples — a run over nothing scores 1.0"
        )
        raise DatasetError(message)

    seen: set[str] = set()

    for example in dataset.examples:
        if example.id in seen:
            message = f"duplicate example id {example.id!r} — one would overwrite the other"
            raise DatasetError(message)

        seen.add(example.id)

        # Checked against the registry rather than a list here, so that renaming
        # an agent breaks the dataset loudly instead of silently scoring every
        # example against a label nothing can ever produce.
        if example.expected_agent != UNROUTABLE and example.expected_agent not in AGENT_NAMES:
            message = (
                f"example {example.id!r} expects agent {example.expected_agent!r}, "
                f"which is not in AGENT_NAMES ({sorted(AGENT_NAMES)})"
            )
            raise DatasetError(message)

        for step in example.expected_plan or []:
            if step not in AGENT_NAMES:
                message = f"example {example.id!r} plans a step {step!r} that is not an agent"
                raise DatasetError(message)

    if not any(example.expected_agent == UNROUTABLE for example in dataset.examples):
        # The same argument `datasets.py` makes about refusals: a set made only of
        # routable instructions cannot tell a good router from one that always
        # picks something.
        message = (
            f"routing dataset {dataset.name!r} has no {UNROUTABLE!r} examples — "
            "it cannot detect a router that always chooses an agent"
        )
        raise DatasetError(message)
