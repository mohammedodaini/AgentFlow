# ruff: noqa: F401  — remove once this module is implemented (M12)
"""Approval API shapes — what the inbox shows and what a decision looks like."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M12): ApprovalRead — id, agent_run_id, requested_action, status, created_at
# TODO(M12): ApprovalDecision — optional reason (audited)
