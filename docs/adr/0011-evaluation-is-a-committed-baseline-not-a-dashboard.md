# ADR-0011: Evaluation is a committed baseline and an exit code, not a dashboard

- **Status:** accepted
- **Date:** 2026-08-13
- **Milestone:** M8

## Context

M6 and M7 shipped with five numbers documented as guesses: `chunk_size_tokens`
(400), `chunk_overlap_tokens` (60), `retrieval_top_k` (5),
`context_token_budget` (8000), and `DEFAULT_MIN_SCORE` (0.0). Each carried a
comment saying, in effect, "M8 replaces this with a measurement". This is M8.

The failure mode being designed against is specific and common. A team builds an
eval harness, runs it once, screenshots the numbers, and never runs it again
because it is a script somebody has to remember, its output is prose, and
nothing breaks when it is skipped. Six months later the prompt has changed forty
times and nobody can say whether retrieval got better or worse.

`docs/agents.md` already made the promise: *"No prompt change ships without the
evaluation harness confirming no regression on the golden set."* A promise like
that is worth exactly as much as the mechanism enforcing it.

## Decision

**The baseline is a committed file, and the verdict is an exit code.**
`app/evaluation/baselines/<dataset>.json` holds the aggregate from the last run
somebody accepted. `python -m app.evaluation` re-runs the golden set, compares,
and returns 1 if any metric fell more than the tolerance below it. `make eval`
is that command. A regression is a red build; an improvement is a diff on a
committed number that a reviewer approves.

**Baselines are never updated automatically.** `--save-baseline` exists and is
run by a human who has read the report. Auto-updating would make every future
comparison pass by construction.

**Golden sets are files that carry their own corpus.** `app/evaluation/data/
<name>.json` holds both the documents and the questions. An eval that ran
against whatever happened to be in the database would measure the database.

**Examples name documents, not chunks.** Chunk ids do not exist until ingestion
and depend on `chunk_size_tokens` and `chunk_overlap_tokens`: the settings this
milestone exists to tune. A golden set keyed on them would need regenerating on
every change to the variable under test.

**Deterministic metrics first, judge second.** Recall@k, precision@k, MRR,
citation accuracy and literal answer matching are free, instant and
reproducible. The LLM judge scores only what code cannot see: faithfulness,
relevance, completeness.

**The judge has an offline counterpart**, like every external dependency since
ADR-0007, so the harness and its regression gate run with no API key.

## Consequences

**Every knob now has an instrument.**
`test_metrics_move_when_retrieval_is_crippled` asserts the numbers actually
respond to a configuration change: a measurement that reads the same however
the system is configured measures nothing, and that test is what stops this
harness quietly becoming decoration.

**The first run produced a real finding, and it contradicted an assumption.**
`DEFAULT_MIN_SCORE` was left at 0.0 at M6 on the argument that a threshold
picked by eye hides answers. The measurement came back stronger than the
argument: on the `handbook` set, no non-zero threshold beats zero. The score
distributions overlap outright: the lowest-scoring answerable question scores
0.069, the highest-scoring unanswerable one 0.262, so every threshold that
catches a refusal discards more real answers than it saves. **Refusal is a
judgement about meaning, and a similarity score cannot make it.** It belongs to
the model reading the context.

**Refusal accuracy is currently 0.000, and it is reported rather than hidden.**
The offline extractive provider has no capacity to decide that a passage fails
to answer a question; it quotes the best-overlapping sentence and always
produces something. Whether Claude does better is exactly what cannot be
measured without a key, so the number sits in the baseline as a known, labelled
gap rather than being quietly excluded from the aggregate.

**Refusal examples are excluded from the recall average.** They score a free 1.0
by construction, so including them would let a dataset raise its own headline
number by adding more unanswerable questions. They are measured separately by
`refusal_accuracy`, which is the metric that actually moves when a system starts
inventing.

**A malformed dataset is refused at load.** The empty one is the dangerous case:
a run over zero examples scores a perfect 1.0 on everything, and in CI that is
indistinguishable from a run over the whole golden set.

**The tolerance is 2%, and that is a guess**: named as one, in the docstring. It
should be tightened once several runs show how much these numbers move on their
own. But a gate that fires on a 0.001 drop gets muted within a week, and a muted
gate is no gate at all.

**What this cannot tell us.** Every number above was measured with the hashing
embedder and the extractive provider. They say the harness works, the metrics
respond, and the gate fires. They say nothing about whether a real embedding
model retrieves well or whether Claude answers faithfully. The method
generalises; the numbers do not, and must be re-measured once a key exists.
