# ruff: noqa: F401  — remove once this module is implemented (M12)
"""`approvals` — human-in-the-loop as a DATABASE RECORD, not an in-memory flag.

Must survive restarts and appear in audits. requested_action jsonb carries the
full pending side effect (e.g. the exact email draft the agent wants to send).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# TODO(M12): class ApprovalStatus(enum.StrEnum) — pending|approved|rejected|expired
# TODO(M12): class Approval(Base) — agent_run_id FK, organization_id FK,
#            requested_action JSONB, status, decided_by FK users nullable, decided_at
