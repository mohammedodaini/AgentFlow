# ADR-0009: Embeddings sit behind a protocol, with a real offline implementation

- **Status:** accepted
- **Date:** 2026-08-13
- **Milestone:** M6

## Context

M6 turns stored documents into searchable ones, which means embedding text. The
embedding provider is the first dependency in this project that is *expensive
per call*, and that changes what a seam has to be worth.

`ObjectStorage` (ADR-0007) exists mostly because production and a laptop cannot
run the same thing. That argument applies here too: no API key, no network on
a plane, but a second, sharper one applies as well: a test suite that embedded
for real would cost money on every run, need connectivity, and be
non-deterministic. Such a suite gets run rarely, and a suite that is run rarely
is indistinguishable from no suite at all. Retrieval is the layer where that
matters most, because retrieval quality caps answer quality absolutely: at M7 a
model handed the wrong three paragraphs writes a fluent, confident, wrong
answer, and the failure presents as a model problem.

There was also a live temptation to reach for `unittest.mock`. A mocked
embedder returning canned vectors makes every retrieval test pass and proves
nothing about ranking: the assertions become "the fake returned what the fake
was told to return", and the SQL that does the actual work is never exercised.

## Decision

`app/rag/embeddings.py` defines `EmbeddingProvider`, a `@runtime_checkable`
`Protocol` with `dimensions`, `embed_documents(texts)` and `embed_query(text)`.
Two implementations satisfy it:

- **`OpenAIEmbedder`**: the real one. OpenAI rather than Anthropic because
  Anthropic offers no embeddings endpoint; `docs/packages.md` marks it a swap
  candidate, and this class is the only thing that would change.
- **`HashingEmbedder`**: offline and deterministic. Hashed bag-of-words:
  `blake2b` maps each word to a coordinate and a sign, counts are damped
  logarithmically, and the vector is L2-normalised so cosine distance behaves.

Two methods rather than one because the distinction is real: several models are
trained asymmetrically and expect `query:` / `passage:` prefixes. Collapsing
them into `embed(texts)` would make adopting such a model a change at every
call site instead of a change in this file.

`Settings` refuses `EMBEDDING_PROVIDER=hashing` when `APP_ENV=production`, and
refuses `openai` without a key.

## Consequences

**The offline embedder is a real retrieval implementation, not a stub.** It
produces genuine lexical similarity, so `tests/integration/test_retrieval.py`
can assert that the *correct* chunk ranked first, through real pgvector, real
`<=>` ordering, and the real tenancy join. That is the whole return on the
decision: the assertions are about ranking, not about a fake.

**What it cannot do is match meaning.** "How do I claim expenses?" will not find
a chunk titled "reimbursement policy", because they share no words. This is
stated loudly rather than hidden, because the failure mode is being quietly
mediocre: no error, no alert, just worse answers. Hence the production refusal.

**`blake2b`, not the built-in `hash()`.** Python salts `hash()` per process, so
the API and the worker would produce different vectors for the same word: every
stored embedding would be unreachable by every query. It presents as "search
returns nothing", with nothing in any log. A test runs the same embedding under
two `PYTHONHASHSEED` values to pin this.

**Being a real implementation means it has real bugs.** Signed hashing lets two
words land on one coordinate with opposite signs and cancel to exactly zero,
and `math.log(0)` raises, so the whole embed call died, but only for documents
containing such a pair. Every text in the unit and integration suites happened
to avoid one; the end-to-end test over a three-paragraph document did not. A
stub would never have had the bug, and would never have caught it either.

**`@runtime_checkable` so conformance is a test, not an `AttributeError`.**
A provider missing `embed_query` should fail in CI, not three layers into a
worker at 3am.

**Verification of the provider itself is honestly partial.** The unit tests
substitute the OpenAI client and assert what is genuinely ours to get wrong:
that vectors come back in the order the texts went in (the endpoint documents
that `data` may arrive out of order, and trusting arrival order gives every
chunk a neighbour's vector while raising nothing), that batches respect the
configured size, and that an empty input makes no request. They cannot say
whether OpenAI's vectors are any good. Until a key exists, this milestone
verifies the plumbing and says so.

**Dimensions are configuration, and the column is not.**
`document_chunks.embedding` is `vector(1536)`. A provider returning 3072 would
fail inside Postgres with a message about the column, several layers below the
one environment variable that caused it, so the ingestion task checks the
width before inserting and names the settings a human would have to change.
