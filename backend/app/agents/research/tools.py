# ruff: noqa: F401  — remove once this module is implemented (M15)
"""research agent tools: web_search(), fetch_url()

Rule: every tool wraps a SERVICE method — tenancy, logging, and permissions
apply to agents automatically. Side-effect tools (*) raise the approval
interrupt before executing (M12).
"""

from __future__ import annotations

from langchain_core.tools import tool

# TODO(M15): implement the tools listed above
