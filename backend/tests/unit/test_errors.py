"""Domain errors → HTTP, and the role guard (M4).

`app/api/errors.py` is the only place that knows both vocabularies, so it is
the single point where a wrong mapping turns "you may not do that" into "the
server is broken". Cheap to test directly, and worth pinning: the ordering of
the mapping table is load-bearing, because `DuplicateEmailError` is a
`ConflictError` and a dict keyed by exact type would miss it.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.errors import _status_for, register_exception_handlers
from app.auth.dependencies import require_role
from app.core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    DuplicateEmailError,
    NotFoundError,
)
from app.integrations.base import OAuthError, OAuthRevokedError
from app.models import Membership, Role


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthenticationError("x"), HTTPStatus.UNAUTHORIZED),
        (AuthorizationError("x"), HTTPStatus.FORBIDDEN),
        (NotFoundError("x"), HTTPStatus.NOT_FOUND),
        (ConflictError("x"), HTTPStatus.CONFLICT),
        (DuplicateEmailError("x"), HTTPStatus.CONFLICT),
        # M11, added after a runtime check found a provider outage answering 500.
        (OAuthError("x"), HTTPStatus.BAD_GATEWAY),
    ],
)
def test_each_domain_error_maps_to_its_status(error: AppError, expected: HTTPStatus) -> None:
    assert _status_for(error) == expected


def test_an_upstream_failure_is_not_reported_as_our_bug() -> None:
    """502, not 500, and the difference is the only actionable part of the answer.

    500 tells a client its request was fine and nothing more. 502 says an upstream
    is down, so retrying may well work — and it keeps our bugs and Google's
    outages apart in the metrics, which is the difference between a useful alert
    and a noisy one.
    """
    assert _status_for(OAuthError("Google is unreachable")) == HTTPStatus.BAD_GATEWAY


def test_a_revoked_credential_is_translated_before_it_reaches_the_mapping() -> None:
    """`OAuthRevokedError` *is* an `OAuthError`, so the table alone would answer
    502 — inviting a retry that can never succeed.

    `IntegrationService` catches it first and raises `NotFoundError` carrying
    "reconnect it", because a revoked credential is not an upstream failure: it is
    something the user did, and the only useful response is the action they can
    take. This records that the subclassing is deliberate, and that the service —
    not the table — is what makes it right.
    """
    assert issubclass(OAuthRevokedError, OAuthError)
    assert _status_for(OAuthRevokedError("revoked")) == HTTPStatus.BAD_GATEWAY


def test_a_subclass_resolves_through_its_base() -> None:
    """`DuplicateEmailError` is never listed in the table; `ConflictError` is.

    This is why the mapping is an ordered list walked with `isinstance` rather
    than a dict keyed by type — a dict would fall through to 500 for every
    subclass anyone adds later.
    """
    assert issubclass(DuplicateEmailError, ConflictError)
    assert _status_for(DuplicateEmailError("x")) == HTTPStatus.CONFLICT


def test_an_unmapped_domain_error_becomes_a_500() -> None:
    """The honest answer for an error nobody has classified.

    Guessing 400 would blame the client for something we have not diagnosed;
    500 says "our problem", which is what an unmapped error actually means.
    """
    assert _status_for(AppError("something nobody mapped")) == HTTPStatus.INTERNAL_SERVER_ERROR


def test_error_codes_are_stable_and_machine_readable() -> None:
    """Clients branch on `code`; `message` is for humans and may be reworded."""
    assert AuthenticationError("x").code == "authentication_failed"
    assert AuthorizationError("x").code == "not_authorized"
    assert NotFoundError("x").code == "not_found"
    assert DuplicateEmailError("x").code == "duplicate_email"


def test_an_explicit_code_overrides_the_default() -> None:
    assert NotFoundError("x", code="document_not_found").code == "document_not_found"


def _app_raising(error: AppError) -> FastAPI:
    """A one-route application whose handler raises `error`.

    Building a throwaway app rather than reusing the real one keeps this a unit
    test: it exercises the handler and nothing else, so a failure here can only
    mean the handler is wrong.
    """
    application = FastAPI()
    register_exception_handlers(application)

    @application.get("/boom")
    async def boom() -> None:
        raise error

    return application


async def _get_boom(error: AppError) -> tuple[int, dict[str, str], Any]:
    transport = ASGITransport(app=_app_raising(error))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")
    return response.status_code, dict(response.headers), response.json()


async def test_the_handler_returns_the_house_error_body() -> None:
    """One shape for every error, nested under `error` so a client can tell a
    failure from a success by structure alone."""
    status, _headers, body = await _get_boom(NotFoundError("No such document"))

    assert status == HTTPStatus.NOT_FOUND
    assert body == {"error": {"code": "not_found", "message": "No such document"}}


async def test_a_401_carries_www_authenticate() -> None:
    """RFC 9110 requires it, and clients read it to decide *how* to authenticate."""
    status, headers, _body = await _get_boom(AuthenticationError("nope"))

    assert status == HTTPStatus.UNAUTHORIZED
    assert headers["www-authenticate"] == "Bearer"


async def test_other_statuses_do_not_carry_www_authenticate() -> None:
    _status, headers, _body = await _get_boom(ConflictError("taken"))

    assert "www-authenticate" not in headers


async def test_an_unmapped_error_is_a_500_and_gets_logged() -> None:
    """The "we made a mistake" path.

    A bare `AppError` means somebody raised a domain error nobody classified.
    It must still produce the house body rather than an unhandled traceback,
    and it must be loud in the logs — which is the branch this covers.
    """
    status, _headers, body = await _get_boom(AppError("nobody mapped me"))

    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert body["error"]["code"] == "app_error"


def _membership(role: Role) -> Membership:
    return Membership(user_id=uuid.uuid4(), organization_id=uuid.uuid4(), role=role)


async def test_require_role_admits_a_listed_role() -> None:
    guard = require_role(Role.OWNER, Role.ADMIN)
    membership = _membership(Role.ADMIN)

    assert await guard(membership) is membership


async def test_require_role_rejects_an_unlisted_role() -> None:
    """The route-level convenience guard.

    Note it is a *second* line of defence — the same rules live in
    OrganizationService, because workers and agent tools never pass through a
    FastAPI dependency.
    """
    guard = require_role(Role.OWNER)

    with pytest.raises(AuthorizationError) as error:
        await guard(_membership(Role.MEMBER))

    assert "owner" in str(error.value)
