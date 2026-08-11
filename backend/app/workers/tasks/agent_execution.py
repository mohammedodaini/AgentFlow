# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M9)
"""arq task: execute long agent runs off the request path.

The API creates the run row and enqueues; this task drives AgentService.
Also where paused runs resume after approval (M12).
"""

from __future__ import annotations

import uuid

from app.services.agent_service import AgentService

# TODO(M9): async def execute_agent_run(ctx, run_id)
# TODO(M12): async def resume_agent_run(ctx, run_id)
