"""Filesystem-backed object storage — the development and test backend.

Layer: storage. Implements `ObjectStorage` from `base.py`.

This is not a toy: it is what `make dev` and the whole test suite run against,
so its failure modes have to be the same ones a real object store has. Three
things it does that a naive `open(path, "wb").write(data)` does not.

**It refuses to escape its root.** Every key is resolved and checked against
the configured directory, so a key that climbs out with `../` fails loudly
rather than writing to `/etc`. `sanitize_filename` already removes separators
from the client-supplied part; this is the second line of defence, because keys
are also built by our own code and our own code can have bugs.

**It writes atomically.** Bytes go to a temporary name and are then renamed
into place — `rename(2)` is atomic within a filesystem. Without it the
ingestion worker can open a file the API is still writing and parse half a PDF,
which is a bug that appears only under load and only sometimes.

**It does its I/O off the event loop.** Filesystem calls block, and a blocked
event loop stops serving *every* request, not just this one.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from pathlib import Path

import structlog

from app.storage.base import ObjectNotFoundError, StorageError

logger = structlog.get_logger(__name__)


class LocalObjectStorage:
    """Stores objects as files under a single root directory.

    Satisfies `ObjectStorage` structurally — it deliberately does not inherit
    from it, which is the point of using a `Protocol`.
    """

    def __init__(self, root: Path) -> None:
        """`root` is created if missing, and everything is confined beneath it.

        Resolved once, here, so the containment check in `_path_for` compares
        two absolute paths. Comparing a relative path against a resolved one is
        the classic way this check silently passes everything.
        """
        self._root = root.expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Where objects live. Read by tests and by operators."""
        return self._root

    def _path_for(self, key: str) -> Path:
        """Map a storage key to a path, refusing anything outside the root."""
        candidate = (self._root / key).resolve()

        if not candidate.is_relative_to(self._root):
            # Deliberately does not echo the key back. Someone probing for path
            # traversal learns nothing from the response, and the full key is in
            # the structured log where an operator can see it.
            logger.warning("storage.key_escapes_root", key=key)
            message = "Invalid storage key"
            raise StorageError(message)

        return candidate

    async def put(self, key: str, data: bytes, *, content_type: str) -> str:
        """Write `data` at `key` and return the key as the stored URI.

        `content_type` is accepted and ignored. A filesystem has nowhere to put
        it, and the value is already on `documents.mime_type` — but S3 and GCS
        both need it at write time, and adding a parameter to an interface after
        it has callers is far more disruptive than carrying an unused one now.
        """
        path = self._path_for(key)
        await asyncio.to_thread(self._write_atomically, path, data)
        logger.debug("storage.put", key=key, bytes=len(data), content_type=content_type)
        return key

    @staticmethod
    def _write_atomically(path: Path, data: bytes) -> None:
        """Write to a sibling temp file, then rename over the target.

        The rename is what makes a reader see either the old bytes or the new
        ones and never a half-written file. Sibling rather than `/tmp` because
        `rename(2)` is only atomic within one filesystem, and `/tmp` is very
        often a different one.
        """
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")

        try:
            # Inside the `try`, not before it. `mkdir` fails for entirely
            # ordinary reasons — a permission problem, a full disk, or a parent
            # that is already a file — and leaving it outside meant a raw
            # `OSError` escaping into a route, where the central error handler
            # cannot recognise it and the client gets a traceback instead of
            # the house error body. A test caught it.
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError as error:
            # `missing_ok=True` covers a temp file that was never created; it
            # does not cover a parent directory that cannot be traversed, which
            # is precisely the case that got us here. A cleanup that can itself
            # raise would replace the real error with a confusing one — the
            # same rule `DocumentService._delete_quietly` follows.
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

            message = f"Could not store object: {error.strerror or error}"
            raise StorageError(message) from error

    async def get(self, uri: str) -> bytes:
        """Read an object whole."""
        path = self._path_for(uri)

        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as error:
            message = f"No stored object at {uri!r}"
            raise ObjectNotFoundError(message) from error
        except OSError as error:
            message = f"Could not read object: {error.strerror or error}"
            raise StorageError(message) from error

    async def delete(self, uri: str) -> None:
        """Remove an object, and the now-empty directory that held it.

        Idempotent, as the protocol requires: `missing_ok=True` means deleting
        something already gone is a success, which is what a cleanup path needs.

        The directory prune matters more than it looks. Every document gets its
        own directory under the key layout, so without it a workspace that has
        uploaded and deleted ten thousand files leaves ten thousand empty
        directories behind, and `ls` on the parent stops being usable.
        """
        path = self._path_for(uri)
        await asyncio.to_thread(self._unlink_and_prune, path)
        logger.debug("storage.delete", key=uri)

    @staticmethod
    def _unlink_and_prune(path: Path) -> None:
        path.unlink(missing_ok=True)

        try:
            path.parent.rmdir()
        except OSError:
            # Not empty, already gone, or the root itself. All three are fine —
            # pruning is tidiness, and tidiness must never fail a delete.
            return
