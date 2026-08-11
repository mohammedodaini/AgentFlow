# mypy: ignore-errors
# ^ remove this pragma when the module below is implemented
# ruff: noqa: F401  — remove once this module is implemented (M10)
"""/conversations — chat threads and their messages."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.schemas.conversation import ConversationRead, MessageCreate, MessageRead
from app.services.conversation_service import ConversationService

router = APIRouter(prefix="/conversations", tags=["conversations"])

# TODO(M10): POST / · GET / · GET /{id}/messages
# TODO(M10): POST /{id}/messages — appends user message, triggers agent run,
#            streams assistant reply (WebSocket or SSE — decide at M10)
