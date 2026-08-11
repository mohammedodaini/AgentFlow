"""Decay & summarization policies — what to forget, what to compress.

Pure decision logic (testable without DB): given (importance, last_accessed,
count), return keep | decay | summarize | delete.
"""

from __future__ import annotations

# TODO(M10): decay_score(importance, last_accessed_at) -> float
# TODO(M10): plan_maintenance(memories) -> list[MaintenanceAction]
