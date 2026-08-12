"""Object storage — where document bytes live.

Layer: storage (infrastructure). The public surface of the package; nothing
outside it should import `app.storage.local` directly.

`create_storage` is a *builder*, in the same shape as `create_engine` and
`create_redis_client`: it takes settings and returns an object, so the caller
owns the lifecycle. There are two callers, and that is the whole reason this
function exists rather than a module-level singleton — the API process builds
one in `lifespan()`, and the arq worker builds its own in `on_startup`. They
are separate processes; neither can borrow the other's.

See ADR-0007 for why the boundary exists at all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request

from app.core.config import Settings
from app.storage.base import (
    ObjectNotFoundError,
    ObjectStorage,
    StorageError,
    build_document_key,
    sanitize_filename,
)
from app.storage.local import LocalObjectStorage

__all__ = [
    "LocalObjectStorage",
    "ObjectNotFoundError",
    "ObjectStorage",
    "StorageError",
    "build_document_key",
    "create_storage",
    "get_storage",
    "sanitize_filename",
]


def create_storage(settings: Settings) -> ObjectStorage:
    """Build the backend named by configuration.

    Returns the `ObjectStorage` protocol rather than `LocalObjectStorage`, so
    the type checker enforces that callers only use the three interface methods.
    Annotating the concrete class would let a call site quietly reach for
    `.root`, and that call site would then break the day this returns a GCS
    backend — exactly the failure the interface exists to prevent.

    `Settings` validates `storage_backend` as a `Literal`, so an unknown value
    fails at process startup rather than here. There is deliberately no `else`
    raising "unknown backend": it would be unreachable, and unreachable
    defensive code is code nobody can test and everybody trusts.
    """
    return LocalObjectStorage(Path(settings.storage_local_path))


def get_storage(request: Request) -> ObjectStorage:
    """Read the backend that `lifespan()` put on the application.

    The same narrowing job `get_session_factory` and `get_redis` do: `app.state`
    is an untyped namespace, and this is the one place that gives it a type back.
    """
    storage: ObjectStorage = request.app.state.storage
    return storage
