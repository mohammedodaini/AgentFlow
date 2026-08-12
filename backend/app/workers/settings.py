"""arq `WorkerSettings` — the worker process entrypoint.

Run with `arq app.workers.settings.WorkerSettings` (see `make worker`).

This is a second composition root. `app/main.py` is the first, and everything
true of it is true here: the worker is a *separate process*, so it owns its own
engine, its own connection pool, its own storage client and its own logging
configuration. It cannot reach `app.state`, and it must not try — two processes
sharing one pool is a bug that only appears under load, in production, as
connections neither process believes it opened.

Settings are read at import time, in the module body. That would be wrong in a
library and is right here: this module exists only to be the argument to the
`arq` command, so importing it *is* starting the worker, and a configuration
error should stop the process rather than surface on the first job.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.logging.config import configure_logging
from app.storage import create_storage
from app.workers.queue import build_redis_settings
from app.workers.tasks.ingestion import ingest_document

logger = structlog.get_logger(__name__)

_settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    """Build everything a job needs, once per worker process.

    `ctx` is arq's answer to dependency injection: a dict handed to every task.
    Building the engine here rather than inside `ingest_document` is the whole
    difference between one pool per process and one pool per job — the latter
    opens and tears down connections thousands of times an hour, and Postgres
    charges real latency for each one.
    """
    configure_logging()

    engine = create_engine(_settings)
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["storage"] = create_storage(_settings)

    logger.info("worker.startup", env=_settings.env, storage=_settings.storage_backend)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Close the pool. Skipping this leaks server-side sessions on every
    restart, and a worker restarts far more often than an API process."""
    await ctx["engine"].dispose()
    logger.info("worker.shutdown")


class WorkerSettings:
    """What arq reads to configure the worker.

    A plain class with class attributes, which is arq's own convention — it is
    never instantiated, so this is a namespace rather than an object.
    """

    functions = [ingest_document]
    """The registry. arq names a job by the function's `__qualname__`, so the
    string `INGEST_DOCUMENT` in `app/models/task.py` and this function must
    agree — a mismatch means jobs are enqueued and silently never run.
    `tests/unit/test_worker_settings.py` asserts they do."""

    redis_settings: RedisSettings = build_redis_settings(_settings)
    """Derived from the same `REDIS_URL` the API enqueues to. Deriving both from
    one setting is what stops producer and consumer pointing at different Redis
    databases — a failure whose only symptom is a queue that never drains."""

    on_startup = startup
    on_shutdown = shutdown

    max_tries = _settings.arq_max_tries
    """Retries help the transient failures and not the permanent ones, which is
    why `ingest_document` decides for itself which kind it just hit rather than
    letting every failure consume all three attempts."""

    job_timeout = _settings.arq_job_timeout_seconds
    """A job with no timeout does not fail — it hangs, holding a worker slot
    forever, and the queue behind it stops moving."""

    keep_result = 3600
    """How long arq keeps a job's result in Redis. One hour is plenty, because
    the durable copy is the `tasks` row in Postgres — which is the entire
    reason that table exists."""
