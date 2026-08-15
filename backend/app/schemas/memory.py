"""Memory API shapes — visibility into what the agent has learned (M10).

Layer: schemas — the API boundary.

Why a read endpoint exists at all
---------------------------------
Long-term memory is the one feature in this system that changes answers without
anybody asking it to. A retrieved document is cited and checkable; a memory is an
uncited assertion that silently shapes every future reply. A store nobody can
inspect is therefore not a neutral convenience — it is an unreviewable input.

So `MemoryRead` exists in the same milestone as the first writer, and it
publishes importance and last-access as well as the text. "Why did it say that?"
is answerable only if someone can see both the memory and how strongly it ranked.

Two columns are deliberately withheld. `embedding` is 1536 floats that mean
nothing to a reader and would dominate every response. `content_hash` is an
implementation detail of the uniqueness constraint, and publishing it would let a
caller test whether one *specific* sentence is already remembered — a membership
oracle over other people's memories, the same class of leak that keeps
`storage_uri` out of `DocumentRead`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.memory import MemoryScope
from app.schemas.common import APIModel


class MemoryRead(APIModel):
    """One durable fact the agent believes."""

    id: uuid.UUID
    scope: MemoryScope
    content: str
    importance: float
    last_accessed_at: datetime
    created_at: datetime
