# ruff: noqa: F401  — remove once this module is implemented (M9)
"""Shared LangGraph state — agents communicate through THIS typed object,
never free-text messages between each other (docs/agents.md rule 1)."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# TODO(M9): class AgentState(TypedDict) — messages, organization_id, user_id,
#           retrieved_chunks, usage
# TODO(M15): plan: list[PlanStep], current_step, intermediate_results
