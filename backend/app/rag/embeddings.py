"""Text → vectors. The only module that knows which embedding model we use.

Layer: rag. Two implementations behind one protocol, for the same reason
`app/storage/` has two: the thing that works in production cannot run on a
laptop with no API key, and the thing that runs on a laptop must not be
mistaken for the thing that works in production.

The seam matters more here than it does for storage, because embeddings are
the one dependency that is *expensive per call*. A test suite that embedded for
real would cost money on every run, need a network, and be non-deterministic —
so it would be run rarely, which is the same as not having it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

import structlog
from fastapi import Request
from openai import AsyncOpenAI

from app.core.config import Settings

logger = structlog.get_logger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """What the rest of the application needs from an embedding model.

    `runtime_checkable` so conformance can be asserted in a test rather than
    discovered as an `AttributeError` inside a worker — the same reason
    `ObjectStorage` carries it.

    Two methods rather than one, because the distinction is real for some
    providers: several models are trained asymmetrically and expect a prefix
    like `query:` on the question and `passage:` on the document. Collapsing
    them into `embed(texts)` would make adopting such a model a change at every
    call site instead of a change here.
    """

    @property
    def dimensions(self) -> int:
        """Vector width. Must equal `DocumentChunk.embedding`'s declared size."""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts. The result matches the input index for index."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """Embed one search query."""
        ...


class OpenAIEmbedder:
    """The real provider. Needs `OPENAI_API_KEY`.

    OpenAI is used for embeddings even though Claude is the generation model,
    because Anthropic does not offer an embeddings endpoint. `docs/packages.md`
    marks this dependency as a swap candidate (voyageai, cohere) — this class
    is the only thing that would change.
    """

    def __init__(self, settings: Settings) -> None:
        self._model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        self._batch_size = settings.embedding_batch_size
        self._client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches, preserving input order.

        Batching is not an optimisation here — it is the difference between one
        request and two hundred for a single document. The batch size is
        bounded because the endpoint has a token limit per request, and
        exceeding it fails the whole call rather than the excess.
        """
        if not texts:
            return []

        vectors: list[list[float]] = []

        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            response = await self._client.embeddings.create(
                model=self._model, input=batch, dimensions=self._dimensions
            )

            # Sort by `index` rather than trusting arrival order. The API
            # documents that results may come back out of order, and a
            # mis-ordered batch is the worst kind of bug: nothing errors, every
            # chunk simply gets a neighbour's vector, and search returns
            # confident nonsense that no test of the plumbing would catch.
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)

        logger.debug("embeddings.embedded", count=len(texts), model=self._model)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]


class HashingEmbedder:
    """An offline, deterministic embedder. Development and tests only.

    Hashed bag-of-words: each word is hashed to a coordinate and a sign, counts
    are damped, and the vector is L2-normalised so cosine distance behaves.
    That gives *genuine* lexical similarity — a search for "expenses" really
    does rank the expenses chunk first — which is what makes the retrieval
    tests meaningful rather than a mock going through the motions.

    What it cannot do is match meaning. "How do I claim expenses?" will not
    find a chunk titled "reimbursement policy", because they share no words.
    That is precisely why `Settings` refuses this provider in production: it
    fails by being quietly mediocre, which no alert will ever catch.

    Hashing uses `blake2b`, not the built-in `hash()`. Python salts `hash()`
    per process, so the API and the worker would produce different vectors for
    the same word and every stored embedding would be unreachable by every
    query — presenting as "search returns nothing", with nothing in any log.
    """

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        counts: dict[int, float] = {}

        for word in _WORD.findall(text.lower()):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            position = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            counts[position] = counts.get(position, 0.0) + sign

        vector = [0.0] * self._dimensions

        for position, count in counts.items():
            if count == 0.0:
                # Two words hashed to this coordinate with opposite signs and
                # cancelled exactly. The coordinate carries no information, and
                # it must be *skipped* rather than damped: `math.log(0)` raises
                # `ValueError: math domain error`, which is how this was found —
                # by an end-to-end search over a three-paragraph document, while
                # every shorter text in the integration suite happened never to
                # collide. Signed hashing makes cancellation ordinary, not rare.
                continue

            # Sublinear damping, the same idea as tf-idf's log term frequency:
            # a word appearing forty times is more relevant than one appearing
            # once, but nowhere near forty times more. Without it a single
            # repeated word dominates the whole vector.
            vector[position] = math.copysign(1.0 + math.log(abs(count)), count)

        norm = math.sqrt(sum(value * value for value in vector))

        if norm == 0.0:
            # No word characters at all — punctuation only, or a script this
            # tokeniser does not split. A zero vector has undefined cosine
            # distance, which pgvector reports as NaN and which then sorts
            # unpredictably. A fixed unit vector is meaningless but well-defined.
            vector[0] = 1.0
            return vector

        return [value / norm for value in vector]


def create_embedder(settings: Settings) -> EmbeddingProvider:
    """Build the provider named by configuration.

    Same builder shape as `create_storage` and `create_engine`, and for the
    same reason: two processes need one each, and neither can borrow the
    other's.
    """
    if settings.embedding_provider == "openai":
        return OpenAIEmbedder(settings)

    logger.warning(
        "embeddings.using_offline_provider",
        detail="lexical matching only; set EMBEDDING_PROVIDER=openai for real retrieval",
    )
    return HashingEmbedder(settings.embedding_dimensions)


def get_embedder(request: Request) -> EmbeddingProvider:
    """Read the provider `lifespan()` stored on the application."""
    embedder: EmbeddingProvider = request.app.state.embedder
    return embedder
