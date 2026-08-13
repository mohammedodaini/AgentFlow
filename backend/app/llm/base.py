"""What the application needs from a large language model.

Layer: llm (infrastructure). The interface only — implementations live beside
this file, and nothing outside the package should import them directly.

This is the third seam in the project with the same shape, after `ObjectStorage`
(ADR-0007) and `EmbeddingProvider` (ADR-0009), and by now the pattern is
deliberate rather than incidental: an external dependency that cannot run on a
laptop gets a protocol, a real implementation, and an offline one that is honest
about being offline.

Why a top-level package rather than `app/rag/generation.py`
-----------------------------------------------------------
Because generation is not retrieval, and four different parts of this system
need a model: RAG answers (M7), LLM-as-judge scoring (M8), the agent's own
reasoning (M9), and memory extraction (M10). A client living inside `rag/`
would make three of those four import from a package they have nothing to do
with — and the first person to notice would fix it by writing a second client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from app.core.exceptions import AppError


class LLMError(AppError):
    """The model could not produce an answer.

    A distinct type because the honest HTTP answer is different: this is an
    upstream failure, not ours, so `app/api/errors.py` maps it to **502 Bad
    Gateway** rather than letting it fall through to 500. A client that sees
    502 knows retrying may work; 500 says nothing useful.

    The status lives in the central map, not on this class — services must not
    know about HTTP (see `app/core/exceptions.py`), and this exception is also
    raised inside the arq worker and, from M9, inside agent tools.
    """

    default_code: ClassVar[str] = "llm_unavailable"


@dataclass(frozen=True)
class Completion:
    """One model response, plus what it cost.

    Frozen because a completion is a record of something that already happened.

    The token counts are not decoration. They are the only honest input to the
    two questions that get asked about an AI product in production — "why is
    the bill this size?" and "which feature is expensive?" — and they have to be
    captured at the boundary where the provider reports them. Reconstructing
    them later from stored text means re-tokenising with a counter that is not
    the model's own, and being quietly wrong forever.

    M12 turns these into per-run cost accounting. Capturing them from the first
    call means that milestone is a query, not an archaeology project.
    """

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None

    @property
    def was_truncated(self) -> bool:
        """True when the model stopped because it ran out of room.

        Worth asking explicitly, because a truncated answer is not a failure
        anything raises: it is a fluent, well-formed response that simply stops
        mid-sentence, and the user reads it as the whole answer.
        """
        return self.stop_reason == "max_tokens"


@runtime_checkable
class LLMProvider(Protocol):
    """A text-in, text-out model.

    Deliberately narrow. It takes a system prompt and a user message and returns
    text — no tool calling, no message history, no structured output. Those all
    arrive with LangGraph at M9, which brings its own abstraction, and inventing
    a worse version of it here would mean maintaining two.

    `runtime_checkable` so a provider missing `stream` fails in a test rather
    than three layers into a request.
    """

    @property
    def model(self) -> str:
        """The model identifier, for logging and cost attribution."""
        ...

    async def complete(self, *, system: str, prompt: str) -> Completion:
        """Answer once, in full."""
        ...

    def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        """Answer incrementally, yielding text as it arrives.

        Not `async def`, deliberately. An `async def` returning an async
        iterator would require callers to write `await provider.stream(...)`
        before iterating, and forgetting the `await` yields a coroutine that
        silently never runs. Declared this way, `async for chunk in
        provider.stream(...)` is the only thing that type-checks.
        """
        ...
