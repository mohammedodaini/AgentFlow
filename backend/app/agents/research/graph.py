# ruff: noqa: F401  — remove once this module is implemented (M15)
"""research agent graph. Gather external info on leads/companies.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M15): build_graph() -> compiled StateGraph with Postgres checkpointer
