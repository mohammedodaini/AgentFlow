# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M15)
"""supervisor agent graph. Entry point of the multi-agent graph: classify intent, route to ONE
specialist or ask Planner for a plan, sequence the plan. Keeps the call graph a tree.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M15): build_graph() -> compiled StateGraph with Postgres checkpointer
