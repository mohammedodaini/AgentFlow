# ruff: noqa: F401  — remove once this module is implemented (M15)
"""planner agent graph. Pure reasoning: decompose a complex request into ordered steps for other agents. No tools.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M15): build_graph() -> compiled StateGraph with Postgres checkpointer
