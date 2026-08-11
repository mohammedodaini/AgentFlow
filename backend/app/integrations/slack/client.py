# ruff: noqa: F401  — remove once this module is implemented (M14)
"""slack API client — the ONLY code that speaks slack's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M14): SlackClient — post_message (approval-gated), list_channels
