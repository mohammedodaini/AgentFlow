"""Domain errors -> HTTP responses. The one place that knows both vocabularies.

Layer: api. This module exists so that no service ever has to.

`app/core/exceptions.py` says services must never import `HTTPException`,
because services have three callers — HTTP routes, arq workers, and agent
tools — and an HTTPException means nothing to the latter two. But something has
to make the translation, and doing it with a `try/except` in every route is how
half the routes end up returning a different error shape from the other half.

Registering handlers centrally means a service raises `NotFoundError` and gets
a 404 with the house error body, everywhere, including from routes nobody has
written yet.
"""

from __future__ import annotations

from http import HTTPStatus

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.exceptions import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)

logger = structlog.get_logger(__name__)

_STATUS_BY_ERROR: list[tuple[type[AppError], HTTPStatus]] = [
    # Ordered most specific first: DuplicateEmailError is a ConflictError, so a
    # dict keyed by exact type would miss it while this walk finds it.
    (AuthenticationError, HTTPStatus.UNAUTHORIZED),
    (AuthorizationError, HTTPStatus.FORBIDDEN),
    (NotFoundError, HTTPStatus.NOT_FOUND),
    (ConflictError, HTTPStatus.CONFLICT),
    # M5. Both are upload-specific and both are the client's to fix, which is
    # why neither is folded into a generic 400: 413 tells a client to send
    # less, 415 tells it to send something else. A single "bad request" would
    # make those two indistinguishable to code, and the difference is the only
    # actionable part of the answer.
    (PayloadTooLargeError, HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    (UnsupportedMediaTypeError, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
    # Deliberately absent: StorageError (app/storage/base.py). It falls through
    # to 500, which is the honest answer — the client did nothing wrong and
    # retrying the same request will not help.
]


class ErrorDetail(BaseModel):
    """The machine-readable half of an error."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """Every error this API returns, in one shape.

    Nested under `error` rather than flat, so a client can tell an error apart
    from a successful body by structure alone without consulting the status
    code — which matters more than it sounds once responses are logged, queued,
    or replayed away from their HTTP context.
    """

    error: ErrorDetail


def _status_for(exc: AppError) -> HTTPStatus:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status

    # A bare AppError means a domain error nobody has mapped yet. 500 is the
    # honest answer: the client did nothing wrong, and we do not know enough to
    # claim otherwise.
    return HTTPStatus.INTERNAL_SERVER_ERROR


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the domain exception hierarchy into the application."""

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: Exception) -> JSONResponse:
        # The signature says `Exception` because that is Starlette's handler
        # protocol; the registration above guarantees the real type.
        if not isinstance(exc, AppError):  # pragma: no cover — defensive
            raise exc

        status = _status_for(exc)

        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            logger.error("error.unmapped", code=exc.code, message=exc.message)

        headers = {}
        if status == HTTPStatus.UNAUTHORIZED:
            # RFC 9110 requires WWW-Authenticate on a 401. Clients read it to
            # decide *how* to authenticate rather than guessing.
            headers["WWW-Authenticate"] = "Bearer"

        body = ErrorResponse(error=ErrorDetail(code=exc.code, message=exc.message))

        return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)
