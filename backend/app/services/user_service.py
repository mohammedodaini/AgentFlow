"""User business logic (profile reads/updates). Registration lives in
app/auth/service.py because it is an auth flow (tokens, hashing)."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.user import User
from app.schemas.user import UserUpdate

logger = structlog.get_logger(__name__)


class UserService:
    """Profile reads and edits.

    Small on purpose, and likely to stay that way. A `User` in this application
    is an identity, not a container for product data — anything a *company*
    owns lives on `Organization`. When this class starts growing methods about
    documents or conversations, that is the signal something has been attached
    to the wrong entity.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_profile(self, user_id: uuid.UUID) -> User:
        """Load one user, or raise `NotFoundError`.

        Returns the model, not a schema. Services speak in domain objects; the
        route converts to `UserRead` on the way out. That is what keeps a
        service callable from a worker or an agent tool, neither of which has
        any use for an HTTP response shape.
        """
        user = await self._session.scalar(select(User).where(User.id == user_id))

        if user is None:
            message = "User not found"
            raise NotFoundError(message)

        return user

    async def update_profile(self, user_id: uuid.UUID, changes: UserUpdate) -> User:
        """Apply a partial update to a profile.

        `exclude_unset=True` is what makes this a genuine PATCH: it yields only
        the fields the client actually sent. Without it every omitted optional
        field arrives as `None` and the update blanks it — so a client changing
        one field would silently erase the others.
        """
        user = await self.get_profile(user_id)
        updates = changes.model_dump(exclude_unset=True)

        for field, value in updates.items():
            setattr(user, field, value)

        await self._session.flush()

        logger.info("user.profile_updated", user_id=str(user_id), fields=sorted(updates))
        return user
