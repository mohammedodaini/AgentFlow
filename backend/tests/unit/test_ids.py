"""UUIDv7 generation (M2).

Primary keys are the one value that ends up in every table, every log line and
every URL. The bit layout is a wire format defined by RFC 9562 — if it drifts,
sortability silently breaks and nothing else in the app notices. Hence tests
that assert the layout, not just "it returns a UUID".
"""

from __future__ import annotations

import time
import uuid

from app.core.ids import uuid7


def test_is_a_uuid_of_version_7() -> None:
    """RFC 9562 §5.7: the version nibble must be 7."""
    value = uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7


def test_uses_the_rfc_variant() -> None:
    """The two high bits of byte 8 must be 0b10, or parsers reject it."""
    assert uuid7().variant == uuid.RFC_4122


def test_values_are_unique() -> None:
    """62 bits of randomness — collisions inside one millisecond stay absurd."""
    values = {uuid7() for _ in range(10_000)}

    assert len(values) == 10_000


def test_ids_sort_by_creation_time() -> None:
    """The whole point of v7 over v4: index locality and free ordering.

    A random v4 primary key scatters inserts across the whole B-tree; a v7 key
    appends to the right edge. That is the difference between a healthy index
    and one that needs constant vacuuming.
    """
    first = uuid7()
    time.sleep(0.002)  # cross a millisecond boundary — v7 has ms resolution
    second = uuid7()

    assert first < second
    assert str(first) < str(second)  # lexicographic order matches too


def test_timestamp_prefix_reflects_now() -> None:
    """The leading 48 bits are unix time in milliseconds, big-endian."""
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    embedded_ms = int.from_bytes(value.bytes[:6], "big")

    assert before <= embedded_ms <= after
