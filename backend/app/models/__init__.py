"""Model registry — the one import that makes the whole schema visible.

Alembic autogenerate diffs `Base.metadata` against the live database. A model
class only joins that metadata when its module is *imported*, so a table whose
module nobody imported is silently invisible: autogenerate sees no such table
and cheerfully generates a migration that drops it.

Importing every model here, and importing `app.models` from alembic/env.py,
makes that failure impossible. Add each new model to this file as it lands.
"""

from __future__ import annotations

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.membership import Membership, Role
from app.models.organization import Organization
from app.models.user import User

# TODO(M5): documents, tasks     · TODO(M6): document_chunks
# TODO(M9): agent_runs, agent_steps
# TODO(M10): conversations, messages, memories
# TODO(M11): integrations, oauth_tokens
# TODO(M12): approvals           · TODO(M16): events

__all__ = [
    "Base",
    "Membership",
    "Organization",
    "Role",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "User",
]
