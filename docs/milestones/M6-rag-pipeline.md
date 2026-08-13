# M6 — The RAG pipeline: chunking, embeddings, and vector search

- **Date:** 2026-08-13
- **Status:** shipped
- **ADRs:** [ADR-0009](../adr/0009-embeddings-behind-a-protocol-with-a-real-offline-implementation.md)

M5 could accept a document and parse it. M6 makes it *findable*. This is the
first milestone whose output a human can judge by eye — you upload a handbook,
ask a question, and either the right paragraph comes back or it does not.

## What was built

| Piece | File | What it does |
|---|---|---|
| `document_chunks` | `app/models/document_chunk.py` | Chunk rows with a `vector(1536)` column and an HNSW index |
| Migration | `alembic/versions/…ce986f05bf72_…` | `CREATE EXTENSION vector`, the table, the HNSW index |
| Chunking | `app/rag/chunking.py` | Paragraph-aware, token-bounded, overlapping. Pure function |
| Embeddings | `app/rag/embeddings.py` | `EmbeddingProvider` protocol + OpenAI and offline implementations |
| Retrieval SQL | `app/repositories/chunk_repository.py` | Cosine search, tenancy join, `replace_for_document` |
| Retriever | `app/rag/retrieval.py` | Embed query → search → optional relevance floor |
| `POST /search` | `app/api/v1/routes/retrieval.py` | Ranked chunks with citations |
| Indexing | `app/workers/tasks/ingestion.py` | The worker now chunks, embeds and indexes |

## Search before generation, deliberately

`/search` ships a milestone before `/ask`. Retrieval quality caps answer quality
absolutely: a model given the wrong three paragraphs writes a fluent,
confident, wrong answer, and the failure looks like a model problem. An endpoint
that returns the chunks and their scores makes retrieval inspectable on its
own — which is also exactly the surface M8's metrics will measure.

## The three decisions worth arguing about

**Chunk geometry (400 tokens, 60 overlap) is a starting point, not a finding.**
`docs/roadmap.md` is explicit that these get tuned at M8 against a golden set,
"using eval metrics, not vibes". Until then they are vibes, and saying so is
more useful than defending them.

**Overlap is capped at `chunk_size // 2`, not merely below `chunk_size`.** Equal
overlap never advances — each chunk would begin with exactly the tokens the
previous one ended with, and the function would allocate until the process died.
But anything above half also produces chunks that are mostly copies of their
neighbours: storage and embedding spend go up while retrieval gets *worse*,
because near-duplicates crowd each other out of the top results.

**No relevance floor by default.** A floor obviously improves a demo. It is off
because the right threshold depends on the embedding model, and picking one by
eye means silently returning nothing for questions whose answer sits just under
the line — and a user reading "no results" cannot tell that from "we found it
and hid it". The mechanism exists and is tested; M8 sets the number.

## `document_chunks` has no `organization_id`

Scope comes from a join to `documents`. Denormalising the tenant id onto the
chunk would make the filter cheaper and introduce a way for the two to
disagree — a chunk whose `organization_id` no longer matches its document's is
unreachable at best and cross-tenant at worst.

That makes the join load-bearing, so it is asserted directly: in
`test_another_organizations_chunks_are_never_returned` the *other* tenant's
chunk is a better match for the query than our own, so a broken filter cannot
hide behind ranking — it would come back first.

## HNSW filters after the vector scan

This is the pgvector fact that is easy to miss and expensive to learn late. An
HNSW index finds nearest neighbours *first* and applies the `WHERE` clause
afterwards, so a tenancy filter can silently return fewer than `top_k` rows —
not wrong answers, just missing ones, with nothing to indicate it happened.
`similarity_search` issues `SET LOCAL hnsw.iterative_scan = 'strict_order'`
before the query, which makes pgvector keep scanning until it has genuinely
found `top_k` matching rows.

## Bugs this milestone found

Six, none of which review caught.

**1. Autogenerate produced an unusable migration.** `alembic revision
--autogenerate` emitted `pgvector.sqlalchemy.vector.VECTOR(...)` with no import,
no `CREATE EXTENSION`, and — most importantly — **no HNSW index**. Applied as
generated, every search would have been a sequential scan that worked fine on
ten chunks and fell over on a hundred thousand. Hand-fixed, all three.

**2. `alembic check` then reported a pending `remove_index`.** The index existed
in the database but not in `Base.metadata`, so the *next* autogenerate would
have emitted a migration dropping it. Fixed by declaring the `Index` on the
model with `postgresql_using="hnsw"` and `postgresql_ops` — which also corrected
a docstring claiming SQLAlchemy could not express it.

**3. The separator was not counted against the chunk budget.** `"\n\n".join()`
costs a token per join. A chunker that "never exceeds `chunk_size`" quietly
exceeded it by one token per joined paragraph.

