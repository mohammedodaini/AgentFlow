"""Forgetting, parsing, and the pure decisions around memory (M10).

Everything here runs without a database or a model, which is exactly why these
functions were separated out. A decay policy tested only through an integration
test is a policy nobody can reason about — and arithmetic is where a forgetting
system is most likely to be quietly wrong.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.memory.policies import (
    FORGET_THRESHOLD,
    HALF_LIFE_DAYS,
    MaintenanceAction,
    decay_score,
    plan_maintenance,
    recency_factor,
    reinforce,
)
from app.memory.writer import MAX_FACTS, NO_FACTS, content_hash, normalize, parse_facts
from app.models.memory import MAX_MEMORY_CHARS

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# decay
# --------------------------------------------------------------------------


def test_a_memory_used_today_is_undiminished() -> None:
    assert recency_factor(NOW, NOW) == pytest.approx(1.0)


def test_one_half_life_halves_a_memory() -> None:
    """The constant means what its name says. If this drifts, every recall
    ranking shifts and nothing else would notice."""
    older = NOW - timedelta(days=HALF_LIFE_DAYS)

    assert recency_factor(older, NOW) == pytest.approx(0.5)


def test_a_future_timestamp_cannot_score_above_one() -> None:
    """Two processes, two clocks. Without the clamp, a memory written by a host
    running a few seconds fast would score above 1.0 and outrank every real
    memory — a ranking bug caused by NTP."""
    assert recency_factor(NOW + timedelta(hours=1), NOW) == 1.0


def test_decay_multiplies_importance_by_recency() -> None:
    assert decay_score(0.8, NOW - timedelta(days=HALF_LIFE_DAYS), NOW) == pytest.approx(0.4)


def test_a_very_old_memory_decays_toward_nothing() -> None:
    assert decay_score(1.0, NOW - timedelta(days=365), NOW) < FORGET_THRESHOLD


# --------------------------------------------------------------------------
# reinforcement
# --------------------------------------------------------------------------


def test_recall_strengthens_a_memory() -> None:
    assert reinforce(0.5) > 0.5


def test_importance_approaches_one_without_ever_exceeding_it() -> None:
    """Proportional reinforcement is what makes the `[0, 1]` check constraint
    unreachable rather than merely enforced. An additive `+= 0.15` would
    eventually try to store 1.05 and raise an `IntegrityError` inside a
    background worker."""
    importance = 0.5

    for _ in range(200):
        importance = reinforce(importance)

    assert importance <= 1.0


def test_a_trusted_memory_gains_less_than_a_new_one() -> None:
    """A fact recalled ten times is not ten times truer."""
    assert reinforce(0.9) - 0.9 < reinforce(0.1) - 0.1


# --------------------------------------------------------------------------
# maintenance
# --------------------------------------------------------------------------


def test_a_fresh_important_memory_is_kept() -> None:
    decisions = plan_maintenance([(uuid.uuid4(), 0.9, NOW)], NOW)

    assert decisions[0].action is MaintenanceAction.KEEP


def test_a_faded_memory_is_forgotten() -> None:
    decisions = plan_maintenance([(uuid.uuid4(), 0.2, NOW - timedelta(days=400))], NOW)

    assert decisions[0].action is MaintenanceAction.FORGET


def test_every_decision_carries_the_score_that_produced_it() -> None:
    """A sweep reporting only "deleted 412 memories" is one nobody can
    sanity-check before running it."""
    memory_id = uuid.uuid4()

    decisions = plan_maintenance([(memory_id, 0.5, NOW - timedelta(days=HALF_LIFE_DAYS))], NOW)

    assert decisions[0].memory_id == memory_id
    assert decisions[0].score == pytest.approx(0.25)


def test_an_empty_sweep_decides_nothing() -> None:
    assert plan_maintenance([], NOW) == []


# --------------------------------------------------------------------------
# normalisation and hashing
# --------------------------------------------------------------------------


def test_normalisation_collapses_whitespace_and_a_trailing_stop() -> None:
    assert normalize("  Invoices   go\nto Finance.  ") == "Invoices go to Finance"


def test_the_hash_ignores_case_and_punctuation() -> None:
    """What makes the unique constraint mean "the same fact" rather than "the
    same bytes". Without it, re-extracting a fact that has a full stop this time
    writes a second row the constraint happily accepts."""
    assert content_hash("Invoices go to Finance.") == content_hash("  invoices go to finance ")


def test_different_facts_hash_differently() -> None:
    assert content_hash("Invoices go to Finance") != content_hash("Invoices go to Legal")


def test_stored_text_keeps_its_capitalisation() -> None:
    """Only the hash is lowercased. "Finance" reads better in a prompt than
    "finance", and the prompt is what this text exists for."""
    assert normalize("Invoices go to Finance.") == "Invoices go to Finance"


# --------------------------------------------------------------------------
# parsing what a model returned
# --------------------------------------------------------------------------


def test_bullet_lines_become_facts() -> None:
    assert parse_facts("- Works in Berlin\n- Approves invoices") == [
        "Works in Berlin",
        "Approves invoices",
    ]


def test_the_no_facts_token_yields_nothing() -> None:
    assert parse_facts(NO_FACTS) == []


def test_prose_yields_nothing_at_all() -> None:
    """The safety property of this whole path.

    A model that ignores the format — or an offline provider that cannot follow
    it — must produce *zero* memories, not one enormous malformed one. Strict
    parsing is what makes running extraction unattended defensible.
    """
    assert parse_facts("Sure! Here are some things I learned about this person.") == []


def test_a_preamble_before_the_bullets_is_discarded() -> None:
    """Models add conversational padding even when told not to. The bullets stay
    usable; the padding must not become a memory."""
    assert parse_facts("Here is what I found:\n\n- Works in Berlin") == ["Works in Berlin"]


def test_too_many_facts_are_capped() -> None:
    """A prompt is a request; this is a bound. A model returning forty facts from
    one exchange has misunderstood the task."""
    reply = "\n".join(f"- fact number {index}" for index in range(20))

    assert len(parse_facts(reply)) == MAX_FACTS


def test_an_overlong_fact_is_dropped_rather_than_truncated() -> None:
    """A truncated fact is not a shorter fact — it is a sentence missing its
    qualifying clause, which is how "reimbursed at 45p per mile, up to 10,000
    miles" becomes a memory asserting something false."""
    reply = f"- {'x' * (MAX_MEMORY_CHARS + 1)}\n- Works in Berlin"

    assert parse_facts(reply) == ["Works in Berlin"]
