"""Language models — the public surface of the package.

Layer: llm (infrastructure). Nothing outside should import `app.llm.anthropic`
or `app.llm.offline` directly; the whole point of the seam is that call sites
name the protocol and never the vendor.

`create_llm` is a builder in the same shape as `create_storage`,
`create_embedder` and `create_engine`, for the same reason: two processes each
need one, and neither can borrow the other's. The API builds one in
`lifespan()`; from M9 the worker will build its own in `on_startup`.

See ADR-0010 for why the boundary exists at all.
"""

from __future__ import annotations

import structlog
from fastapi import Request

from app.core.config import Settings
from app.llm.anthropic import AnthropicLLM
from app.llm.base import Completion, LLMError, LLMProvider
from app.llm.offline import NO_CONTEXT_ANSWER, OfflineLLM

__all__ = [
    "NO_CONTEXT_ANSWER",
    "AnthropicLLM",
    "Completion",
    "LLMError",
    "LLMProvider",
    "OfflineLLM",
    "create_llm",
    "get_llm",
]

logger = structlog.get_logger(__name__)


def create_llm(settings: Settings) -> LLMProvider:
    """Build the provider named by configuration.

    Returns the protocol rather than the concrete class, so the type checker
    stops a call site reaching for something only one implementation has — the
    same reasoning as `create_storage`.
    """
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings)

    logger.warning(
        "llm.using_offline_provider",
        detail="extractive quoting, not generation; set LLM_PROVIDER=anthropic for real answers",
    )
    return OfflineLLM()


def get_llm(request: Request) -> LLMProvider:
    """Read the provider `lifespan()` stored on the application."""
    provider: LLMProvider = request.app.state.llm
    return provider
