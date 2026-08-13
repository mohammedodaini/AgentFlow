"""`/ask` request and response shapes (M7).

Layer: schemas — the API boundary.

Separate from `schemas/document.py` because these describe an *action*, not a
resource. `DocumentRead` is a view of a row; `AskResponse` is the result of
something that happened once, cost money, and will never exist again. Filing
them together would be the first step toward an `AskResponse` that quietly grows
document fields nobody asked for.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.repositories.chunk_repository import MAX_TOP_K
from app.schemas.common import APIModel


class AskRequest(BaseModel):
    """A question to answer from this organization's documents.

    Deliberately the same shape as `SearchRequest`. The two endpoints answer the
    same question at different levels of processing — "which passages?" and
    "what is the answer?" — and a client moving from one to the other should not
    have to rewrite its request.
    """

    query: str = Field(min_length=1, max_length=2000, description="Natural-language question")
    top_k: int = Field(
        default=5, ge=1, le=MAX_TOP_K, description="How many chunks to retrieve as context"
    )
    """Retrieval breadth, not answer length. More chunks means more evidence and
    a larger bill on every question — and past a point, worse answers, because
    the relevant passage gets buried among marginal ones."""


class Citation(APIModel):
    """One source the answer drew on.

    `number` is what appears in the answer text as `[1]`. It is returned rather
    than left for the client to infer, because inferring it means parsing prose
    with a regex and being wrong the first time a model writes `[1][2]`.

    `chunk_id` and `chunk_index` are here so a client can do more than name the
    document: it can highlight the exact passage. Without them, "according to
    handbook.pdf" is the strongest citation a UI can offer, which for a 200-page
    handbook is barely a citation at all.
    """

    number: int = Field(description="The bracketed marker used in the answer, from 1")
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    score: float = Field(ge=0.0, le=1.0)


class AskUsage(APIModel):
    """What the answer cost.

    Returned to the client rather than merely logged, deliberately: this is a
    multi-tenant product where one organization's questions are another's noisy
    neighbour, and a customer who can see the cost of a question can make their
    own decisions about `top_k`. It also means the number M12 bills on is the
    number the user was shown, rather than a second one reconstructed later.
    """

    input_tokens: int
    output_tokens: int
    context_tokens: int = Field(description="Tokens of retrieved context inside the input")
    dropped_sources: int = Field(
        default=0, description="Retrieved chunks that did not fit the context budget"
    )


class AskResponse(APIModel):
    """An answer, its evidence, and its cost."""

    answer: str

    citations: list[Citation]
    """Empty when nothing was retrieved. That case is neither an error nor an
    empty answer — it is an explicit refusal, and a client should render it as
    "we could not find this in your documents" rather than as a failure."""

    model: str = Field(description="Which model produced this, for attribution and debugging")
    usage: AskUsage

    truncated: bool = Field(
        default=False,
        description="The model hit its output limit; the answer stops mid-thought",
    )
    """The only signal separating a complete answer from one that ran out of
    room. Nothing raises, and the text reads as finished — so a client that does
    not surface this shows half an answer as the whole truth."""
