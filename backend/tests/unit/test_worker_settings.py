"""The worker's registry has to agree with the producer (M5).

Small file, high value. The API enqueues a job by *name* — the string
`INGEST_DOCUMENT` — and arq resolves that name against
`WorkerSettings.functions` using each function's `__qualname__`. Nothing
connects those two facts at import time, so renaming the function is a change
that type-checks, lints, passes every other test, and silently stops every
upload from being processed. The only symptom is documents that stay `pending`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

from fastapi import Request

from app.core.config import get_settings
from app.models import INGEST_DOCUMENT
from app.workers.queue import build_redis_settings, get_queue, ingestion_job_id
from app.workers.settings import WorkerSettings, shutdown, startup
from app.workers.tasks.ingestion import ingest_document


def test_the_registered_job_name_matches_what_the_api_enqueues() -> None:
    """The one assertion this module exists for."""
    registered = {function.__qualname__ for function in WorkerSettings.functions}

    assert INGEST_DOCUMENT in registered, (
        f"the API enqueues {INGEST_DOCUMENT!r}, but the worker registers {sorted(registered)}"
    )


def test_the_ingestion_task_is_registered() -> None:
    assert ingest_document in WorkerSettings.functions


def test_retry_and_timeout_come_from_settings() -> None:
    """Not hardcoded, so a deployment can raise the timeout without a code
    change — and so the values appear in `.env.example` rather than being
    buried in a class body where nobody looks."""
    settings = get_settings()

    assert WorkerSettings.max_tries == settings.arq_max_tries
    assert WorkerSettings.job_timeout == settings.arq_job_timeout_seconds


def test_the_worker_and_the_producer_derive_redis_from_one_setting() -> None:
    """Both call `build_redis_settings`, so they cannot disagree.

    The assertion is against the function rather than against
    `WorkerSettings.redis_settings`, and the difference is worth knowing:
    `WorkerSettings` evaluates its class attributes at *import* time, so its
    frozen copy reflects whatever `REDIS_URL` was before this test suite
    repointed it at database 1.

    That is correct for the worker — the module exists only to be the argument
    to the `arq` command, so importing it is starting the process — and it does
    mean the class attribute is not the thing to assert on. The invariant that
    actually matters is that one setting feeds both sides.
    """
    settings = get_settings().model_copy(update={"redis_url": "redis://example:6380/4"})

    derived = build_redis_settings(settings)

    assert derived.host == "example"
    assert derived.port == 6380
    assert derived.database == 4


async def test_startup_builds_everything_a_job_needs_and_shutdown_releases_it() -> None:
    """`ctx` is arq's dependency injection, and `on_startup` is what fills it.

    A missing key here is not a type error and not a lint error — it is a
    `KeyError` inside the first job that runs, in production, after a deploy.
    Building the engine once per process rather than once per job is the whole
    reason this hook exists.

    Nothing connects: `create_engine` builds a pool lazily, so this stays a
    unit test.
    """
    ctx: dict[str, object] = {}

    await startup(ctx)

    try:
        assert ctx["session_factory"] is not None
        assert ctx["storage"] is not None
        assert ctx["engine"] is not None
    finally:
        # Skipping this leaks server-side sessions on every worker restart, and
        # a worker restarts far more often than an API process.
        await shutdown(ctx)


def test_get_queue_narrows_app_state_back_to_a_type() -> None:
    """`app.state` is an untyped namespace; this is the one place that gives it
    a type back — the same job `get_storage` and `get_redis` do."""
    sentinel = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(queue=sentinel)))

    assert get_queue(cast("Request", request)) is sentinel


def test_the_job_id_is_derived_from_the_task_row() -> None:
    """arq treats a job id as an idempotency key, so deriving it from the task
    row — rather than letting arq generate a random one — is what stops a
    retried request putting two copies of the same work in the queue."""
    task_id = uuid.UUID("00000000-0000-7000-8000-00000000000a")

    assert ingestion_job_id(task_id) == ingestion_job_id(task_id)
    assert ingestion_job_id(task_id) != ingestion_job_id(uuid.uuid4())
