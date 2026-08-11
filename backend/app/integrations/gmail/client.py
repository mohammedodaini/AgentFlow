# ruff: noqa: F401  — remove once this module is implemented (M12)
"""gmail API client — the ONLY code that speaks gmail's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M12): GmailClient — search_messages, get_message, create_draft, send (send is approval-gated)
