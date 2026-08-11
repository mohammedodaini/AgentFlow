# ruff: noqa: F401  — remove once this module is implemented (M12)
"""email agent graph. Draft, summarize, and (after approval) send email.

Invoked via services/agent_service.py — never directly from a route.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.state import AgentState

# TODO(M12): build_graph() -> compiled StateGraph with Postgres checkpointer
