# ruff: noqa: F401  — remove once this module is implemented (M12)
"""Approval business logic: create pending approvals (called by agent tools),
list the inbox, record decisions, trigger run resumption.

Decision = DB update + audit event + AgentService.resume_after_approval.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApprovalExpiredError, NotFoundError
from app.models.approval import Approval
from app.services.agent_service import AgentService

# TODO(M12): class ApprovalService — request(run_id, action), inbox(org_id),
#            approve(id, user_id), reject(id, user_id, reason), expire_stale()
