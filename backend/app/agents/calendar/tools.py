# ruff: noqa: F401  — remove once this module is implemented (M12)
"""calendar agent tools: list_events(), create_event()*, create_reminder()* — writes approval-gated

Rule: every tool wraps a SERVICE method — tenancy, logging, and permissions
apply to agents automatically. Side-effect tools (*) raise the approval
interrupt before executing (M12).
"""

from __future__ import annotations

from langchain_core.tools import tool

# TODO(M12): implement the tools listed above
