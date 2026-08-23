# M8: Evaluation: the golden set, the metrics, and the gate

- **Date:** 2026-08-13
- **Status:** shipped
- **ADRs:** [ADR-0011](../adr/0011-evaluation-is-a-committed-baseline-not-a-dashboard.md)

M6 and M7 shipped five numbers labelled "a starting point, not a finding". M8 is
the instrument that turns them into findings, and the first thing it measured
contradicted an assumption the previous two milestones rested on.

## What was built

| Piece | File | What it does |
|---|---|---|
| Metrics | `app/evaluation/metrics.py` | recall@k, precision@k, MRR, citation accuracy |
| Golden sets | `app/evaluation/datasets.py` | Load + validate; corpus and questions in one file |
| The dataset | `app/evaluation/data/handbook.json` | 4 documents, 15 questions, 4 of them unanswerable |
| Judge | `app/evaluation/judge.py` | `LLMJudge` + `HeuristicJudge` behind one protocol |
| Judge prompts | `app/prompts/evaluation/{system,judge}.md` | The 1–5 rubric |
| Runner | `app/evaluation/runner.py` | Ingest → ask → score → aggregate → compare |
| CLI | `app/evaluation/__main__.py` | `make eval`; **the exit code is the product** |
| Baseline | `app/evaluation/baselines/handbook.json` | Committed, so a regression shows in a diff |

## The finding

The headline result is not a score. It is that **a similarity threshold cannot
implement refusal**.

M6 left `DEFAULT_MIN_SCORE` at 0.0, arguing that a threshold chosen by eye
silently hides answers sitting just under it. That was a reasonable argument.
The measurement is better than an argument:

| threshold | correct refusals | answerable lost | overall accuracy |
|---|---|---|---|
| 0.000 | 0 / 4 | 0 / 11 | **0.733** |
| 0.100 | 1 / 4 | 2 / 11 | 0.667 |
| 0.220 | 2 / 4 | 6 / 11 | 0.467 |
| 0.263 | 4 / 4 | 9 / 11 | 0.400 |

The two populations overlap outright: the lowest-scoring **answerable** question
scores 0.069, the highest-scoring **unanswerable** one scores 0.262. No line
separates them, so every threshold that catches a refusal throws away more real
answers than it saves. Zero is optimal, and now it is optimal *for a reason on
record* rather than by argument.

The conclusion generalises further than the number: refusal is a judgement about
meaning, and cosine similarity does not encode meaning. It belongs to the model
reading the context, which is what `app/prompts/rag/system.md` instructs, and
what `MIN_EVIDENCE_SCORE` in `generation.py` deliberately does not attempt.

## The first baseline

```
recall             1.000     precision          0.348
mrr                0.864     answer_match       0.273
refusal_accuracy   0.000     citation_accuracy  1.000
faithfulness       1.600     relevance          1.867     completeness  1.667
```

Read honestly:

- **recall 1.000, mrr 0.864**: retrieval finds the right document for every
  answerable question, usually at rank 1. This is the number that bounds
  everything else, and it is good.
- **precision 0.348**: `top_k=5` over a four-document corpus returns nearly
  everything. An artefact of a small dataset, not a defect.
- **refusal_accuracy 0.000**: all four unanswerable questions got answered. The
  extractive offline provider *cannot* refuse: it quotes the best-overlapping
  sentence and always produces something. This is a limitation of the offline
  model, and it stays in the baseline as a labelled gap rather than being
  excluded to make the report look better.
- **answer_match 0.273, judge scores ~1.7**: the same cause. A provider that
  quotes a single sentence rarely contains a specific expected substring.

**Every one of these changes the moment a real key exists**, which is the point
of committing the baseline: the change will be visible as a diff.

## Decisions worth arguing about

**Examples name documents, not chunks.** The obvious design records the chunk id
that should be retrieved, and it is unusable, because chunk ids depend on
`chunk_size_tokens` and `chunk_overlap_tokens`, the settings under test. A golden
set keyed on them would need regenerating every time the variable changed, which
makes it a mirror rather than a measurement.

**Refusal examples are excluded from the recall average.** They score a free 1.0
by construction, so including them would let a dataset improve its own headline
number by adding more unanswerable questions.

**An unparseable judge verdict scores 1, never "skip".** Skipping drops the
example from the average, which *raises* the score of a run whose answers broke
the judge. A harness must never make a broken run look better than a working one.

**The judge's biases are documented in its own module docstring**: verbosity
position, self-preference, with what is done about each, and for
self-preference an admission that nothing can be while the judge and the
generator are the same model.

## Bugs and surprises

Fewer than previous milestones, and the reason is worth noting: `metrics.py` and
`datasets.py` are pure functions with no I/O, so they were exhaustively testable
before anything used them. The parts that broke at M6 and M7 were the parts that
touched the world.

**The linter and a comment disagreed.** A `frozenset("...".split())` was
rewritten to a list literal by `ruff --fix`, leaving a comment explaining a form
the code no longer had. Caught reviewing the diff; the comment went.

**`asyncio.run` cannot nest.** The CLI test was written `async def`, so
`cli.main()` tried to start a loop inside a running one. The tests are
synchronous instead, correct, since the thing under test is a process.

## Verified at runtime

`make eval` against the real database, four ways:

- A clean run: exit 0, report written, recall 1.000.
- A planted unbeatable baseline: **exit 1**, failing metrics listed. This is the
  assertion the milestone rests on: a gate that cannot fail is not a gate.
- A shortfall inside the 2% tolerance: exit 0.
- After every run, `SELECT count(*) FROM organizations WHERE name LIKE 'eval-%'`
  returns 0, including when the run raises partway, because cleanup is in a
  `finally`. A leftover corpus would inflate the next run's recall, and the
  improvement would look real.

## Gate

```
ruff · ruff format · mypy --strict (209 files) · alembic check
456 tests, 2 skipped · 98.35% coverage (gate 97%)
```

Pyramid: 264 unit / 88 integration / 106 e2e. No migration, M8 adds no tables.

## Known gaps, deliberately left

**The 2% regression tolerance is a guess.** Named as one. It should tighten once
several runs show how much these numbers move on their own.

**One dataset, fifteen questions.** Small on purpose: a set a human can read in
one sitting is a set a human will keep honest, but small enough that a single
example moves `recall` by nine points. More questions before more metrics.

**No per-question cost tracking.** The `Completion` token counts exist and the
runner does not aggregate them. M12 owns cost, and doing it here would be a
second, worse version.

**Nothing is tuned yet.** M8 built the instrument and used it once, on the one
question where there was a strong prior to overturn. Sweeping `chunk_size_tokens`
and `chunk_overlap_tokens` is now a `for` loop over `make eval`, and it is worth
running against a *real* embedder, because a sweep against the hashing one would
tune the geometry for lexical overlap and be actively misleading.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
make eval                    # exit 1 on a regression
make eval-baseline           # accept these scores, after reading the report
```

Reports land in `backend/var/eval/` (gitignored). The baseline at
`backend/app/evaluation/baselines/handbook.json` is committed on purpose.
