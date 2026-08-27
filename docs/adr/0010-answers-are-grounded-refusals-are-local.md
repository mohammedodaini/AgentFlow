# ADR-0010: Answers are grounded in retrieved text, and refusals never reach the model

- **Status:** accepted
- **Date:** 2026-08-13
- **Milestone:** M7

## Context

M7 puts a language model on top of M6's retrieval. That is where a document
search becomes an AI product, and also where it acquires its first failure mode
that no exception can catch.

A model asked a question with an empty or irrelevant context block does not
fail. It answers, fluently, confidently, from its training data, with no
citations and no indication that the corpus was silent. Every status code is
200. Every schema validates. The response is indistinguishable from a correct
one except by someone who already knows the answer, which is precisely the
person who was not going to ask.

Two smaller decisions sit alongside it. There is no `ANTHROPIC_API_KEY` in this
environment, so the suite needs a model it can run without one. And streaming
was in the roadmap, which raises the question of what a client should do when a
failure happens after the response has already begun.

## Decision

**Answers are grounded, and grounding is structural rather than instructed.**
The prompt (`app/prompts/rag/system.md`) tells Claude to use only the numbered
sources, cite every claim, and refuse with an exact sentence when the sources do
not answer the question. But an instruction is a request, not a guarantee, so
the code makes the failure impossible to reach rather than merely discouraged.

**No context, no call.** When retrieval yields nothing usable, `Generator`
returns the refusal locally and never contacts the model at all. "Nothing
usable" excludes chunks with *zero* similarity, which a vector search returns
regardless: a `top_k` query always produces `top_k` neighbours, however far
away they are. That threshold (`MIN_EVIDENCE_SCORE`) is not the tuned relevance
floor M6 deferred to M8; it excludes only the degenerate case.

**Citations are built with the prompt, in one loop.** `app/rag/context.py`
numbers the sources and records what each number means at the same moment. The
alternative, number them in one place, reconstruct the mapping in another, is
a bug that makes every citation in the product wrong while raising nothing.

**One `LLMProvider` protocol, two implementations.** `AnthropicLLM` for
production, `OfflineLLM` for everywhere else, with `Settings` refusing the
offline one in production. The third instance of the pattern established by
ADR-0007 and ADR-0009.

**Streaming is a second endpoint, not a replacement.** `POST /ask` returns JSON;
`POST /ask/stream` returns SSE. Streaming is strictly harder to consume, and a
service-to-service caller should not pay that cost for a latency benefit only a
human perceives.

## Consequences

**The refusal is testable without a key, and it is asserted by the *absence of a
call*.** `tests/integration/test_generation.py` passes a provider that raises if
invoked at all. A test that only checked the answer text would pass just as
happily against an implementation that called the model and got lucky.

**The offline model is extractive, not canned.** It parses the numbered blocks
out of the prompt and returns the sentence with the most words in common with
the question. That narrowness is the point: a provider returning "This is a test
answer." would let the context be empty, the citations wrong and the budget
broken while every assertion still passed. This one cannot answer unless the
right chunk was genuinely retrieved and genuinely survived into the prompt.

**It is coupled to the prompt template, deliberately and visibly.** The parser
depends on the `[n]` block format; a test renders the real template and asserts
the parser finds its blocks, so a reworded prompt fails loudly rather than
turning every offline answer into a refusal. The same test asserts the refusal
sentence is byte-identical in both files.

**A context budget exists separately from the context window.** The window is a
limit; `CONTEXT_TOKEN_BUDGET` is a decision. Filling a 200k window with every
plausible chunk costs money on every question and measurably degrades answers,
because the relevant passage gets buried among marginal ones. Chunks are dropped
whole rather than truncated, half a passage can invert what it says.

**Temperature is zero.** A RAG answer is faithful summarisation, not creativity:
sampling randomness is an opportunity to drift from the retrieved text, which is
exactly what citations exist to prevent. It also makes M8's evaluation measure
prompt changes rather than sampling noise.

**Streaming cannot report a failure as a status code, so it reports one as an
event.** Once the first byte is written the 200 has already been sent.
`/ask/stream` emits `sources` first, then `token` events, then `done`, or
`error` instead, which is the only thing distinguishing a dropped connection
from a finished answer. Sources come first because retrieval completes before
the first token exists, so a client can render citations immediately.

**We still cannot say whether the answers are good.** Everything above is
verified against the offline provider and a substituted Anthropic client: the
plumbing, the budgeting, the citation mapping, the refusal, the error handling.
Whether Claude writes faithful prose over these chunks is unmeasured, and stays
unmeasured until M8 builds a golden set and a key exists to run it against.
Saying so is more useful than a milestone note that implies otherwise.
