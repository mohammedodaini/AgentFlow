"""Decay and reinforcement — what to keep remembering, and what to let go.

Layer: memory. Pure decision logic: given an importance, a last-access time and
a clock, return a number or an action. No database, no model, no I/O — which is
what makes a forgetting policy testable at all, and why it lives in its own
module rather than inside a repository method.

Why anything is forgotten
-------------------------
An agent that never forgets is not a better agent. Every stored memory is a
candidate for every future prompt, so an unbounded store means recall spends its
budget on facts that stopped being true — the old office address, the previous
approval chain — and there is no citation for the user to check them against. A
stale memory is not neutral: it is a confident, uncited claim that quietly
steers answers.

So memories fade unless something keeps them alive, and the thing that keeps
them alive is *use*.

The shape of the score
----------------------
    decay_score = importance × 0.5 ^ (days_since_access / HALF_LIFE_DAYS)

Exponential rather than linear, because "how long since this mattered?" has no
natural maximum — a linear decay needs an arbitrary horizon at which everything
becomes worthless, and an exponential one simply keeps halving.

**These constants are not measured.** They are a defensible starting point, in
exactly the sense `chunk_size_tokens` was before M8 replaced it with numbers.
Measuring them needs conversations long enough to have something worth
forgetting, which this project does not yet have.
"""

from __future__ import annotations

import enum
import math
import uuid
from dataclasses import dataclass
from datetime import datetime

HALF_LIFE_DAYS = 30.0
"""How long an unused memory takes to count half as much.

A month, chosen so that a fact from last week is nearly undiminished and one
from last quarter is faint but recoverable. Short enough that stale facts fade
before they mislead; long enough that a monthly process ("the quarterly report
goes to Finance") survives to its next occurrence.
"""

REINFORCEMENT = 0.15
"""How much of the remaining headroom a recall adds to importance.

Proportional (`+= R × (1 - importance)`) rather than additive, so importance
approaches 1 without ever needing a clamp to stay in range — and so an
already-trusted memory gains less from being recalled again than a new one does.
A fact recalled ten times is not ten times truer.
"""

FORGET_THRESHOLD = 0.05
"""Below this decayed score, a memory is not worth carrying.

Deliberately low. The asymmetry is the point: keeping a marginal memory costs a
few tokens on the recalls that happen to surface it, while deleting a real one
is unrecoverable. When the two errors have different costs, the threshold
belongs near the cheaper one.
"""


class MaintenanceAction(enum.StrEnum):
    """What a sweep decided about one memory."""

    KEEP = "keep"
    FORGET = "forget"


@dataclass(frozen=True)
class MaintenanceDecision:
    """One memory, its decayed score, and the verdict.

    Carries the score as well as the action so a sweep can be *read* before it
    is run. A maintenance job reporting only "deleted 412 memories" is one
    nobody can sanity-check, and the first time it deletes too much is the first
    time anyone finds out.
    """

    memory_id: uuid.UUID
    score: float
    action: MaintenanceAction


def recency_factor(last_accessed_at: datetime, now: datetime) -> float:
    """`0.5 ^ (age / half-life)`, clamped to [0, 1].

    A memory accessed in the future — clock skew between two processes, or a
    fixture written carelessly — would otherwise score *above* 1 and outrank
    everything real. Clamping is cheaper than trusting every clock in the system.

    Both arguments must be timezone-aware. Subtracting a naive datetime from an
    aware one raises, which is the correct outcome and the reason every timestamp
    column in this schema is `timezone=True`.
    """
    age_days = (now - last_accessed_at).total_seconds() / 86_400

    if age_days <= 0:
        return 1.0

    return min(1.0, math.pow(0.5, age_days / HALF_LIFE_DAYS))


def decay_score(importance: float, last_accessed_at: datetime, now: datetime) -> float:
    """How much this memory should still count, in [0, 1]."""
    return importance * recency_factor(last_accessed_at, now)


def reinforce(importance: float) -> float:
    """Strengthen a memory because it was just recalled.

    Recall is the only thing that raises importance, and that is a deliberate
    refusal of the obvious alternative: asking the model, at extraction time,
    how important the fact it just wrote is. That number has nothing behind it —
    models are not calibrated on their own output, and everything they extract
    looks important to them at the moment of writing.

    Use is evidence. A memory that keeps surfacing for real questions has
    demonstrated its worth in a way no self-assessment can.
    """
    return min(1.0, importance + REINFORCEMENT * (1.0 - importance))


def plan_maintenance(
    memories: list[tuple[uuid.UUID, float, datetime]], now: datetime
) -> list[MaintenanceDecision]:
    """Decide what to keep, given `(id, importance, last_accessed_at)` triples.

    Takes tuples rather than `Memory` objects on purpose: this is the layer that
    must stay testable without a database, and the sweep calling it reads only
    three columns rather than dragging a 1536-float embedding across the wire
    for every row it is about to consider deleting.

    There is no `SUMMARIZE` action, and its absence is a decision rather than an
    omission. Compressing several faded memories into one is the textbook next
    step, and it needs a model call whose output nothing here can check: a
    summary that silently drops the one qualifying clause produces a memory that
    is confident, compact and wrong — and it *replaces* the evidence that would
    have contradicted it. With no API key in this environment there is no way to
    measure how often that happens, and an unmeasured summariser is how a system
    starts misremembering on purpose.
    """
    decisions: list[MaintenanceDecision] = []

    for memory_id, importance, last_accessed_at in memories:
        score = decay_score(importance, last_accessed_at, now)
        action = MaintenanceAction.FORGET if score < FORGET_THRESHOLD else MaintenanceAction.KEEP
        decisions.append(MaintenanceDecision(memory_id=memory_id, score=score, action=action))

    return decisions
