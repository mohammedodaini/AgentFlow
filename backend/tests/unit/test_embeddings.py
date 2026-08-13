"""The embedding seam: determinism, geometry, and the dimension contract (M6).

`HashingEmbedder` is a development tool, and it is tested as seriously as
production code for one reason: every retrieval test in the suite depends on
it. If it stopped being deterministic, or stopped producing unit vectors, those
tests would fail in ways that look like retrieval bugs.

`OpenAIEmbedder` is exercised too, against a substituted client. That is a
narrower claim than it sounds and the line is worth stating precisely: these
tests cannot say whether OpenAI returns good vectors — that needs a key, a
network and money — but they can say whether *our* code hands back the vectors
it was given, in the order it was given them, in the batches it promised. Both
of those are ours to get wrong, and one of them fails silently: a mis-ordered
batch gives every chunk a neighbour's vector, raises nothing, and turns search
into confident nonsense.

So the fake client here is not a stand-in for the API. It is a way to feed the
one input a real API would almost never produce on demand — out-of-order
results, which the endpoint documents as legal — and watch what our code does
with it.
"""

from __future__ import annotations

import math
import subprocess
import sys
from dataclasses import dataclass

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.models.document_chunk import EMBEDDING_DIMENSIONS
from app.rag.embeddings import (
    EmbeddingProvider,
    HashingEmbedder,
    OpenAIEmbedder,
    create_embedder,
)


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder(EMBEDDING_DIMENSIONS)


def _cosine(first: list[float], second: list[float]) -> float:
    """Both vectors are unit length, so the dot product is the cosine."""
    return sum(a * b for a, b in zip(first, second, strict=True))


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


# --------------------------------------------------------------------------
# the dimension contract
# --------------------------------------------------------------------------


def test_settings_and_the_column_agree_on_dimensions() -> None:
    """The check that stops a whole class of production failure.

    `document_chunks.embedding` is `vector(1536)`. A provider configured for a
    different width does not fail at startup, or at upload — it fails inside
    the ingestion worker on the INSERT, so the user sees their document go
    `failed` and an operator sees a message about a column.
    """
    assert get_settings().embedding_dimensions == EMBEDDING_DIMENSIONS


async def test_the_hashing_embedder_produces_that_width(embedder: HashingEmbedder) -> None:
    vectors = await embedder.embed_documents(["hello world"])

    assert embedder.dimensions == EMBEDDING_DIMENSIONS
    assert len(vectors[0]) == EMBEDDING_DIMENSIONS


def test_it_satisfies_the_protocol() -> None:
    """Structural conformance, checked rather than assumed — the same reason
    `LocalObjectStorage` is checked against `ObjectStorage`."""
    assert isinstance(HashingEmbedder(8), EmbeddingProvider)


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------


async def test_vectors_are_unit_length(embedder: HashingEmbedder) -> None:
    """Cosine distance assumes normalised vectors.

    Without normalisation a long chunk would score closer to everything simply
    for being long, and ranking would measure document size rather than
    relevance.
    """
    vectors = await embedder.embed_documents(
        ["expenses policy", "a much longer piece of text " * 20]
    )

    for vector in vectors:
        assert math.isclose(_norm(vector), 1.0, rel_tol=1e-9)


async def test_text_with_no_words_still_produces_a_usable_vector(
    embedder: HashingEmbedder,
) -> None:
    """Punctuation only, or a script this tokeniser does not split.

    A zero vector has undefined cosine distance — pgvector returns NaN, which
    then sorts unpredictably and silently corrupts a ranking. A fixed unit
    vector is meaningless but well-defined.
    """
    vector = await embedder.embed_query("!!! ??? ...")

    assert math.isclose(_norm(vector), 1.0)
    assert not any(math.isnan(value) for value in vector)


async def test_two_words_that_cancel_do_not_crash_the_embedder(
    embedder: HashingEmbedder,
) -> None:
    """A regression test, and the bug it pins was a hard crash.

    Signed hashing means two words can land on the same coordinate with
    opposite signs and sum to exactly zero. `math.log(0)` raises
    `ValueError: math domain error`, so the whole embed call died — taking the
    ingestion job with it, and only for documents unlucky enough to contain
    such a pair.

    "aat" and "ack" are that pair at 1536 dimensions: both hash to coordinate
    213, with opposite signs. Hard-coded rather than searched for at runtime,
    and the cancellation is asserted rather than assumed — otherwise a change
    to the hash would quietly turn this into a test of nothing.
    """
    vector = await embedder.embed_query("aat ack")

    assert vector[213] == 0.0, "the pair must actually cancel, or this tests nothing"
    assert math.isclose(_norm(vector), 1.0)


async def test_shared_words_score_higher_than_unrelated_text(
    embedder: HashingEmbedder,
) -> None:
    """The property that makes every retrieval test meaningful.

    This embedder matches words, not meaning — a real limitation, and enough
    for a test to assert that the *right* chunk ranks first rather than merely
    that some chunk came back.
    """
    query = await embedder.embed_query("expense reimbursement policy")
    related, unrelated = await embedder.embed_documents(
        [
            "Our expense reimbursement policy pays out monthly.",
            "The office plants are watered on Tuesdays.",
        ]
    )

    assert _cosine(query, related) > _cosine(query, unrelated)


# --------------------------------------------------------------------------
# determinism — the property blake2b exists to guarantee
# --------------------------------------------------------------------------


