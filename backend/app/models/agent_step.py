# ruff: noqa: F401  — remove once this module is implemented (M9)
"""`agent_steps` — the trace: every LLM call and tool call within a run.

This table answers "why did the agent do that?". Never exists without a run.
Future: partition by month when hot (docs/database.md).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M9): class AgentStep(Base) — agent_run_id FK, step_index, node_name,
#           tool_name, tool_input JSONB, tool_output JSONB, latency_ms, tokens
