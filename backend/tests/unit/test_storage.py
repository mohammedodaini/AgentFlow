"""Object storage: containment, atomicity, idempotence (M5).

Unit tests, but they touch a real filesystem under `tmp_path`. That is not a
contradiction of the marker — the point of the marker is "fast and isolated",
and these are both — it is a deliberate refusal to mock the thing under test.
Every interesting behaviour here *is* a filesystem behaviour: a key that
escapes its root, a rename that has to be atomic, a delete that has to succeed
when there is nothing to delete. A mock would confirm all three and prove none.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from app.storage import ObjectStorage, build_document_key, sanitize_filename
from app.storage.base import MAX_FILENAME_LENGTH, ObjectNotFoundError, StorageError
from app.storage.local import LocalObjectStorage


@pytest.fixture
def local_storage(tmp_path: pathlib.Path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path / "objects")


def test_the_local_backend_satisfies_the_protocol() -> None:
    """Structural conformance, checked rather than assumed.

    `LocalObjectStorage` deliberately does not inherit from `ObjectStorage`, so
    nothing but this assertion notices if a method is renamed — and the failure
    without it would surface as an `AttributeError` inside a worker, hours
    after the change.
    """
    assert issubclass(LocalObjectStorage, ObjectStorage)


async def test_put_then_get_returns_the_same_bytes(local_storage: LocalObjectStorage) -> None:
    uri = await local_storage.put("a/b/report.pdf", b"payload", content_type="application/pdf")

    assert uri == "a/b/report.pdf", "the local backend uses the key as the URI"
    assert await local_storage.get(uri) == b"payload"


async def test_put_creates_missing_parent_directories(local_storage: LocalObjectStorage) -> None:
    """The key layout nests four levels deep, and none of them exist up front."""
    await local_storage.put("organizations/o/documents/d/f.txt", b"x", content_type="text/plain")

    assert (local_storage.root / "organizations/o/documents/d/f.txt").is_file()


async def test_put_overwrites_an_existing_object(local_storage: LocalObjectStorage) -> None:
    """Required by the protocol: a retried upload must not need a delete first."""
    await local_storage.put("k", b"first", content_type="text/plain")
    await local_storage.put("k", b"second", content_type="text/plain")

    assert await local_storage.get("k") == b"second"


async def test_put_leaves_no_temporary_files_behind(local_storage: LocalObjectStorage) -> None:
    """The atomic write uses a `.tmp` sibling; it must be renamed, not copied.

    A leftover temp file is not cosmetic. The retrieval layer will one day list
    a prefix, and a directory holding both `report.pdf` and
    `.report.pdf.a1b2.tmp` is a directory with two documents in it.
    """
    await local_storage.put("d/report.pdf", b"payload", content_type="application/pdf")

    names = sorted(path.name for path in (local_storage.root / "d").iterdir())
    assert names == ["report.pdf"]


async def test_get_raises_for_a_missing_object(local_storage: LocalObjectStorage) -> None:
    """A row pointing at bytes that are gone is a broken invariant, so this is a
    distinct error type rather than a `None` a caller might ignore."""
    with pytest.raises(ObjectNotFoundError):
        await local_storage.get("never/written")


async def test_delete_is_idempotent(local_storage: LocalObjectStorage) -> None:
    """Deleting nothing succeeds — required, because `delete` runs on cleanup
    paths where the object may or may not have been written yet."""
    await local_storage.delete("never/written")


async def test_delete_removes_the_object_and_prunes_its_directory(
    local_storage: LocalObjectStorage,
) -> None:
    await local_storage.put("docs/one/report.pdf", b"x", content_type="application/pdf")

    await local_storage.delete("docs/one/report.pdf")

    assert not (local_storage.root / "docs/one/report.pdf").exists()
    assert not (local_storage.root / "docs/one").exists(), "empty directory should be pruned"


async def test_delete_keeps_a_directory_that_still_has_objects(
    local_storage: LocalObjectStorage,
) -> None:
    """The prune must never take a sibling with it."""
    await local_storage.put("docs/a.txt", b"x", content_type="text/plain")
    await local_storage.put("docs/b.txt", b"y", content_type="text/plain")

    await local_storage.delete("docs/a.txt")

    assert await local_storage.get("docs/b.txt") == b"y"


async def test_a_filesystem_failure_on_write_becomes_a_storage_error(
    local_storage: LocalObjectStorage,
) -> None:
    """Not a bare `OSError` escaping into a route.

    Here the parent of the target is already a *file*, so `mkdir` fails. The
    point is the translation: every error leaving this module must be a
    `StorageError`, or the API's central handler cannot produce the house error
    body and the client gets a raw traceback instead.
    """
    await local_storage.put("collision", b"x", content_type="text/plain")

    with pytest.raises(StorageError):
        await local_storage.put("collision/child.txt", b"y", content_type="text/plain")


async def test_a_filesystem_failure_on_read_becomes_a_storage_error(
    local_storage: LocalObjectStorage,
) -> None:
    """Reading a directory as if it were an object.

    Distinct from `ObjectNotFoundError`: the key exists, it just is not a file.
    Collapsing the two would tell an operator "the object is missing" when the
    real answer is "the key layout is wrong".
    """
    await local_storage.put("folder/file.txt", b"x", content_type="text/plain")

    with pytest.raises(StorageError):
        await local_storage.get("folder")


@pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "/etc/passwd"])
async def test_a_key_that_escapes_the_root_is_refused(
    local_storage: LocalObjectStorage, key: str
) -> None:
    """The second line of defence.

    `sanitize_filename` already strips separators from the client-supplied
    part, so reaching this check means our own key-building code has a bug —
    which is exactly when a containment check earns its place. `/etc/passwd` is
    included because an absolute path silently *discards* the root in
    `Path.__truediv__`, which is the least obvious of the three.
    """
    with pytest.raises(StorageError):
        await local_storage.put(key, b"pwned", content_type="text/plain")

    assert list(local_storage.root.rglob("*")) == [], "nothing may be written on this path"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "etc_passwd"),
        ("Q3 report (final).pdf", "Q3_report_final_.pdf"),
        ("..", "file"),
        (".hidden", "hidden"),
        ("", "file"),
        (None, "file"),
        ("日本語", "file"),
        ("with\x00null.txt", "with_null.txt"),
        ("a/b/c.txt", "a_b_c.txt"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    """The allowlist, case by case.

    `日本語` collapsing to `file` is a real limitation, written down here rather
    than discovered later: the display name survives on `documents.title`, and
    only the storage key is anglicised.
    """
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_truncates_from_the_front() -> None:
    """Long names are cut so the extension survives — it is the part a human
    uses to recognise a file in a storage browser."""
    result = sanitize_filename("a" * 500 + ".pdf")

    assert len(result) == MAX_FILENAME_LENGTH
    assert result.endswith(".pdf")


def test_the_document_key_puts_the_tenant_first() -> None:
    """Tenant-first is what makes "erase this customer" a prefix operation."""
    organization_id = uuid.UUID("00000000-0000-7000-8000-000000000001")
    document_id = uuid.UUID("00000000-0000-7000-8000-000000000002")

    key = build_document_key(organization_id, document_id, "../../etc/passwd")

    assert key == f"organizations/{organization_id}/documents/{document_id}/etc_passwd"
