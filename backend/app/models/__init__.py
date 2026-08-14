"""Model registry — the one import that makes the whole schema visible.

Alembic autogenerate diffs `Base.metadata` against the live database. A model
class only joins that metadata when its module is *imported*, so a table whose
module nobody imported is silently invisible: autogenerate sees no such table
and cheerfully generates a migration that drops it.

Importing every model here, and importing `app.models` from alembic/env.py,
makes that failure impossible. Add each new model to this file as it lands.
"""

from __future__ import annotations

from app.models.agent_run import AgentRun, RunStatus
from app.models.agent_step import AgentStep
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.document import Document, DocumentSource, DocumentStatus
from app.models.document_chunk import EMBEDDING_DIMENSIONS, DocumentChunk
from app.models.membership import Membership, Role
from app.models.organization import Organization
from app.models.task import INGEST_DOCUMENT, Task, TaskStatus
from app.models.user import User

# TODO(M10): conversations, messages, memories
# TODO(M11): integrations, oauth_tokens
# TODO(M12): approvals           · TODO(M16): events

__all__ = [
    "EMBEDDING_DIMENSIONS",
    "INGEST_DOCUMENT",
    "AgentRun",
    "AgentStep",
    "Base",
    "Document",
    "DocumentChunk",
    "DocumentSource",
    "DocumentStatus",
    "Membership",
    "Organization",
    "Role",
    "RunStatus",
    "Task",
    "TaskStatus",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
]
