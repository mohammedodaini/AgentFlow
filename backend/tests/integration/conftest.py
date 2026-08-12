"""Integration-layer fixtures.

These are the tests unit tests cannot replace. A `UNIQUE` constraint, an
`ON DELETE CASCADE` and a server-side `now()` default are all enforced by the
database, so the only way to know they work is to ask the database.

There is nothing left in this file. `db_session` moved to `tests/conftest.py`
in M4, once the end-to-end tests needed the same transactional isolation these
do — and two fixtures with the same name and different semantics is a bug
waiting to be written.

The file stays as documentation of where to add fixtures that only this layer
needs.
"""

from __future__ import annotations
