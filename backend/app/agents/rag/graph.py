# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M9)
"""rag agent graph. THE FIRST AGENT (single-agent milestone). Answers questions over the org
knowledge base with citations.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M9): build_graph() -> compiled StateGraph with Postgres checkpointer
