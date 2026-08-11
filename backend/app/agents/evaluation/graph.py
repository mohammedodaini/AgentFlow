# ruff: noqa: F401  — remove once this module is implemented (M8)
"""evaluation agent graph. Offline only: score runs/answers against golden datasets, LLM-as-judge. Uses app/evaluation/ harness.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M8): build_graph() -> compiled StateGraph with Postgres checkpointer
