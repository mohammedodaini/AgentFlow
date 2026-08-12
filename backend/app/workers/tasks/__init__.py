"""Background task functions, one module per kind of work.

Every function here runs in the worker process with an arq `ctx` as its first
argument, and is registered in `WorkerSettings.functions`. A task module that is
written but not registered is a task that gets enqueued and never runs, so the
registry in `app/workers/settings.py` is the first file to check when a job
seems to vanish.
"""

from __future__ import annotations

from app.workers.tasks.ingestion import ingest_document

# TODO(M9): agent_execution · TODO(M10): memory_extraction · TODO(M11): email_sync

__all__ = ["ingest_document"]
