"""Running a worker task inside a test's transaction.

The worker is a separate process in production: it opens its own session, holds
its own storage client and embedder, and commits for real. None of that is
compatible with a suite whose isolation depends on one open transaction per
test (ADR-0006) — a real session would commit outside it and leak rows into
every test that followed.

This module is the adapter. It builds the `ctx` dict `WorkerSettings.on_startup`
builds, but backed by the test's own session, so a task can be invoked directly
and everything it writes is rolled back with the rest of the test.

It lives here rather than in a test file because two of them need it now: the
integration tests that exercise the task's own decisions, and the end-to-end
search test, which needs a document that was genuinely ingested rather than one
whose chunks were placed by hand to match the query.

Not a fixture. It takes arguments — a substitute embedder, a retry number — and
a fixture that takes arguments is a function wearing a costume.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import create_llm
from app.llm.base import LLMProvider
from app.models import Document
from app.models.task import Task
from app.rag.embeddings import EmbeddingProvider, create_embedder
from app.storage import ObjectStorage
from app.workers.tasks.ingestion import ingest_document


class _BorrowedSession:
    """Hands the worker the test's session without ever closing it.

    `ingest_document` opens `async with session_factory() as session`, which in
    production creates and disposes a session. Here it must join the test's
    transaction instead — otherwise the worker commits for real and the
    rollback at the end of the test never sees any of it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def worker_context(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    embedder: EmbeddingProvider | None = None,
    llm: LLMProvider | None = None,
    job_try: int = 1,
    max_tries: int = 3,
) -> dict[str, Any]:
    """The dict `WorkerSettings.on_startup` builds, assembled by hand.

    Building it explicitly is honest rather than a shortcut: it is just a dict
    in production too.

    `settings` and `embedder` joined it at M6, when the task began chunking and
    embedding. They are built the way the worker builds them rather than
    stubbed, so tests exercise the real chunker and the real offline embedder —
    the failures worth catching (a chunk over the limit, a vector of the wrong
    width) are invisible to a stub that returns whatever it is asked for.

    `llm` joined at M10, when memory extraction made the worker a caller of
    models. It is the one entry usually *overridden*: extraction is the single
    place where the reply's exact format decides whether anything is stored, so a
    test about storage should choose the reply rather than depend on the offline
    provider's judgement of what is worth remembering.
    """
    settings = get_settings()
    return {
        "session_factory": lambda: _BorrowedSession(session),
        "storage": storage,
        "settings": settings,
        "embedder": embedder or create_embedder(settings),
        "llm": llm or create_llm(settings),
        "job_try": job_try,
        "max_tries": max_tries,
    }


async def run(ctx: dict[str, Any], document: Document, task: Task) -> dict[str, Any]:
    """Invoke `ingest_document` the way arq would, with ids as strings."""
    return await ingest_document(
        ctx,
        document_id=str(document.id),
        organization_id=str(document.organization_id),
        task_id=str(task.id),
    )
