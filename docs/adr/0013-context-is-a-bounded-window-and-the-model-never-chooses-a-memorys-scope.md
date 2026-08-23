# ADR-0013: Context is a bounded window, and the model never chooses a memory's scope

- **Status:** accepted
- **Date:** 2026-08-15
- **Milestone:** M10

## Context

M9's agent answers one question. M10 makes it hold a conversation, which
introduces two problems a single question does not have.

**A conversation grows without limit.** The obvious implementation appends every
turn to every prompt. Cost then grows linearly with thread length while answer
quality does not follow, long contexts measurably bury the relevant part, so a
three-month thread costs fifty times a fresh one and answers worse. Nothing
fails; the bill and the quality drift in opposite directions, quietly.

**An agent that learns things has to decide who they belong to.** Extraction
reads a private conversation and writes durable facts. Those facts then appear in
future prompts, uncited, with nothing for a user to check them against. Which of
them are *the company's* and which are *one person's* is a privacy question being
answered on every extraction pass.

## Decision

**The prompt window is bounded by tokens and drops the oldest turns.**
`history_token_budget` is separate from `context_token_budget`, so a long thread
cannot starve the retrieved documents. `app/agents/history.py` slides from the
newest end backwards and reports what it dropped.

**It does not skip a long turn to fit an older short one**: the one place it
deliberately differs from `assemble_context`. Retrieval results are independent,
so dropping a long chunk to fit two short ones costs only relevance. A
conversation with a hole in the middle does not read as incomplete; it reads as a
non-sequitur, which the model then tries to explain.

**Long-term memory is what makes the bound survivable.** Extraction lifts durable
facts out of a conversation before the window drops them; recall puts them back
on demand. These arrived in one milestone because neither is defensible alone: a
window with no memory forgets permanently, and memory with no window solves a
problem nobody has.

**Summarising the dropped turns is deliberately not implemented.** It is the
other well-known answer and it costs a model call on the user's turn, which
`docs/agents.md` rule 5 forbids. Worse, a summariser that drops the one
qualifying clause fails invisibly and *replaces* the evidence that would have
contradicted it. There is no API key here to measure how often ours would, and an
unmeasured summariser is how a system starts misremembering on purpose.

**Everything extracted is `scope=user`. The model never chooses.** This is
ADR-0012's rule at a second boundary. There, the tenant is closed over so a
prompt-injected document cannot make the model search another organization's
files. Here, the scope is fixed in code so no phrasing, injected or merely
unlucky, can promote a fact from one person's private thread into something
every colleague's answers draw on. **A privacy boundary is not a field for a model
to fill in.**

A run with no user is therefore not extracted from at all. `scope=user` requires
a `user_id`, and quietly widening to org scope to make the insert succeed is
exactly the failure the rule exists to prevent.

**Memories inform the answer, never the query.** Recalled text is put in front of
the model; it is never added to the search string. Letting it steer retrieval
would close a loop: a wrong memory biasing the search that was supposed to
correct it, and the system would grow more confident in it every turn.

**A memory is never a citation.** The conversational system prompt says so
explicitly, and the memory block sits above the numbered sources so the block
parser cannot mistake one for the other. A citation points at a passage a user
can open; a remembered fact has nothing behind it, and numbering it would hand
someone a reference they cannot check.

**Recall is a mutating read.** Returning a memory reinforces it and stamps
`last_accessed_at`. That is unusual enough to be worth stating twice, and it is
what makes decay mean anything: without it the policy would measure a memory's
*age* rather than its *use*.

## Consequences

**Importance is earned by use, never self-reported.** The tempting alternative is
asking the model, at extraction time, how important the fact it just wrote is.
That number has nothing behind it: models are not calibrated on their own
output, and everything looks important at the moment of writing. A memory that
keeps surfacing for real questions has demonstrated its worth in a way no
self-assessment can.

**Deduplication happens twice, and the two differ in kind.** The database
enforces exact uniqueness on a hash of the normalised content: a guarantee no
code path can forget, and one that needs `NULLS NOT DISTINCT` (Postgres 15+),
because `user_id` is NULL for org memories and standard NULLs would let the same
org fact be stored once per extraction run forever. The writer *additionally*
skips near-duplicates by similarity, which is a policy: tunable, fallible, and
the only thing that catches "Invoices are approved by Finance" against "Finance
approves invoices".

**The blend multiplies rather than adds.** `similarity × (1 + w·importance) ×
recency`. A sum would let a very important memory clear the bar on importance
alone and surface for a question it has nothing to do with. A product cannot:
zero similarity is zero score, however important or fresh the fact. Importance
and recency modulate relevance; they never substitute for it.

**The blend cannot be an `ORDER BY`.** HNSW only accelerates the distance
operator, so ordering by any expression containing `importance` throws the index
away and sequentially scans every memory in the organization. Recall therefore
over-fetches by distance and re-ranks in Python. That is a real approximation: a
memory ranked 40th by similarity but very important is never found, and it is
the right trade, because the alternative degrades to a full scan exactly when the
table grows large enough for any of this to matter.

**Refusal is unchanged by memory.** A run that recalled three memories and
retrieved no documents still refuses. Memories are not evidence, and answering
from them would produce exactly the confident, unverifiable reply ADR-0010's
refusal exists to prevent.

**The conversational prompt pair is a near-copy, not an extension.** M8 committed
a baseline measured against `rag/system` and `rag/answer`, and ADR-0011 says no
prompt change ships without the harness confirming no regression. Adding
placeholders to the measured template would change every `/ask` prompt, by
whitespace at minimum, with no key here to re-measure. So the measured path is
untouched and the new path is new. The cost is two files that must be edited
together; consolidating them is the first thing to do once the harness covers
conversations.

**Contextualisation is deterministic, and it is the weakest thing in this
milestone.** A follow-up borrows keywords from the previous two user turns, which
demonstrably works, "How much is that?" retrieves the mileage passage in a real
trace, and it also dilutes the query with everything else those turns mentioned.
A model-written *standalone question* is the production answer and would very
likely beat it. Whether it does is a question M8's harness can now ask, and
answering it needs a key.

**No streaming for a conversation turn.** `/ask/stream` (M7) streams because a
single answer benefits from first-token latency. A conversation turn must also
*persist* the reply, and a stream that fails halfway has already shown the client
something it can no longer commit. Doing that correctly means writing the message
from the stream's completion callback, real work with real failure modes, which
M13's chat UI is what justifies.
