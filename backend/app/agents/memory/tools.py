# ruff: noqa: F401  — remove once this module is implemented (M10)
"""memory agent tools: store_memory(), search_memory()

Rule: every tool wraps a SERVICE method — tenancy, logging, and permissions
apply to agents automatically. Side-effect tools (*) raise the approval
interrupt before executing (M12).
"""

from __future__ import annotations

from langchain_core.tools import tool

# TODO(M10): implement the tools listed above
