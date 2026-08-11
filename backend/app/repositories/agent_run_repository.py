# ruff: noqa: F401  — remove once this module is implemented (M9)
"""Agent run/step queries. Repository justified: trace joins, status transitions,
cost aggregation for billing."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.agent_step import AgentStep

# TODO(M9): class AgentRunRepository — create_run, append_step, finish_run,
#           get_with_steps(org_id, run_id), list_by_org(org_id, status=None)
# TODO(M12): mark_paused_for_approval(run_id) / resume(run_id)
