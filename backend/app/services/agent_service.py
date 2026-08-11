# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M9)
"""The bridge between the web world and the agent world.

Creates the agent_runs row, invokes the LangGraph graph (app/agents/) with
checkpointing, records steps/tokens/cost, handles pause-for-approval and
resume. Routes and workers call THIS — never a graph directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AgentExecutionError
from app.repositories.agent_run_repository import AgentRunRepository

# TODO(M9): class AgentService — start_run(org_id, user_id, agent_name, input),
#           get_run, list_runs, cancel_run
# TODO(M12): resume_after_approval(run_id, decision) — loads checkpoint, continues graph
