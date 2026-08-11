# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Conversation + message API shapes."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.common import APIModel

# TODO(M10): ConversationRead — id, title, created_at
# TODO(M10): MessageCreate — content
# TODO(M10): MessageRead — id, role, content, agent_run_id, created_at
