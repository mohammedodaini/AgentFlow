"""The producer side of the queue: how the API hands work to the worker.

Layer: workers. Imported by the API (to enqueue) and by nothing in the worker
itself — `settings.py` is the consumer side.

Why a second Redis pool
-----------------------
`app.state.redis` already exists (M3, the refresh-token denylist) and this does
*not* reuse it. That client is built with `decode_responses=True`, which turns
every reply into `str`; arq serialises job payloads and reads them back as
`bytes`, so sharing the client corrupts every job at the moment it is dequeued.
The failure is not a clean error either — it surfaces inside arq as a
deserialisation exception that mentions no configuration flag at all.

Two pools against one server is a genuine cost: two sets of connections, two
things to close on shutdown. It is the right trade, because the alternative is
one pool whose settings are wrong for one of its two users.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request

from app.core.config import Settings
from app.models.task import INGEST_DOCUMENT

logger = structlog.get_logger(__name__)


class JobQueue(Protocol):
    """The one method the application needs from arq.

    A `Protocol` for the same reason `ObjectStorage` is one, plus a sharper
    one: without it, testing anything that enqueues requires a live Redis. With
    it, a test substitutes a five-line class that records calls, and the
    assertion becomes "the service enqueued exactly one ingestion job for this
    document" — which is the behaviour that actually matters, and which is
    invisible when the only observable is a job sitting in Redis.

    `ArqRedis` satisfies this structurally; nothing had to change in arq.
    """

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        """Push a job. arq's own keyword arguments are underscore-prefixed
        (`_job_id`, `_defer_by`), which is why `**kwargs` is untyped here."""
        ...


def build_redis_settings(settings: Settings) -> RedisSettings:
    """Translate our `REDIS_URL` into arq's own settings object.

    arq does not take a URL, and hand-writing host/port/database in two places
    is how the API and the worker end up pointed at different Redis databases —
    a bug whose only symptom is that jobs are accepted and never run.
    """
    return RedisSettings.from_dsn(settings.redis_url)


async def create_queue(settings: Settings) -> ArqRedis:
    """Open the producer pool. Called once per process, from `lifespan()`.

    `async`, and therefore not usable at import time — which is a feature. It
    makes "connect to Redis" something a process does when it starts, rather
    than a side effect of importing a module.
    """
    return await create_pool(build_redis_settings(settings))


def get_queue(request: Request) -> JobQueue:
    """Read the pool `lifespan()` stored on the application.

    Typed as the protocol rather than `ArqRedis`, so a route cannot reach the
    rest of the Redis client through the queue handle — the same narrowing
    `get_storage` does.
    """
    queue: JobQueue = request.app.state.queue
    return queue


def ingestion_job_id(task_id: uuid.UUID) -> str:
    """The arq job id for one ingestion task.

    Derived from the task row rather than random, because arq treats a job id
    as an idempotency key: enqueueing an id that is already queued is a no-op.
    So a retried HTTP request — or a sweeper re-enqueueing a task that looks
    stuck — cannot produce two workers parsing the same PDF at once.
    """
    return f"ingest:{task_id}"


async def enqueue_ingestion(
    queue: JobQueue,
    *,
    document_id: uuid.UUID,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
) -> None:
    """Ask a worker to ingest a document.

    All three ids travel in the payload, and each is load-bearing.
    `document_id` is the work. `organization_id` is what lets the worker use the
    same tenant-scoped repository the API uses instead of an unscoped
    `SELECT * FROM documents WHERE id = ?` — the worker is not exempt from
    tenancy just because no user is watching. `task_id` is the row it reports
    progress into.

    UUIDs are passed as strings because the payload is serialised: keeping the
    wire format to JSON-compatible primitives means the queue stays readable
    with `redis-cli`, and stays decodable by a future worker that no longer
    imports our Python types.

    Failure is deliberately *not* caught here. If Redis is down the caller needs
    to know — see `DocumentService.upload` for what it does about that.
    """
    await queue.enqueue_job(
        INGEST_DOCUMENT,
        document_id=str(document_id),
        organization_id=str(organization_id),
        task_id=str(task_id),
        _job_id=ingestion_job_id(task_id),
    )
    logger.info(
        "queue.enqueued",
        kind=INGEST_DOCUMENT,
        document_id=str(document_id),
        task_id=str(task_id),
    )
