# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M9)
"""`agent_runs` — one row per top-level agent invocation.

The aggregate root of execution: unit of observability AND billing.
checkpoint jsonb holds LangGraph state so runs survive restarts/approvals.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# TODO(M9): class RunStatus(enum.StrEnum) — running | paused_for_approval |
#           succeeded | failed | cancelled
# TODO(M9): class AgentRun(Base) — organization_id FK, conversation_id FK nullable,
#           triggered_by FK users, agent_name, status, input, output, error,
#           checkpoint JSONB, started_at, finished_at, total_tokens, cost_usd;
#           relationships steps -> AgentStep, approvals -> Approval
