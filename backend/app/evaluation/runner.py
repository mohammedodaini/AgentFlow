# ruff: noqa: F401  — remove once this module is implemented (M8)
"""Eval runner: dataset -> system -> metrics + judge -> report vs baseline.

THE REGRESSION GATE: no prompt/retrieval change ships without this
confirming no regression on the golden set (docs/agents.md).
Runs via `make eval` and in CI.
"""

from __future__ import annotations

from app.evaluation import datasets, judge, metrics

# TODO(M8): async run_eval(dataset_name) -> EvalReport; baseline compare + exit code
