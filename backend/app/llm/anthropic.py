"""The real model client. Needs `ANTHROPIC_API_KEY`.

Layer: llm. The only module in the project that imports the `anthropic` SDK —
which is the point of the seam, and is worth checking with grep the day someone
is tempted to "just call the API here".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog
from anthropic import AnthropicError, AsyncAnthropic

from app.core.config import Settings
from app.llm.base import Completion, LLMError

logger = structlog.get_logger(__name__)

_UNAVAILABLE = "The language model is unavailable. Please try again."
"""Written for the person who sees it in a browser, not for a log reader. It
says what happened and what to do, and deliberately leaks nothing about which
provider we use or how it failed."""


class AnthropicLLM:
    """Claude, via the official SDK.

    Holds one `AsyncAnthropic` client for the life of the process. The client
    owns an HTTP connection pool, so building one per request would open and
    discard a pool per question — the same reason `OpenAIEmbedder` is built once
    in `lifespan()` rather than per call.
    """

    def __init__(self, settings: Settings) -> None:
        self._model = settings.llm_model
        self._max_tokens = settings.llm_max_tokens
        self._temperature = settings.llm_temperature
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
        )

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, *, system: str, prompt: str) -> Completion:
        """One request, one answer.

        Every SDK failure is folded into `LLMError`. That is not carelessness
        about error types: the caller's options are identical for a rate limit,
        a timeout and a 500 — say so and stop — and the detail that would
        distinguish them is already in the log line below. Leaking
        `anthropic.RateLimitError` to a route would put the vendor's exception
        hierarchy into our HTTP layer, which is exactly what this package exists
        to prevent.
        """
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except AnthropicError as error:
            logger.warning("llm.request_failed", model=self._model, error=str(error))
            raise LLMError(_UNAVAILABLE) from error

        # `content` is a list of blocks, not a string. Today a text-only request
        # returns exactly one text block, but joining rather than indexing `[0]`
        # means that when a block of another kind appears this returns the prose
        # rather than raising an IndexError or silently dropping half the answer.
        text = "".join(block.text for block in response.content if block.type == "text")

        logger.info(
            "llm.completed",
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )

        return Completion(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        """Yield the answer as it is generated.

        Streaming is a latency illusion, and a valuable one: the total time to a
        complete answer is unchanged, but time-to-first-token drops from several
        seconds to a few hundred milliseconds, and a user watching words appear
        does not experience the wait as a hang.

        The cost is that **the response has already begun when a failure
        happens**. Once the first token is on the wire the status code is sent
        and cannot be taken back, so a mid-stream error can never become a 502.
        The route handles that by emitting an explicit `error` event; here the
        exception simply propagates to it.
        """
        try:
            async with self._client.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except AnthropicError as error:
            logger.warning("llm.stream_failed", model=self._model, error=str(error))
            raise LLMError(_UNAVAILABLE) from error