async def test_the_same_text_always_embeds_identically(embedder: HashingEmbedder) -> None:
    other = HashingEmbedder(EMBEDDING_DIMENSIONS)

    assert await embedder.embed_query("expenses") == await other.embed_query("expenses")


def test_vectors_are_identical_across_processes() -> None:
    """The reason this embedder hashes with `blake2b` and not `hash()`.

    Python salts `hash()` per process (PYTHONHASHSEED), so a built-in hash
    would give the *worker* one vector for a word and the *API* a different one
    for the same word. Every stored embedding would be unreachable by every
    query — presenting as "search returns nothing", with nothing in any log.

    Two subprocesses with deliberately different seeds are the only honest way
    to assert this: inside a single process the bug is invisible.
    """
    script = (
        "from app.rag.embeddings import HashingEmbedder;"
        "print(HashingEmbedder(64)._vector('expenses reimbursement')[:8])"
    )

    outputs = [
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        ).stdout
        for seed in ("0", "12345")
    ]

    assert outputs[0] == outputs[1], "hashing must not depend on the process hash seed"


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def test_the_offline_provider_is_the_default() -> None:
    """So a fresh clone runs its whole test suite with no API key and no
    network. `Settings` refuses this same default in production."""
    assert isinstance(create_embedder(get_settings()), HashingEmbedder)


def test_the_openai_provider_is_selected_by_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam's whole purpose: one setting, no call sites changed."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    get_settings.cache_clear()

    assert isinstance(create_embedder(get_settings()), OpenAIEmbedder)


# --------------------------------------------------------------------------
# the OpenAI provider — our half of it
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    """One element of the API's `data` array."""

    index: int
    embedding: list[float]


@dataclass(frozen=True)
class _Response:
    data: list[_Item]


class FakeEmbeddings:
    """Stands in for `client.embeddings`, recording the batches it is asked for.

    Each vector is `[float(position)] * 4`, so a vector's contents identify the
    text it belongs to. That is what makes the ordering assertion possible at
    all: with interchangeable vectors, a shuffled result is indistinguishable
    from a correct one.
    """

    def __init__(self, *, shuffle: bool = False) -> None:
        self.batches: list[list[str]] = []
        self._shuffle = shuffle

    async def create(self, *, model: str, input: list[str], dimensions: int) -> _Response:  # noqa: A002 — the API's parameter name
        del model, dimensions
        self.batches.append(list(input))

        items = [
            _Item(index=position, embedding=[float(self._sequence + position)] * 4)
            for position in range(len(input))
        ]
        self._sequence += len(input)

        return _Response(data=list(reversed(items)) if self._shuffle else items)

    _sequence = 0


def openai_embedder(fake: FakeEmbeddings, *, batch_size: int = 96) -> OpenAIEmbedder:
    """A real `OpenAIEmbedder` with its client's `embeddings` replaced.

    The object under test is genuine — its batching loop, its sort, its
    `embed_query` delegation all run. Only the network boundary is substituted,
    which is the smallest possible cut and the reason these assertions mean
    something.
    """
    settings = get_settings().model_copy(
        update={
            "embedding_provider": "openai",
            "openai_api_key": SecretStr("sk-test-not-a-real-key"),
            "embedding_batch_size": batch_size,
        }
    )
    embedder = OpenAIEmbedder(settings)
    # `embeddings` is a cached property, so mypy calls it read-only while the
    # assignment is perfectly ordinary at runtime — it populates the cache.
    embedder._client.embeddings = fake  # type: ignore[assignment, misc] # noqa: SLF001
    return embedder


async def test_vectors_come_back_in_the_order_the_texts_went_in() -> None:
    """The silent failure this sort exists to prevent.

    The endpoint documents that `data` may arrive out of order. Trusting
    arrival order raises nothing, fails no type check, and gives every chunk a
    neighbour's vector — so search returns confident nonsense and every test of
    the plumbing still passes.
    """
    fake = FakeEmbeddings(shuffle=True)

    vectors = await openai_embedder(fake).embed_documents(["first", "second", "third"])

    assert vectors == [[0.0] * 4, [1.0] * 4, [2.0] * 4]


async def test_texts_are_sent_in_bounded_batches() -> None:
    """Not an optimisation: the endpoint has a token limit per request, and
    exceeding it fails the whole call rather than the excess."""
    fake = FakeEmbeddings()

    await openai_embedder(fake, batch_size=2).embed_documents(["a", "b", "c", "d", "e"])

    assert fake.batches == [["a", "b"], ["c", "d"], ["e"]]


async def test_embedding_nothing_makes_no_request_at_all() -> None:
    """An empty batch is a billable round trip that can only return nothing."""
    fake = FakeEmbeddings()

    assert await openai_embedder(fake).embed_documents([]) == []
    assert fake.batches == []


async def test_a_query_is_embedded_as_a_single_text() -> None:
    fake = FakeEmbeddings()

    vector = await openai_embedder(fake).embed_query("expenses")

    assert vector == [0.0] * 4
    assert fake.batches == [["expenses"]]


def test_the_openai_provider_reports_the_configured_width() -> None:
    """`DocumentChunk.embedding` is `vector(1536)`; a provider that disagreed
    would fail inside Postgres, several layers below the mistake."""
    assert openai_embedder(FakeEmbeddings()).dimensions == EMBEDDING_DIMENSIONS
