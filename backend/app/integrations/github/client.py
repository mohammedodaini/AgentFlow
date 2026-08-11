# ruff: noqa: F401  — remove once this module is implemented (M14)
"""github API client — the ONLY code that speaks github's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M14): GitHubClient — list_issues, create_issue (approval-gated)
