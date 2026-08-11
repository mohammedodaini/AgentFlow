# ruff: noqa: F401  — remove once this module is implemented (M2)
"""Shared schema building blocks — pagination, IDs, timestamps.

Layer: schemas (API boundary only — services return domain data, routes
serialize through these; ORM objects NEVER cross the API boundary).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

# TODO(M2): class APIModel(BaseModel) — model_config = ConfigDict(from_attributes=True)
# TODO(M2): class Page[T] — items, total, limit, offset (generic pagination envelope)
