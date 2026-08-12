"""Shared schema building blocks — pagination, IDs, timestamps.

Layer: schemas (API boundary only — services return domain data, routes
serialize through these; ORM objects NEVER cross the API boundary).

Why that rule is absolute
-------------------------
Return an ORM object from a route and three things happen. First, every column
is serialised, including the ones nobody meant to publish — `password_hash` is
one attribute access away from a JSON response. Second, the response shape
becomes whatever the table happens to look like, so a migration silently
changes the public API. Third, FastAPI touching a lazy relationship *after* the
session has closed raises deep inside serialisation, which under asyncio
surfaces as `MissingGreenlet` from a stack trace mentioning no code of yours.

An explicit schema per response fixes all three: it is a whitelist, it is
versioned independently of the table, and it forces the data to be loaded while
the session is still open.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field


def _normalize_email(value: str) -> str:
    """Lowercase and trim, so one human means one row.

    `EmailStr` lowercases the *domain* but preserves the local part, because
    RFC 5321 says the local part is technically case-sensitive. In practice no
    mail provider treats it that way, and `users.email` carries a UNIQUE
    constraint that Postgres evaluates case-sensitively — so without this,
    `Ada@example.com` and `ada@example.com` are two accounts that both satisfy
    the constraint, and the second registration succeeds instead of being
    rejected.

    Normalising here rather than in each service means every entry point
    (register, login, invite) gets the same rule for free, and none of them can
    forget it.
    """
    return value.strip().lower()


NormalizedEmail = Annotated[EmailStr, AfterValidator(_normalize_email)]
"""A validated, lowercased email address. Use this everywhere, never bare `str`."""


class APIModel(BaseModel):
    """Base for every response schema.

    `from_attributes=True` lets `UserRead.model_validate(user_orm_object)` read
    attributes rather than dict keys — the one line that makes converting a
    model into a schema a single call instead of a field-by-field copy.
    """

    model_config = ConfigDict(from_attributes=True)


class Page[T](APIModel):
    """A pagination envelope, so every list endpoint answers the same shape.

    Returning a bare JSON array is the tempting alternative and it hurts twice:
    the client cannot tell "20 results" from "20 of 4,000", and there is
    nowhere to add a total later without breaking every consumer.

    Offset-based, which is correct while result sets are small. Deep offsets
    make Postgres count and discard every skipped row, so the ordered-list
    endpoints in later milestones will move to a keyset cursor — cheap here,
    because UUIDv7 primary keys already sort chronologically (ADR-0003).
    """

    items: list[T]
    total: int = Field(description="Total matching rows, ignoring limit/offset")
    limit: int
    offset: int
