# ruff: noqa: F401  — remove once this module is implemented (M10)
"""Conversation business logic: create threads, append messages (append-only),
kick off an agent run for each user message and link the reply to it."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.conversation import Conversation
from app.models.message import Message
from app.services.agent_service import AgentService

# TODO(M10): class ConversationService — create, list(org_id, user_id),
#            add_user_message -> triggers AgentService.run, persists assistant reply
