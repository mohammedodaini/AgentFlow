# ruff: noqa: F401  — remove once this module is implemented (M11)
"""Integration API shapes. Token values NEVER appear in any response schema."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M11): IntegrationRead — id, provider, status, external_account_id, connected_at
