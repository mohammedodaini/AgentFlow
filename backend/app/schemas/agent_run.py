# ruff: noqa: F401  — remove once this module is implemented (M9)
"""Agent run / step API shapes for the observability endpoints."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M9): AgentRunRead — id, agent_name, status, started_at, finished_at,
#           total_tokens, cost_usd
# TODO(M9): AgentStepRead — step_index, node_name, tool_name, latency_ms, tokens
