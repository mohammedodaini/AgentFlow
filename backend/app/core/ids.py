"""Identifier generation — UUIDv7 primary keys.

Layer: core (leaf — imports only the standard library).
Called by: app/models/base.py (UUIDPrimaryKeyMixin default).

Why v7 and not v4
-----------------
A v4 UUID is 122 random bits, so consecutive inserts land in random places in
the primary-key B-tree. Every insert dirties a different page, the index stops
fitting the write cache, and the table needs far more vacuuming than its row
count suggests. A v7 UUID puts a 48-bit millisecond timestamp in front, so new
rows append to the right edge of the index the way a bigserial would — while
staying globally unique and safe to expose in URLs.

The free consequence: `ORDER BY id` is `ORDER BY created_at`, and a paginated
cursor can be the id itself.

Why v4 is still not "wrong": v7 leaks creation time to anyone holding the id.
For AgentFlow's ids (organizations, documents, agent runs) that is not a
secret. A password-reset token would use `secrets.token_urlsafe` instead —
different job, different tool.

Why hand-rolled
---------------
`uuid.uuid7()` is standard-library, but only from Python 3.14; this project
runs 3.13. The alternatives were a dependency (`uuid-utils`, `uuid6`) or ten
lines of bit-packing owned here. RFC 9562 §5.7 is small and stable enough that
carrying a package for it costs more than it saves. Delete this module and
switch to `uuid.uuid7` when the project moves to 3.14.
"""

from __future__ import annotations

import secrets
import time
import uuid

_VERSION_7 = 0x70
"""Version nibble, occupying the high 4 bits of byte 6."""

_VARIANT_RFC = 0x80
"""RFC 9562 variant `0b10`, occupying the high 2 bits of byte 8."""


def uuid7() -> uuid.UUID:
    """Return a time-ordered UUIDv7.

    Layout (RFC 9562 §5.7), 16 bytes big-endian::

        bytes 0-5   unix timestamp in milliseconds (48 bits)
        byte  6     version 0x7 (4 bits) | random (4 bits)
        byte  7     random (8 bits)
        byte  8     variant 0b10 (2 bits) | random (6 bits)
        bytes 9-15  random (56 bits)

    Ordering is at millisecond resolution. Two ids minted inside the same
    millisecond sort randomly relative to each other — the RFC's optional
    monotonic counter is deliberately not implemented, because nothing here
    needs a total order finer than "which request came first".
    """
    timestamp_ms = time.time_ns() // 1_000_000
    timestamp = timestamp_ms.to_bytes(6, "big")

    # 10 random bytes cover positions 6..15; two of them get bits overwritten.
    random_bytes = bytearray(secrets.token_bytes(10))
    random_bytes[0] = (random_bytes[0] & 0x0F) | _VERSION_7
    random_bytes[2] = (random_bytes[2] & 0x3F) | _VARIANT_RFC

    return uuid.UUID(bytes=timestamp + bytes(random_bytes))
