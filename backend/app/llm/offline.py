"""An offline, deterministic model. Development and tests only.

Layer: llm. The counterpart to `HashingEmbedder` (ADR-0009), and it exists for
the same reason: there is no `ANTHROPIC_API_KEY` in this environment, and a test
suite that needs one is a test suite nobody runs.

What this is, precisely
-----------------------
It is **extractive, not generative**. It reads the numbered context blocks out
of the prompt, scores each against the question by word overlap, and returns the
best-matching sentence with its citation marker attached. It does not write
prose, and it must never be mistaken for something that does.

That narrowness is what makes it useful. An offline model returning a fixed
string ("This is a test answer.") would let every test pass while proving
nothing: the context could be empty, the citations wrong, the token budget
broken, and no assertion would move. This one *cannot* answer unless the right
chunk was actually retrieved and actually made it into the prompt — so a test
asserting the answer mentions expenses is genuinely asserting that retrieval,
budgeting and prompt assembly all worked.

The coupling, stated plainly
----------------------------
This module knows the shape of `app/prompts/rag/answer.md` — specifically that
context blocks begin with `[n]`. That is a real dependency between two files
that could drift apart silently, so `tests/unit/test_offline_llm.py` renders the
real template and asserts this parser finds its blocks. Change the template and
that test fails, which is the entire point of writing the coupling down.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from app.llm.base import Completion

QUESTION_LABEL = "Question:"
"""What `prompts/rag/answer.md` puts before the question.

Load-bearing in two places below, so it is named rather than spelled twice.
"""

_BLOCK = re.compile(
    rf"^\[(\d+)\]\s*(.*?)(?=^\[\d+\]|^{QUESTION_LABEL}|\Z)", re.MULTILINE | re.DOTALL
)
"""A context block: `[n]` at the start of a line, up to the next block, the
question, or the end of the prompt.

**Stopping at the question is not optional.** Without it the final block runs to
the end of the string and swallows the question line — which then scores a
perfect match against itself and wins every time. The result is an "answer" that
parrots the question back with a citation attached: fluent, well-formed,
completely wrong, and it passed the unit tests, because there the question's
words also appear in the right chunk. The integration test caught it.
"""

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z0-9]+")

NO_CONTEXT_ANSWER = "I could not find anything about that in your documents."
"""What to say when nothing was retrieved.

The single most important string in this file. Refusing is the correct answer to
a question the corpus cannot support, and a model that invents one instead is
worse than useless — it is confidently wrong, with a citation attached. The real
prompt instructs Claude to say the same thing; this makes the refusal path
testable without a key.
"""

_APPROXIMATE_CHARS_PER_TOKEN = 4
"""Only for the fake usage numbers below. Never used for a real budget — see
`app/rag/context.py`, which counts with tiktoken, because this ratio swings by a
factor of three between prose and a table of numbers."""


class OfflineLLM:
    """Answers by quoting the most relevant retrieved sentence."""

    def __init__(self, model: str = "offline-extractive") -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, *, system: str, prompt: str) -> Completion:
        text = self._answer(prompt)

        # Plausible rather than real. Nothing bills on these, and a test that
        # asserted an exact count here would only be asserting this arithmetic.
        return Completion(
            text=text,
            input_tokens=(len(system) + len(prompt)) // _APPROXIMATE_CHARS_PER_TOKEN,
            output_tokens=len(text) // _APPROXIMATE_CHARS_PER_TOKEN,
            stop_reason="end_turn",
        )

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        """Yield the same answer word by word.

        Chunked rather than sent whole, because a streaming endpoint that emits
        exactly one chunk does not exercise streaming at all — the event
        framing, the client-side reassembly and the terminating event would all
        go untested.
        """
        del system

        for index, word in enumerate(self._answer(prompt).split(" ")):
            yield word if index == 0 else f" {word}"

    def _answer(self, prompt: str) -> str:
        """Pick the sentence with the most words in common with the question."""
        blocks = _BLOCK.findall(prompt)

        if not blocks:
            return NO_CONTEXT_ANSWER

        question_words = self._words(self._question_of(prompt))
        best_score = 0.0
        best: tuple[str, str] | None = None

        for marker, body in blocks:
            # `SOURCE_TEMPLATE` is `[n] {title}\n{content}`, so the block's first
            # line is the document title. It has to go before sentence-splitting:
            # a filename has no terminating punctuation, so `_SENTENCE` cannot
            # separate it from the passage and it ends up quoted as part of the
            # answer — "handbook.pdf Expenses are reimbursed…".
            _, _, content = body.strip().partition("\n")

            for sentence in _SENTENCE.split(content.strip()):
                stripped = sentence.strip()

                if not stripped:
                    continue

                sentence_words = self._words(stripped)

                # Normalised by sentence length so a long sentence does not win
                # merely for containing more words — the same reason the
                # embeddings are unit vectors.
                score = len(question_words & sentence_words) / (len(sentence_words) or 1)

                if score > best_score:
                    best_score, best = score, (marker, stripped)

        if best is None:
            return NO_CONTEXT_ANSWER

        marker, sentence = best
        return f"{sentence} [{marker}]"

    @staticmethod
    def _question_of(prompt: str) -> str:
        """The last non-empty line, with the `Question:` label removed.

        The template puts the question after the context — the ordering
        long-context models follow most reliably. The label is stripped because
        "question" is not part of what was asked, and leaving it in would let
        the word match any chunk that happens to discuss questions.
        """
        lines = [line.strip() for line in prompt.splitlines() if line.strip()]

        if not lines:
            return ""

        return lines[-1].removeprefix(QUESTION_LABEL).strip()

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(_WORD.findall(text.lower()))
