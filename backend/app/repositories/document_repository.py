"""Document queries. Every method is scoped by organization.

Layer: repositories. Takes a session, owns no transaction — the caller commits
(`app/db/deps.py` for HTTP, the worker's own `async with` for background jobs).

Why a repository here, when M2 and M3 put queries straight in their services
-----------------------------------------------------------------------------
A repository that only wraps `session.get` earns nothing and costs a file. This
one exists because documents have two callers with genuinely different needs —
the API asks for a page of a tenant's documents filtered by status, and the
ingestion worker asks to move one row through a state machine — and because the
same `WHERE organization_id = ?` clause would otherwise be copy-pasted into six
places. Copy-pasted tenancy filters are how a multi-tenant system leaks: the
leak is one forgotten `.where()`, in one method, added on a Friday.

Here it is forgettable in exactly one file — a file that can be read in full in
a minute, and tested directly.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document, DocumentStatus

MAX_PAGE_SIZE = 100
"""Hard ceiling on `limit`, applied by the service. Named here because it is a
property of the query, not of the endpoint: without it a client can ask for
`limit=1000000` and turn one request into a table scan."""


class DocumentRepository:
    """Reads and writes for the `documents` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scoped(self, organization_id: uuid.UUID) -> Select[tuple[Document]]:
        """The base query every read starts from.

        One private helper rather than a repeated `.where()` in each method:
        that is the point of the class. A new read method physically cannot
        forget the tenancy filter, because it has nowhere else to begin.
        """
        return select(Document).where(Document.organization_id == organization_id)

    async def add(self, document: Document) -> Document:
        """Stage a new document and flush so its id exists.

        `flush`, not `commit`. The row must be visible to the rest of *this*
        transaction — the service needs the id to build the storage key — while
        the decision to make it permanent stays with whoever owns the
        transaction. M2 learned this one the hard way: `default=uuid7` is a
        column default evaluated at flush, so `document.id` is None until here.
        """
        self._session.add(document)
        await self._session.flush()
        return document

    async def get(self, organization_id: uuid.UUID, document_id: uuid.UUID) -> Document | None:
        """One document, or None if it does not exist *for this tenant*.

        Returns None rather than raising: "not found" is not exceptional at
        this layer, and the service is the layer that knows whether a missing
        row means a 404 or an empty list. Raising `NotFoundError` here would
        force the worker — which has a legitimate reason to shrug at a document
        deleted while its job sat in the queue — to catch an exception in order
        to express "never mind".
        """
        # Annotated rather than returned inline: `AsyncSession.scalar` is typed
        # to return Any, so returning it directly would silently hand `Any` to
        # every caller and switch off type checking downstream of this method.
        document: Document | None = await self._session.scalar(
            self._scoped(organization_id).where(Document.id == document_id)
        )
        return document

    async def list_for_organization(
        self,
        organization_id: uuid.UUID,
        *,
        status: DocumentStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Document]:
        """A page of the tenant's documents, newest first.

        Ordered by `id` descending rather than `created_at` descending, which
        looks wrong and is not. UUIDv7 embeds a millisecond timestamp in its
        leading bits (ADR-0003), so id order tracks insertion order, and it
        sorts on the primary key index instead of needing a second index on a
        timestamp column.

        More importantly it is a *total* order, which `created_at` is not.
        `created_at` defaults to `now()`, and in PostgreSQL `now()` is
        transaction start time — so every row written by one request shares a
        timestamp exactly. Ordering by it leaves ties for the database to break
        however it likes, which across a page boundary means silently
        duplicating or skipping rows between two identical requests.

        The honest limit: within a single millisecond, ids differ only in their
        random bits, so relative order there is arbitrary (our `uuid7()` is the
        pure-random variant, not the monotonic-counter one). Arbitrary but
        *stable*, which is the property pagination actually needs.
        """
        query = self._scoped(organization_id)

        if status is not None:
            query = query.where(Document.status == status)

        result = await self._session.scalars(
            query.order_by(Document.id.desc()).limit(limit).offset(offset)
        )
        return list(result)

    async def count_for_organization(
        self, organization_id: uuid.UUID, *, status: DocumentStatus | None = None
    ) -> int:
        """Total matching rows, ignoring limit/offset — the `total` in `Page`.

        A second round trip, and worth it: without a total the client cannot
        tell "20 documents" from "20 of 4,000", which is the difference between
        a page and a mystery.
        """
        query = select(func.count()).select_from(Document)
        query = query.where(Document.organization_id == organization_id)

        if status is not None:
            query = query.where(Document.status == status)

        total = await self._session.scalar(query)
        # `count()` cannot return NULL, but `scalar()` is typed Optional.
        # Being explicit beats a `# type: ignore` that would also hide a real
        # None on the day this query grows a join.
        return total or 0

    async def set_status(
        self,
        document: Document,
        status: DocumentStatus,
        *,
        error: str | None = None,
    ) -> Document:
        """Move a document through the ingestion state machine.

        Takes the loaded object rather than an id, so the caller has already
        proved it read the row within its tenant scope. An id-only signature
        would be an unscoped write hiding inside a scoped class.

        `error` is always assigned, never only on failure. A document that
        fails, gets re-ingested, and succeeds must not keep the old message: a
        stale error sitting next to `status=ready` is worse than no error at
        all, because somebody will believe it.
        """
        document.status = status
        document.error = error
        await self._session.flush()
        return document

    async def delete(self, document: Document) -> None:
        """Remove the row. Chunks follow via `ON DELETE CASCADE` from M6.

        Deleting the *bytes* is the service's job, not this one's. A repository
        that reached into object storage would need object storage in its
        constructor, and then every test of a query would need a storage double.
        """
        await self._session.delete(document)
        await self._session.flush()
