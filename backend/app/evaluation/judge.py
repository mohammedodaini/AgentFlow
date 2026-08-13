"""Scoring the things code cannot measure: LLM-as-judge.

Layer: evaluation. Deliberately the *second* half of M8. `metrics.py` runs first
because it is free, instant and reproducible; this exists only for what those
cannot see — whether an answer is faithful to its sources, whether it addresses
the question actually asked, and whether it left something out.

Two implementations behind one protocol, the same shape as every other external
dependency in this project (ADR-0007, ADR-0009, ADR-0010). `LLMJudge` asks a
model. `HeuristicJudge` computes the same three numbers from word overlap, so
the harness runs with no API key — and so the runner, the report and the
regression gate are all testable offline.

Known biases, and what is done about them
-----------------------------------------
An LLM judge is a measuring instrument with documented faults, and using one
without naming them is how a team ships a number it trusts more than it should.

- **Verbosity bias.** Longer answers score higher, roughly regardless of
  content. Mitigated by scoring completeness against a *reference answer*
  rather than the judge's own sense of thoroughness, so length beyond the
  reference buys nothing.
- **Position bias.** Whichever text appears first is favoured. Mitigated by
  never asking for a comparison between two candidate answers — each answer is
  scored alone, against fixed criteria.
- **Self-preference.** A model rates its own output above another's. Not
  mitigated, and cannot be while the judge and the generator are the same
  model. Recorded because the honest response is to read judge scores as a
  *relative* signal between runs, never as an absolute quality figure.

The rubric is 1–5 rather than 1–10 because models do not use the middle of a
ten-point scale consistently, and the extra resolution is noise dressed as
precision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

from app.core.config import Settings
from app.llm.base import LLMError, LLMProvider
from app.prompts import loader as prompts

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = "evaluation/system"
JUDGE_PROMPT = "evaluation/judge"

MIN_SCORE = 1
MAX_SCORE = 5

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "you",
        "your",
        "do",
        "does",
        "can",
        "could",
        "should",
        "would",
        "my",
        "me",
    ]
)
"""Common words carry no signal about faithfulness — every answer contains them,
so counting them drags every score toward the same middling number."""


@dataclass(frozen=True)
class JudgeScore:
    """One answer's three scores, each 1–5.

    Three numbers rather than one, because they fail independently and the fix
    differs for each. A faithful but incomplete answer needs more context; a
    complete but unfaithful one needs a stricter prompt; an irrelevant one needs
    better retrieval. Collapsing them into a single "quality" figure discards
    the only part that says what to do next.
    """

    faithfulness: int
    """Is every claim supported by the sources? The one that matters most — an
    unfaithful answer is a confident invention with a citation attached."""

    relevance: int
    """Does it answer the question that was actually asked?"""

    completeness: int
    """Does it cover what the reference answer covers?"""

    notes: str = ""
    """Why. Kept because a score with no reasoning is unarguable, and an eval
    nobody can argue with is an eval nobody acts on."""

    @property
    def mean(self) -> float:
        return (self.faithfulness + self.relevance + self.completeness) / 3


@runtime_checkable
class Judge(Protocol):
    """Scores one answer against its sources and a reference."""

    async def score(
        self, *, question: str, answer: str, sources: str, reference: str
    ) -> JudgeScore: ...


class LLMJudge:
    """Asks a model to apply the rubric. Needs a real provider."""

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def score(
        self, *, question: str, answer: str, sources: str, reference: str
    ) -> JudgeScore:
        """Render the rubric prompt, call the model, parse the verdict.

        A judge that cannot be parsed scores 1 — not 3, and not "skip".
        Skipping silently drops the example from the average, which *raises* the
        score of a run whose answers broke the judge; a middling default does
        the same thing more quietly. Failing low makes a malformed run look bad,
        which is the direction an evaluation harness should err in.
        """
        prompt = prompts.render(
            JUDGE_PROMPT,
            question=question,
            answer=answer,
            sources=sources,
            reference=reference or "(no reference answer was provided)",
        )

        try:
            completion = await self._llm.complete(
                system=prompts.load_prompt(SYSTEM_PROMPT), prompt=prompt
            )
        except LLMError as error:
            logger.warning("judge.call_failed", error=error.message)
            return JudgeScore(MIN_SCORE, MIN_SCORE, MIN_SCORE, notes=f"judge failed: {error.code}")

        return _parse(completion.text)


class HeuristicJudge:
    """Deterministic scoring from word overlap. Development and tests only.

    Not a model, and not pretending to be. It cannot detect a fluent
    misstatement, a subtly wrong number, or an answer that contradicts its own
    citation — all of which are exactly why the LLM judge exists.

    What it *can* do is give the runner, the report and the regression gate
    something real to run against with no key: an answer built from words that
    never appear in its sources genuinely scores low here, and an answer that
    ignores the question genuinely scores low on relevance. That makes the
    harness testable, which is the whole reason it exists.
    """

    async def score(
        self, *, question: str, answer: str, sources: str, reference: str
    ) -> JudgeScore:
        answer_words = _content_words(answer)

        if not answer_words:
            return JudgeScore(MIN_SCORE, MIN_SCORE, MIN_SCORE, notes="empty answer")

        return JudgeScore(
            faithfulness=_scale(_covered(answer_words, _content_words(sources))),
            relevance=_scale(_covered(_content_words(question), answer_words)),
            completeness=_scale(_covered(_content_words(reference), answer_words)),
            notes="heuristic: word overlap, not meaning",
        )


def create_judge(settings: Settings, llm: LLMProvider) -> Judge:
    """Build the judge matching the configured provider.

    Tied to `llm_provider` rather than given a setting of its own: a real judge
    needs a real model, so the two can never sensibly disagree, and a second
    switch would only create a state where the eval quietly uses the heuristic
    while the report implies otherwise.
    """
    if settings.llm_provider == "anthropic":
        return LLMJudge(llm)

    logger.warning(
        "judge.using_heuristic",
        detail="word overlap, not meaning; set LLM_PROVIDER=anthropic for real judging",
    )
    return HeuristicJudge()


def _parse(text: str) -> JudgeScore:
    """Pull the verdict out of the model's reply.

    The JSON is taken from the first `{` to the last `}` rather than parsed from
    the whole string. Models wrap JSON in prose or a fenced code block often
    enough that a strict parse would fail on replies that are perfectly usable —
    and, per `score` above, a parse failure costs the run a point.
    """
    start, end = text.find("{"), text.rfind("}")

    if start == -1 or end <= start:
        logger.warning("judge.unparseable", reply=text[:200])
        return JudgeScore(MIN_SCORE, MIN_SCORE, MIN_SCORE, notes="unparseable judge reply")

    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("judge.invalid_json", reply=text[:200])
        return JudgeScore(MIN_SCORE, MIN_SCORE, MIN_SCORE, notes="invalid JSON from judge")

    return JudgeScore(
        faithfulness=_clamp(raw.get("faithfulness")),
        relevance=_clamp(raw.get("relevance")),
        completeness=_clamp(raw.get("completeness")),
        notes=str(raw.get("notes", "")),
    )


def _clamp(value: object) -> int:
    """Force a reported score into the rubric.

    Models return 0, 6, "4", and 4.5. Clamping rather than rejecting keeps one
    eccentric field from discarding an otherwise valid verdict — the number was
    still a judgement, merely expressed outside the scale it was given.
    """
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MIN_SCORE

    return max(MIN_SCORE, min(MAX_SCORE, number))


def _content_words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def _covered(needles: set[str], haystack: set[str]) -> float:
    """What fraction of `needles` appear in `haystack`. Empty needles score 1.0
    — nothing was required, so nothing is missing."""
    if not needles:
        return 1.0

    return len(needles & haystack) / len(needles)


def _scale(fraction: float) -> int:
    """Map [0, 1] onto the 1–5 rubric.

    1 at zero coverage, 5 at full coverage, evenly spaced between — so the
    heuristic's numbers read on the same scale as the model's, without a second
    column in the report explaining which is which.
    """
    return MIN_SCORE + round(fraction * (MAX_SCORE - MIN_SCORE))
