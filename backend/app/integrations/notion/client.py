# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M14)
"""notion API client — the ONLY code that speaks notion's wire format.

Consumed by services and agent tools; returns OUR domain shapes, never raw
provider payloads.
"""

from __future__ import annotations

import httpx

from app.integrations.base import BaseClient

# TODO(M14): NotionClient — search_pages, get_page (feeds RAG ingestion)
