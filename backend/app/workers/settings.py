# ruff: noqa: F401  — remove once this module is implemented (M5)
"""arq WorkerSettings — the worker process entrypoint (`arq app.workers.settings.WorkerSettings`).

Runs in a SEPARATE process from the API: it needs its own DB session factory
and logging setup (on_startup), and registers every task function.
"""

from __future__ import annotations

from arq.connections import RedisSettings

from app.core.config import get_settings

# TODO(M5): class WorkerSettings — functions=[ingest_document, ...],
#           redis_settings from config, on_startup/on_shutdown, retry policy
