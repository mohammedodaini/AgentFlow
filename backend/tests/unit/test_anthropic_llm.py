"""The Anthropic client — our half of it (M7).

The same narrow claim as the OpenAI embedder tests at M6, and worth restating
because it is easy to overclaim. These tests cannot say whether Claude writes
good answers; that needs a key, a network and money. They can say whether *our*
code reads the response correctly, captures what it cost, and turns a vendor
failure into something the rest of the application understands.

All three are ours to get wrong, and two fail silently: text extracted from the
wrong place yields an empty answer, and usage never captured is a bill nobody
can explain.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import pytest
from anthropic import APIConnectionError
from pydantic import SecretStr

from app.core.config import get_settings
from app.llm.anthropic import AnthropicLLM
from app.llm.base import LLMError, LLMProvider


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int = 11
    output_tokens: int = 7


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage = field(default_factory=_Usage)
    stop_reason: str | None = "end_turn"


async def _aiter(pieces: list[str]) -> AsyncIterator[str]:
    for piece in pieces:
        yield piece


class _Stream:
    """The async context manager `client.messages.stream()` returns."""

    def __init__(self, pieces: list[str]) -> None:
        self.text_stream = _aiter(pieces)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeMessages:
    """Stands in for `client.messages`, recording what it was asked.

    Only the network boundary is replaced. The `AnthropicLLM` under test is
    real — its block joining, its usage capture and its error folding all run,
    which is what makes these assertions mean anything.
    """

    def __init__(
        self,
        *,
        response: _Response | None = None,
        pieces: list[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._response = response or _Response(content=[_Block("Expenses are monthly. [1]")])
        self._pieces = pieces or ["Expenses ", "are ", "monthly."]
        self._error = error

    async def create(self, **kwargs: object) -> _Response:
        self.calls.append(kwargs)

        if self._error:
            raise self._error

        return self._response

    def stream(self, **kwargs: object) -> _Stream:
        self.calls.append(kwargs)

        if self._error:
            raise self._error

        return _Stream(self._pieces)


def anthropic_llm(fake: FakeMessages, **overrides: object) -> AnthropicLLM:
    settings = get_settings().model_copy(
        update={
            "llm_provider": "anthropic",
            "anthropic_api_key": SecretStr("sk-ant-test-not-a-real-key"),
            **overrides,
        }
    )
    llm = AnthropicLLM(settings)
    llm._client.messages = fake  # type: ignore[assignment, misc] # noqa: SLF001
    return llm


# --------------------------------------------------------------------------
# reading the response
# --------------------------------------------------------------------------


def test_it_satisfies_the_protocol() -> None:
    """Conformance as a test rather than an `AttributeError` three layers into
    a request."""
    assert isinstance(anthropic_llm(FakeMessages()), LLMProvider)


async def test_the_answer_text_is_extracted_from_the_content_blocks() -> None:
    """`content` is a list of blocks, not a string. Indexing `[0]` works today
    and breaks the day a block of another kind arrives first."""
    fake = FakeMessages(
        response=_Response(content=[_Block("Expenses "), _Block("are monthly. [1]")])
    )

    completion = await anthropic_llm(fake).complete(system="s", prompt="p")

    assert completion.text == "Expenses are monthly. [1]"


async def test_non_text_blocks_are_skipped_rather_than_crashing() -> None:
    """A thinking or tool-use block must not become part of the answer, and
    must not raise either. Joining the text blocks does both."""
    fake = FakeMessages(
        response=_Response(content=[_Block("internal", type="thinking"), _Block("The answer. [1]")])
    )

    completion = await anthropic_llm(fake).complete(system="s", prompt="p")

    assert completion.text == "The answer. [1]"


async def test_usage_is_captured_from_the_provider() -> None:
    """Not re-derived later with a tokeniser that is not the model's own. M12
    bills on these, and the alternative is being quietly wrong forever."""
    completion = await anthropic_llm(FakeMessages()).complete(system="s", prompt="p")

    assert completion.input_tokens == 11
    assert completion.output_tokens == 7


async def test_a_max_tokens_stop_reason_is_reported_as_truncated() -> None:
    """The only signal that a fluent, well-formed answer stopped mid-sentence.
    Nothing raises, and the text reads as finished."""
    fake = FakeMessages(
        response=_Response(content=[_Block("It depends on")], stop_reason="max_tokens")
    )

    completion = await anthropic_llm(fake).complete(system="s", prompt="p")

    assert completion.was_truncated


# --------------------------------------------------------------------------
# what we send
# --------------------------------------------------------------------------


async def test_the_configured_model_and_limits_are_sent() -> None:
    """Temperature zero especially: a RAG answer is faithful summarisation, and
    every degree of sampling randomness is a chance to drift from the retrieved
    text."""
    fake = FakeMessages()

    await anthropic_llm(fake, llm_model="claude-sonnet-5", llm_max_tokens=512).complete(
        system="the rules", prompt="the question"
    )

    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5"
    assert call["max_tokens"] == 512
    assert call["temperature"] == 0.0
    assert call["system"] == "the rules"
    assert call["messages"] == [{"role": "user", "content": "the question"}]


def test_the_model_property_reports_what_will_be_called() -> None:
    """Returned in the API response for attribution: an answer nobody can
    attribute to a model version is an answer nobody can reproduce."""
    assert anthropic_llm(FakeMessages(), llm_model="claude-sonnet-5").model == "claude-sonnet-5"


# --------------------------------------------------------------------------
# failure
# --------------------------------------------------------------------------


async def test_a_provider_failure_becomes_an_llm_error() -> None:
    """The vendor's exception hierarchy must not reach the HTTP layer.

    `LLMError` is what `app/api/errors.py` maps to 502 — and 502 rather than
    500 is the actionable half: it says an upstream is down, so retrying may
    work, and it keeps our bugs and Anthropic's outages apart in the metrics.
    """
    fake = FakeMessages(error=APIConnectionError(request=None))  # type: ignore[arg-type]

    with pytest.raises(LLMError) as raised:
        await anthropic_llm(fake).complete(system="s", prompt="p")

    assert raised.value.code == "llm_unavailable"
    assert "unavailable" in raised.value.message


async def test_the_error_message_leaks_nothing_about_the_provider() -> None:
    """It is rendered to an end user. Which model we use, and how it failed,
    are operator concerns and live in the log line instead."""
    fake = FakeMessages(error=APIConnectionError(request=None))  # type: ignore[arg-type]

    with pytest.raises(LLMError) as raised:
        await anthropic_llm(fake).complete(system="s", prompt="p")

    assert "anthropic" not in raised.value.message.lower()
    assert "claude" not in raised.value.message.lower()


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


async def test_streaming_yields_each_piece_as_it_arrives() -> None:
    llm = anthropic_llm(FakeMessages(pieces=["Expenses ", "are ", "monthly."]))

    pieces = [piece async for piece in llm.stream(system="s", prompt="p")]

    assert pieces == ["Expenses ", "are ", "monthly."]


async def test_a_streaming_failure_becomes_an_llm_error() -> None:
    """A failure *before* the first byte can still become a 502. One after it
    cannot — which is why the route also emits an explicit `error` event."""
    fake = FakeMessages(error=APIConnectionError(request=None))  # type: ignore[arg-type]
    llm = anthropic_llm(fake)

    with pytest.raises(LLMError):
        [piece async for piece in llm.stream(system="s", prompt="p")]
