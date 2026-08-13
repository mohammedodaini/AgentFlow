# M7 — Generation: `/ask`, grounded answers, and citations

- **Date:** 2026-08-13
- **Status:** shipped
- **ADRs:** [ADR-0010](../adr/0010-answers-are-grounded-refusals-are-local.md)

M6 could find the right paragraph. M7 turns it into an answer — and, more
importantly, refuses to invent one when the paragraph is not there.

## What was built

| Piece | File | What it does |
|---|---|---|
| The LLM seam | `app/llm/base.py` | `LLMProvider` protocol, `Completion`, `LLMError` |
| Claude client | `app/llm/anthropic.py` | The only module importing the `anthropic` SDK |
| Offline model | `app/llm/offline.py` | Extractive, deterministic, no key required |
| Prompt loader | `app/prompts/loader.py` | Templates as files; `KeyError` on a missing placeholder |
| The prompts | `app/prompts/rag/{system,answer}.md` | The grounding rules and the context frame |
| Context assembly | `app/rag/context.py` | Token budget + the citation map, built together |
| Generator | `app/rag/generation.py` | retrieve → assemble → render → generate |
| `POST /ask` | `app/api/v1/routes/generation.py` | Answer with citations and usage |
| `POST /ask/stream` | same | The same answer over SSE |

## A new top-level package, and why

`app/llm/` rather than `app/rag/generation_client.py`. Generation is not
retrieval, and four separate parts of this system need a model: RAG answers
(M7), LLM-as-judge scoring (M8), the agent's reasoning (M9), and memory
extraction (M10). A client living inside `rag/` would make three of those import
from a package they have nothing to do with — and the first person to notice
would fix it by writing a second client.

## The decision the milestone is really about

**A model given no context does not fail. It invents.** Fluently, confidently,
from training data, with no citations and a 200 status code. Nothing in a test
suite notices, because nothing is wrong except the content.

So the refusal is structural rather than instructed:

- `app/prompts/rag/system.md` tells Claude to refuse with an exact sentence.
- `Generator` never gets that far — with no usable context it returns the
  refusal locally and **never calls the model at all**.
- The test asserts this by passing a provider that raises if invoked. A test
  checking only the answer text would pass against an implementation that
  called out and happened to get lucky.

## Citations are built with the prompt, not after it

`assemble_context` numbers the sources and records what each number means in the
same loop. The failure it prevents is two individually correct halves that
disagree: the answer says `[2]`, the API returns a `[2]` pointing at a different
chunk, every schema validates, and every citation in the product is wrong.

Each citation carries `chunk_id` and `chunk_index`, not just the document. For a
200-page handbook, "according to handbook.pdf" is barely a citation at all.

## Bugs this milestone found

Three — and the third was found only by running the thing.

**1. The offline model answered by repeating the question.** The block regex ran
to the end of the string, so the final context block swallowed the trailing
`Question:` line. That line then scored a perfect word-overlap match against
itself and won every time. The output was the question, echoed back, with a
citation attached — fluent, well-formed, entirely wrong. It passed the unit
tests, because a question's words also appear in the chunk that answers it; only
an integration test whose question was worded differently from the passage
exposed it.

**2. Quoted answers began with a filename.** `SOURCE_TEMPLATE` puts the document
title on the marker line, and a filename has no terminating punctuation, so the
sentence splitter could not separate it from the passage — every answer read
"handbook.pdf Expenses are reimbursed…".

**3. A refusal came back with three citations attached.** Found at runtime, with
curl, and invisible to every test at the time. A vector search always returns its
`top_k` nearest neighbours however far away they are — "nothing relevant" is not
a state pgvector can report — so a question the corpus could not answer still
produced a full context. The model refused; the citations remained. The response
told the user both that we found nothing and that here are the things we found.
Fixed with `MIN_EVIDENCE_SCORE`, which excludes chunks of *zero* similarity and
is deliberately not the tuned relevance floor M8 owns.

Fixing that third one broke `test_a_small_budget_drops_sources_and_says_so`,
which had been retrieving three chunks and now retrieved one. The query was
widened rather than the assertion weakened — otherwise the test would have kept
passing while covering nothing.

## Verified at runtime

Real uvicorn, real arq worker, curl:

- `POST /ask` → the passage as an answer, citing `[1]`, and `[1]` is the
  highest-scoring chunk (0.401 against 0.175 and 0.000).
- `usage` returned to the client: `input_tokens=1423`, `context_tokens=912`,
  `dropped_sources=0`.
- `POST /ask/stream` → `event: sources` first, carrying the full citation list,
  then `token` frames, then `done`. Headers confirmed:
  `content-type: text/event-stream`, `x-accel-buffering: no`,
  `cache-control: no-cache`.
- A question the documents cannot answer → the refusal, `citations: []`, and
  `usage` all zeroes — which is the proof that the model was never called.

## Gate

```
ruff · ruff format · mypy --strict (204 files) · alembic check
395 tests, 2 skipped · 98.25% coverage (gate 97%)
```

Pyramid: 223 unit / 74 integration / 100 e2e. No migration — M7 adds no tables.

## Known gaps, deliberately left

**No conversation history.** `/ask` is stateless: one question, one answer. Chat
threads are M10, and building a half-version now would mean throwing it away.

**No reranking, no hybrid search.** Same answer as M6: M8 measures first.

**Answer quality is unmeasured, and that is the honest headline.** Everything
verified above is plumbing — budgeting, citation mapping, refusal, error
handling, SSE framing. Whether Claude writes faithful prose over these chunks
needs a golden set (M8) and an API key, and neither exists yet. The offline
provider quotes sentences; it does not write, so no amount of testing against it
says anything about generation quality.

**Token counts from the offline provider are approximated** at four characters
per token. Nothing bills on them, and the real client captures the provider's
own numbers.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099         # terminal 1
uv run arq app.workers.settings.WorkerSettings  # terminal 2 — required
```

Register, upload a `.txt` or `.pdf`, poll until `ready`, then:

```bash
curl -X POST localhost:8099/api/v1/ask \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' \
  -d '{"query": "How are expenses reimbursed?", "top_k": 3}'
```

Add `/stream` to the path for SSE. With `LLM_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY` set, the same commands hit Claude instead.
