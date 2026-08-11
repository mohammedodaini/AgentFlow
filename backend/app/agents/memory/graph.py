# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""memory agent graph. Runs AFTER the response is sent (async — never adds user latency):
extract durable facts, store/update/decay.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M10): build_graph() -> compiled StateGraph with Postgres checkpointer
