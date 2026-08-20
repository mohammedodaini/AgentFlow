"""POST /auth/* — register, login, refresh, logout.

Thin: parse schema → call app/auth/service.py → return schema.
No password or JWT logic here, ever.

Every function below is a few lines for a reason. A route's job is to be the
translation layer between HTTP and the domain: pick apart the request, hand it
to a service, shape the answer. The moment a route contains an `if`, that
condition is a business rule stranded somewhere no worker and no agent tool
will ever execute it.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthService
from app.auth.tokens import TokenPair, TokenService
from app.db.deps import get_db
from app.db.redis import get_redis
from app.middleware.rate_limit import client_ip
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenPairResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> AuthService:
    """Assemble the service from its dependencies.

    Written as a dependency rather than constructed inside each route so a test
    can swap the whole service through `app.dependency_overrides` — and so the
    wiring lives in exactly one place when a third dependency appears. It just
    did: M16 needs the caller's address for the audit trail.

    The address is resolved *here* rather than in the service, because a service
    has no request — and keeping it that way is what lets `AuthService` be called
    from a worker or a test without inventing one.
    """
    return AuthService(session, TokenService(redis), ip_address=client_ip(request))


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _as_response(pair: TokenPair) -> TokenPairResponse:
    return TokenPairResponse(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/register", status_code=HTTPStatus.CREATED, summary="Create an account")
async def register(request: RegisterRequest, service: AuthServiceDep) -> TokenPairResponse:
    """Create a user, their personal organization, and a token pair.

    201 rather than 200 because a resource was created. Tokens come back
    immediately so the client is not forced to turn around and call `/login`
    with credentials it just sent — one round trip saved, and no reason to keep
    the password in memory a moment longer.
    """
    _user, pair = await service.register(request)
    return _as_response(pair)


@router.post("/login", summary="Exchange credentials for tokens")
async def login(request: LoginRequest, service: AuthServiceDep) -> TokenPairResponse:
    """Verify a password and issue tokens.

    200, not 201: no resource is created. A session here is a signed claim, not
    a row.
    """
    _user, pair = await service.login(request)
    return _as_response(pair)


@router.post("/refresh", summary="Rotate a refresh token")
async def refresh(request: RefreshRequest, service: AuthServiceDep) -> TokenPairResponse:
    """Exchange a refresh token for a new pair; the old one stops working.

    The client must replace *both* stored tokens with what comes back. Keeping
    the old refresh token and presenting it again is indistinguishable from
    theft, and is treated as such — see `TokenService.rotate`.
    """
    return _as_response(await service.refresh(request.refresh_token))


@router.post("/logout", status_code=HTTPStatus.NO_CONTENT, summary="Revoke a refresh token")
async def logout(request: RefreshRequest, service: AuthServiceDep) -> Response:
    """Revoke a refresh token. Idempotent, and quiet about what it found.

    204 with no body: there is nothing to say, and saying whether the token was
    valid would tell an attacker which of their guesses are real tokens.

    Note the honest limit — the caller's *access* token keeps working until it
    expires. See `AuthService.logout`.
    """
    await service.logout(request.refresh_token)
    return Response(status_code=HTTPStatus.NO_CONTENT)
