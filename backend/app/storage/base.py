"""The object-storage contract: what every backend must do, and nothing more.

Layer: storage (infrastructure). Depends only on `app.core.exceptions`.

Why an interface for something with one implementation
------------------------------------------------------
Normally that is over-engineering, and normally it would be here too. The
justification is specific: this is the one seam where local development and
production are *guaranteed* to differ. Nothing runs MinIO or a GCS emulator on
a laptop for a tutorial project, and nothing serves production traffic off a
container's filesystem. So the second implementation is not hypothetical — it
is scheduled (M16), and writing the boundary now costs three methods.

The interface is deliberately tiny: put, get, delete, and a key builder. No
listing, no signed URLs, no copy, no multipart. Every one of those is easy to
add against a real requirement and impossible to remove once a caller depends
on it — and the ones a filesystem cannot emulate honestly (signed URLs) would
make the local backend a lie.

See ADR-0007.
"""

from __future__ import annotations

import re
import uuid
from typing import ClassVar, Protocol, runtime_checkable

from app.core.exceptions import AppError

MAX_FILENAME_LENGTH = 200
"""Leaves room inside the 1024-char `documents.storage_uri` column for the
organization and document UUIDs, and stays under the 255-byte per-component
limit every common filesystem imposes."""

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
r"""An allowlist, inverted. A denylist of dangerous characters is the wrong
shape for this problem: you have to think of every one, and `..`, NUL, `/`,
`\`, newline, and unicode look-alikes are only the ones people remember."""


class StorageError(AppError):
    """Object storage could not do what it was asked.

    An `AppError` subclass, so the API's central handler produces the house
    error body instead of a bare traceback. Deliberately left *unmapped* in
    `app/api/errors.py`, which means it surfaces as 500 — the honest status,
    because a storage failure is our problem and there is nothing the client
    can change about the request to fix it.
    """

    default_code: ClassVar[str] = "storage_error"


class ObjectNotFoundError(StorageError):
    """The key is not there.

    Still a 500 rather than a 404: reaching this means a `documents` row points
    at bytes that have gone missing, which is a broken invariant, not a client
    mistake. It must be loud.
    """

    default_code: ClassVar[str] = "storage_object_not_found"


@runtime_checkable
class ObjectStorage(Protocol):
    """What a storage backend must provide.

    A `Protocol`, not an abstract base class, so a backend does not have to
    import from here to satisfy it — and, more usefully, so a test double is a
    plain class with three methods rather than a subclass carrying inherited
    machinery. `runtime_checkable` lets a test assert conformance directly.

    All three methods are `async`. The local backend does synchronous file I/O
    and could have been sync, but then swapping in a network-backed
    implementation would change every call site from `x` to `await x` — the
    exact leak the interface exists to prevent.
    """

    async def put(self, key: str, data: bytes, *, content_type: str) -> str:
        """Store `data` at `key`; return the URI to save on the document row.

        Returns a URI rather than accepting one because only the backend knows
        how to name its own objects. Overwrites: a repeated `put` of the same
        key must succeed, so a retried upload does not need a delete first.
        """
        ...

    async def get(self, uri: str) -> bytes:
        """Read an object whole. Raises `ObjectNotFoundError` if absent.

        Whole, not streamed, because the caller — the ingestion worker — has to
        hold the full document in memory to parse it anyway. A streaming read
        belongs here the day something can actually consume a stream.
        """
        ...

    async def delete(self, uri: str) -> None:
        """Remove an object. Idempotent: deleting what is not there succeeds.

        Idempotency is not a nicety. Delete is called during cleanup after a
        failed upload, where the object may or may not have been written, and a
        cleanup path that can itself raise is a cleanup path that masks the
        original error.
        """
        ...


def sanitize_filename(filename: str | None) -> str:
    """Reduce a client-supplied filename to something safe to use as a key.

    The threat is path traversal: `../../../../etc/passwd`, or on the local
    backend simply `../` climbing out of the configured root. Everything
    outside `[A-Za-z0-9._-]` collapses to `_`, which removes separators, NULs
    and newlines in one rule rather than several.

    Leading and trailing dots are stripped as well, so `..` cannot survive as a
    name and a file cannot be made hidden. Anything that reduces to nothing
    becomes `"file"` — refusing an upload because someone named it `"日本語"`
    would be absurd, and the display name is kept on `documents.title`
    regardless.

        >>> sanitize_filename("../../etc/passwd")
        'etc_passwd'
        >>> sanitize_filename("Q3 report (final).pdf")
        'Q3_report_final_.pdf'
    """
    if not filename:
        return "file"

    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", filename).strip("._")

    if not cleaned:
        return "file"

    if len(cleaned) > MAX_FILENAME_LENGTH:
        # Truncate from the front so the extension survives — it is the part a
        # human uses to recognise the file in a storage browser.
        cleaned = cleaned[-MAX_FILENAME_LENGTH:]

    return cleaned


def build_document_key(
    organization_id: uuid.UUID, document_id: uuid.UUID, filename: str | None
) -> str:
    """The canonical key layout for an uploaded document.

        organizations/<org-uuid>/documents/<doc-uuid>/<safe-filename>

    Two properties are doing real work here.

    **The tenant is the first path component.** That makes "delete everything
    belonging to this customer" a prefix operation, which is what a GDPR
    erasure request actually asks for, and it makes a bucket policy or IAM
    condition expressible per tenant later. A flat `documents/<uuid>` layout
    can do neither.

    **The document UUID is in the path.** Two people uploading `report.pdf` on
    the same day must not collide, and a UUIDv7 sorts chronologically, so a
    listing of the prefix comes back in upload order for free (ADR-0003).

    The filename is kept only because a human staring at a storage browser at
    3am needs one recognisable thing; nothing reads it back.
    """
    safe_name = sanitize_filename(filename)
    return f"organizations/{organization_id}/documents/{document_id}/{safe_name}"
