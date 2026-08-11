# ruff: noqa: F401  — remove once this module is implemented (M12)
"""/approvals — the human-in-the-loop inbox.

Approve resumes the paused LangGraph run from its checkpoint;
reject fails it cleanly. Both write the decision to the approvals row.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.approval import ApprovalDecision, ApprovalRead
from app.services.approval_service import ApprovalService

router = APIRouter(prefix="/approvals", tags=["approvals"])

# TODO(M12): GET /?status=pending — the inbox
# TODO(M12): POST /{id}/approve · POST /{id}/reject — records decided_by/decided_at,
#            resumes or terminates the run
