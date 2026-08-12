"""`tasks` — the durable mirror of arq queue jobs.

Layer: models. Redis holds the queue; Postgres holds the truth.

Why mirror the queue at all
---------------------------
arq already tracks jobs in Redis, so this table looks redundant until you ask
three questions it cannot answer. *What happened to the upload I made on
Tuesday?* — arq discards job results after `keep_result` seconds. *Which
ingestions failed this week?* — Redis is not queryable that way. *Did we lose
work when Redis was flushed?* — a flush is invisible to anyone who only
consulted Redis.

The split is the same one `app/db/redis.py` states: Redis holds what may
disappear, Postgres holds what may not. The *job* may disappear; the *record
that we accepted the work* may not.

Written by workers (`app/workers/tasks/`), created by services at enqueue time,
read by anyone asking what the background did.
"""

from __future__ import annotations

import enum
import uuid
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

INGEST_DOCUMENT = "ingest_document"
"""The only `kind` at M5.

A plain string rather than an enum: `kind` names a *function*, and the set of
background functions changes with every milestone. A native Postgres enum would
demand an `ALTER TYPE` migration each time — precisely the cost M2 learned
about the hard way — while buying nothing, because the value is never rendered
to a user and never branched on outside the worker registry.
"""


class TaskStatus(enum.StrEnum):
    """Lifecycle of one queued job."""

    QUEUED = "queued"
    """Row committed. The job reaches Redis just after — see ADR-0008."""

    RUNNING = "running"
    """A worker claimed it. `attempts` says how many times."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One unit of background work, as Postgres remembers it."""

    __tablename__ = "tasks"

    __table_args__ = (
        # "What is still running / what failed, for this tenant" — the query an
        # operator actually types. The leading column also serves a bare org
        # filter, so no separate index on organization_id.
        Index("ix_tasks_organization_id_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    """Background work is tenant-scoped too. A task nobody can attribute to an
    organization is a task nobody can bill, rate-limit, or show in a UI."""

    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    """Which worker function this is. See `INGEST_DOCUMENT` above."""

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The job's arguments, e.g. `{"document_id": "..."}`.

    `JSONB` rather than `JSON`: it is stored parsed, so it can be indexed and
    queried (`payload->>'document_id'`) instead of only read back whole.

    Duplicating the arguments that were also handed to arq is deliberate. It is
    what makes a task re-runnable from the database alone after Redis has
    forgotten the job.
    """

    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            values_callable=lambda enum_class: [member.value for member in enum_class],
        ),
        nullable=False,
        default=TaskStatus.QUEUED,
    )

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Incremented by the worker each time it starts.

    `server_default` as well as `default` so a row inserted by a migration or by
    hand — neither of which runs the Python default — still gets 0 rather than
    NULL, and `attempts + 1` does not silently become NULL.
    """

    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """Whatever the job produced, or why it did not.

    On success at M5: `{"characters": 41233}`. On failure: `{"error": "..."}`.
    Failures deliberately do *not* get their own column — the user-facing copy
    of the message lives on `documents.error`, which is what the API returns.
    Keeping the operator's copy here avoids two columns that must agree.
    """
