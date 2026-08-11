# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M14)
"""stripe API client — the ONLY code that speaks stripe's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M14): StripeClient — API-key auth (no OAuth): customers, invoices for proposal/billing
#   context
