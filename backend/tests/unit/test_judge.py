"""The LLM-as-judge, and its offline counterpart (M8).

Most of what follows tests the *parsing*, not the judging, and that is the right
emphasis. Whether a model applies a rubric well cannot be tested without a key,
and would not be deterministic if it could. What can be tested is everything
around it: that a malformed verdict fails in the safe direction, that an
out-of-range score is coerced rather than discarded, and that a judge outage
does not quietly vanish from the average.

That last one is the failure worth naming. Skipping an example whose judge call
failed *raises* the run's score, because the remaining examples are the ones
that worked. An evaluation harness must never make a broken run look better than
a working one.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from app.core.config import get_settings
from app.evaluation.judge import (
    MAX_SCORE,
    MIN_SCORE,
    HeuristicJudge,
    Judge,
    JudgeScore,
    LLMJudge,
    create_judge,
)
from app.llm.base import Completion, LLMError
from app.llm.offline import OfflineLLM

SOURCES = "[1] Expenses are reimbursed monthly, provided a receipt is attached."
QUESTION = "How often are expenses reimbursed?"
GOOD_ANSWER = "Expenses are reimbursed monthly when a receipt is attached. [1]"
REFERENCE = "Expenses are reimbursed monthly, provided a receipt is attached."


class ReplyingLLM:
    """Returns a fixed reply, or raises. Stands in for the model only."""

    model = "fake-judge"

    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error

    async def complete(self, *, system: str, prompt: str) -> Completion:
        del system, prompt

        if self._error:
            raise self._error

        return Completion(text=self._reply, input_tokens=1, output_tokens=1)

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        del system, prompt
        raise NotImplementedError
        yield ""  # pragma: no cover — makes this an async generator


async def verdict(reply: str) -> JudgeScore:
    return await LLMJudge(ReplyingLLM(reply)).score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )


# --------------------------------------------------------------------------
# parsing a verdict
# --------------------------------------------------------------------------


async def test_a_clean_json_verdict_is_read() -> None:
    score = await verdict('{"faithfulness": 5, "relevance": 4, "completeness": 3, "notes": "ok"}')

    assert (score.faithfulness, score.relevance, score.completeness) == (5, 4, 3)
    assert score.notes == "ok"
    assert score.mean == pytest.approx(4.0)


async def test_json_wrapped_in_prose_is_still_read() -> None:
    """Models preface JSON with an explanation or fence it in a code block often
    enough that a strict parse would reject perfectly usable verdicts — and a
    parse failure costs the run a point."""
    score = await verdict(
        'Here is my assessment:\n```json\n{"faithfulness": 4, "relevance": 4, '
        '"completeness": 4, "notes": "fine"}\n```\nHope that helps.'
    )

    assert score.faithfulness == 4


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("I think it was pretty good, honestly.", id="no-json-at-all"),
        pytest.param("{faithfulness: 5, relevance: 5}", id="not-valid-json"),
        pytest.param("", id="empty-reply"),
    ],
)
async def test_an_unparseable_verdict_scores_the_minimum(reply: str) -> None:
    """Fails low, not middling, and never skipped.

    Skipping drops the example from the average, which *raises* the score of a
    run whose answers broke the judge. A middling default does the same thing
    more quietly. Failing low makes a malformed run look bad, which is the
    direction an evaluation harness should err in.
    """
    score = await verdict(reply)

    assert score.faithfulness == MIN_SCORE
    assert score.relevance == MIN_SCORE
    assert score.completeness == MIN_SCORE
    assert score.notes


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        pytest.param(0, MIN_SCORE, id="below-the-scale"),
        pytest.param(9, MAX_SCORE, id="above-the-scale"),
        pytest.param("4", 4, id="a-string"),
        pytest.param(4.7, 4, id="a-float"),
        pytest.param(None, MIN_SCORE, id="missing"),
        pytest.param("excellent", MIN_SCORE, id="a-word"),
    ],
)
async def test_scores_outside_the_rubric_are_coerced_into_it(
    reported: object, expected: int
) -> None:
    """Models return 0, 6, "4" and 4.5. Clamping keeps one eccentric field from
    discarding an otherwise valid verdict — the number was still a judgement,
    merely expressed outside the scale it was given."""
    score = await verdict(
        f'{{"faithfulness": {json.dumps(reported)}, "relevance": 3, "completeness": 3}}'
    )

    assert score.faithfulness == expected


async def test_a_judge_outage_scores_the_minimum_rather_than_vanishing() -> None:
    """An unavailable judge must not silently improve the run's average."""
    score = await LLMJudge(ReplyingLLM(error=LLMError("down"))).score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )

    assert score.faithfulness == MIN_SCORE
    assert "llm_unavailable" in score.notes


# --------------------------------------------------------------------------
# the heuristic judge
# --------------------------------------------------------------------------


async def test_the_heuristic_judge_separates_grounded_from_invented() -> None:
    """It cannot detect a fluent misstatement — that is what the real judge is
    for. What it can do is score an answer built from words appearing nowhere in
    its sources below one that quotes them, which is enough to make the runner
    and the regression gate testable with no key."""
    judge = HeuristicJudge()

    grounded = await judge.score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )
    invented = await judge.score(
        question=QUESTION,
        answer="Reimbursement happens quarterly through the pension portal.",
        sources=SOURCES,
        reference=REFERENCE,
    )

    assert grounded.faithfulness > invented.faithfulness
    assert grounded.completeness > invented.completeness


async def test_the_heuristic_judge_scores_an_off_topic_answer_as_irrelevant() -> None:
    judge = HeuristicJudge()

    on_topic = await judge.score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )
    off_topic = await judge.score(
        question=QUESTION,
        answer="The office plants are watered on Tuesdays.",
        sources=SOURCES,
        reference=REFERENCE,
    )

    assert on_topic.relevance > off_topic.relevance


async def test_an_empty_answer_scores_the_minimum() -> None:
    score = await HeuristicJudge().score(
        question=QUESTION, answer="", sources=SOURCES, reference=REFERENCE
    )

    assert score.mean == MIN_SCORE


async def test_heuristic_scores_stay_inside_the_rubric() -> None:
    """So a report can print heuristic and model scores in one column without a
    second column explaining which scale each used."""
    score = await HeuristicJudge().score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )

    for value in (score.faithfulness, score.relevance, score.completeness):
        assert MIN_SCORE <= value <= MAX_SCORE


async def test_the_heuristic_judge_is_deterministic() -> None:
    """Without this, every eval comparison would measure sampling noise as often
    as the change under test."""
    judge = HeuristicJudge()

    first = await judge.score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )
    second = await judge.score(
        question=QUESTION, answer=GOOD_ANSWER, sources=SOURCES, reference=REFERENCE
    )

    assert first == second


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def test_the_heuristic_judge_is_the_offline_default() -> None:
    """So `make eval` runs on a fresh clone with no key."""
    judge = create_judge(get_settings(), OfflineLLM())

    assert isinstance(judge, HeuristicJudge)
    assert isinstance(judge, Judge)


def test_the_real_judge_is_selected_with_a_real_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tied to `llm_provider` rather than a switch of its own: a real judge
    needs a real model, so the two can never sensibly disagree."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    get_settings.cache_clear()

    assert isinstance(create_judge(get_settings(), OfflineLLM()), LLMJudge)
