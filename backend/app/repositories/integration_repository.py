"""Data access for `integrations` and their credentials.

Layer: repositories. Takes a session, owns no transaction. Every method takes an
`organization_id` and every query filters on it — the same non-negotiable rule as
every other repository here.

The eager load is not a performance choice
------------------------------------------
`selectinload(Integration.tokens)` appears on every read, because the caller is a
service that will immediately ask "does this need refreshing?" — and a lazy
relationship under asyncio raises `MissingGreenlet` from inside whatever happened
to touch it first. M9 paid for that lesson twice in one milestone.

There is a second reason specific to this table. A lazy load of the *token* is a
query that fetches a decryptable credential at an unpredictable moment, from a
call site that does not look like it is reading secrets. Making it explicit keeps
"when do we load credentials?" answerable by reading one file.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.integration import Integration, IntegrationStatus, Provider


class IntegrationRepository:
    """Tenant-scoped reads and writes for connected accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, organization_id: uuid.UUID, integration_id: uuid.UUID
    ) -> Integration | None:
        """One integration, or None if it belongs to another tenant."""
        integration: Integration | None = await self._session.scalar(
            select(Integration)
            .where(
                Integration.organization_id == organization_id,
                Integration.id == integration_id,
            )
            .options(selectinload(Integration.tokens))
        )
        return integration

    async def get_active(
        self, organization_id: uuid.UUID, provider: Provider
    ) -> Integration | None:
        """The live connection for one provider, if there is one.

        At most one row can match: the partial unique index on `(organization_id,
        provider) WHERE status = 'active'` guarantees it. Without that guarantee
        this method would have to *pick* — and any rule for picking would silently
        strand the other row's credential.
        """
        integration: Integration | None = await self._session.scalar(
            select(Integration)
            .where(
                Integration.organization_id == organization_id,
                Integration.provider == provider,
                Integration.status == IntegrationStatus.ACTIVE,
            )
            .options(selectinload(Integration.tokens))
        )
        return integration

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Integration]:
        """Every connection this organization has ever made, newest first.

        Including revoked and disconnected ones, deliberately. "Google Calendar —
        needs reconnecting" is the most useful thing this endpoint can say, and
        filtering to active rows would render it as an absence indistinguishable
        from never having connected at all.
        """
        return list(
            await self._session.scalars(
                select(Integration)
                .where(Integration.organization_id == organization_id)
                .order_by(Integration.created_at.desc())
                .options(selectinload(Integration.tokens))
            )
        )

    async def upsert_active(
        self,
        *,
        organization_id: uuid.UUID,
        provider: Provider,
        connected_by: uuid.UUID | None,
        scopes: list[str],
        external_account_id: str | None,
    ) -> Integration:
        """Reuse the live row for this provider, or open one.

        Reconnecting must not create a second row — the partial unique index would
        reject it, and rightly. Reusing also preserves the id, so anything already
        referring to this integration keeps working across a reconnect.

        A row that was `REVOKED` or `DISCONNECTED` is *not* reused: it is history,
        and resurrecting it would overwrite the record of when the previous
        connection ended. A fresh row is the honest representation of a fresh
        grant.
        """
        integration = await self.get_active(organization_id, provider)

        if integration is None:
            integration = Integration(
                organization_id=organization_id,
                provider=provider,
                status=IntegrationStatus.ACTIVE,
                # Set explicitly, and it is load-bearing. Every *read* here uses
                # `selectinload`, so callers may assume `tokens` is populated —
                # but a newly constructed object leaves the relationship
                # *unloaded*, and after the flush below it is a persistent object
                # whose first `.tokens` access is a lazy SELECT. Under asyncio that
                # raises `MissingGreenlet`, from inside the service line that
                # stores the credential.
                #
                # Assigning before the flush marks the relationship loaded while
                # the object is still pending, so no query is ever attempted. The
                # eager-load contract this module documents had a hole exactly one
                # code path wide, and this closes it.
                tokens=None,
            )
            self._session.add(integration)

        integration.connected_by = connected_by
        integration.scopes = scopes
        integration.external_account_id = external_account_id
        await self._session.flush()
        return integration
