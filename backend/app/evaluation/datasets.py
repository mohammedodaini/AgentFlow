"""Golden datasets: versioned Q&A pairs with expected sources/answers.

Stored as files under evaluation/data/ (diffable in PRs — a dataset change
is a review event, same as a code change).
"""

from __future__ import annotations

from pathlib import Path

# TODO(M8): GoldenExample model; load_dataset(name) -> list[GoldenExample]
