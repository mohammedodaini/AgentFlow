# ruff: noqa: F401  — remove once this module is implemented (M2)
"""Declarative base + column conventions shared by every table.

Layer: models. Conventions (docs/database.md): UUIDv7 PKs, created_at /
updated_at everywhere, snake_case, plural table names.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# TODO(M2): class Base(DeclarativeBase) — naming_convention for constraints (Alembic autogenerate
#   sanity)
# TODO(M2): mixins: UUIDPrimaryKeyMixin (uuid7), TimestampMixin (created_at, updated_at server
#   defaults)
