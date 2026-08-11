# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M9)
"""/agent-runs — observability API: list runs, inspect a run's step trace."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.agent_run import AgentRunRead, AgentStepRead
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])

# TODO(M9): GET / — org's runs (status, cost, tokens) · GET /{id} — run detail
# TODO(M9): GET /{id}/steps — the trace · POST /{id}/cancel
