# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""arq task: post-run memory extraction (docs/agents.md rule 5: memory is
asynchronous — extraction must never add latency to the user)."""

from __future__ import annotations

import uuid

from app.memory.writer import extract_and_store

# TODO(M10): async def extract_memories(ctx, run_id)