**4. Token counts are not additive under concatenation.** Byte-pair encoding
merges *across* a join, so `encode(a + sep + b)` can be longer than the sum of
its parts. A `chunk_size=8` test produced a 9-token chunk with every piece
individually within budget. The packing loop still budgets by summing — it is
fast and very nearly right — and `_enforce_limit` now measures each finished
chunk once and splits it if it is still over. "Very nearly" is not what a hard
bound means.

**5. Two words that cancel crashed the offline embedder.** Signed hashing lets
two words land on one coordinate with opposite signs and sum to exactly zero;
`math.log(0)` raises `ValueError: math domain error`. Every text in the unit and
integration suites happened to avoid such a pair. The new end-to-end test, over
a three-paragraph document, did not — the failure appeared on its first run.
Zero-count coordinates are now skipped, and `"aat"`/`"ack"` (both hash to
coordinate 213, opposite signs) is the regression test.

**6. `EmbeddingProvider` was missing `@runtime_checkable`,** so the conformance
test raised `TypeError` on `isinstance` instead of asserting anything.

## Two things that broke without being touched

Worth recording because neither was caused by this milestone's code.

**mypy was silently upgraded 1.x → 2.3.0.** `pyproject.toml` asks for
`mypy>=1.13`, so resolving the new M6 dependencies re-locked it to a new major
version — which then reported six pre-existing type errors in
`tests/unit/test_models.py`. Fixed properly (a `Table` narrowing helper, and a
`str`-annotated variable instead of an enum-to-literal comparison) rather than
pinned back.

**Three older tests built a production `Settings` and were refused** by the new
`EMBEDDING_PROVIDER=hashing` guard. They already supplied a real `SECRET_KEY` for
the M3 guard; the list of things "production" requires now grows every couple of
milestones, so it lives in one `enter_production()` helper rather than being
copied a fourth time.

## Verified at runtime

Not only in tests. A real `uvicorn` plus a real `arq` worker, driven by `curl`:

- Upload → `202 pending`; ingested and `ready` in **under one second**; the
  worker log reports `4 jobs complete, 0 failed, 0 retries`.
- `POST /search` returns the chunk with `document_id`, `document_title`,
  `chunk_index` and a score — a complete citation.
- A document split into three chunks (376 / 399 / 118 tokens, all ≤ 400) ranks
  correctly *between* chunks: "receipt reimbursed expenses claim" → chunk 0
  (0.535); "holiday manager approved weeks" → chunk 2 (0.479). The overlap is
  visible — chunk 1 opens with chunk 0's tail.
- A request carrying another organization's id is `404 not_found`, not `403`,
  so the endpoint is not an oracle for enumerating tenants.
- Migration rehearsed **down and up**: `downgrade` removes `document_chunks` and
  deliberately leaves the `vector` extension (dropping a type another schema may
  use is not this migration's business, and `CREATE EXTENSION IF NOT EXISTS`
  makes re-upgrade safe). `\d document_chunks` after re-upgrade shows the HNSW
  index with `vector_cosine_ops`, `m=16`, `ef_construction=64`.

## Gate

```
ruff · ruff format · mypy --strict (191 files) · alembic check
316 tests, 2 skipped · 98.01% coverage (gate 97%)
```

Pyramid: 176 unit / 62 integration / 80 e2e.

`tests/worker_harness.py` is new: the `ctx` dict and borrowed session that let a
worker task run inside a test's transaction, extracted from the integration
tests once the end-to-end search test needed them too.

## Known gaps, deliberately left

**No reranking.** A cross-encoder over the top 50 candidates is the standard
next improvement and often a large one. Adding it now would be spending latency
and money on the strength of a blog post; M8 builds the golden set that can say
whether it helps *this* corpus. Measure, then optimise.

**No hybrid search.** BM25 alongside vectors handles exact terms — product
codes, error numbers — that embeddings are bad at. Same reasoning: M8 first.

**The offline embedder matches words, not meaning.** "How do I claim expenses?"
will not find "reimbursement policy". `Settings` refuses it in production for
exactly this reason. Everything above was verified with it, so the *ranking
mechanism* is proven and the *semantic quality* is not — that needs a key.

**Scores are low, and that is expected.** A 400-token chunk covering three
unrelated topics dilutes its own embedding until it matches nothing strongly:
the runtime run above scored 0.18 on such a chunk and 0.535 once the chunks were
topically coherent. That is the chunking-quality argument made concrete, and it
is precisely what M8 will tune.

## Reproduce

```bash
make up                                         # Postgres + Redis
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099         # terminal 1
uv run arq app.workers.settings.WorkerSettings  # terminal 2 — required
```

Then register, upload a `.txt` or `.pdf`, poll until `ready`, and
`POST /api/v1/search` with `{"query": "...", "top_k": 3}`.
