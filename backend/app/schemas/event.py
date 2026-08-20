"""Audit event API shapes.

Layer: schemas — the API boundary.

The whitelist matters more here than almost anywhere else. `Event.payload` is
JSONB written by a dozen call sites, and `EventService._safe` strips credentials
on the way in — but a schema that published the row wholesale would depend on
that scrubber being perfect forever. This publishes the columns and the payload
and nothing else, so a column added later is invisible until somebody adds it
here on purpose.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import APIModel


class EventRead(APIModel):
    """One entry in the audit trail."""

    id: uuid.UUID
    event_type: str = Field(description="What happened, e.g. user.signed_in")
    actor_user_id: uuid.UUID | None = Field(default=None, description="Who did it, if a person did")
    actor_agent_run_id: uuid.UUID | None = Field(
        default=None, description="Which run did it, if an agent did"
    )
    payload: dict[str, Any] = Field(description="Facts about the event. Never secrets")
    ip_address: str | None = None
    created_at: datetime
