"""FastAPI auth dependencies — the gate every protected route declares.

Dependencies (not middleware) because they are per-route, testable, and
composable: get_current_user -> get_current_membership -> require_role.

Why not middleware
------------------
Middleware runs on every request and therefore has to carry a list of paths to
skip — which is a denylist, and a denylist of unprotected routes fails *open*:
forget to add a new route and it is public, silently, with nothing to notice.
A dependency fails closed. A route without `CurrentUser` in its signature is
visibly unprotected, right there in the function definition, and shows up as
unauthenticated in the OpenAPI schema.

Composability is the other half. `require_role(Role.OWNER)` builds on
`get_current_membership`, which builds on `get_current_user`; FastAPI resolves
the chain once per request and caches it, so declaring the strictest one you
need costs nothing extra.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.security import decode_token
from app.db.deps import get_db
from app.models.membership import Membership, Role
from app.models.user import User
from app.services.organization_service import OrganizationService

ORGANIZATION_HEADER = "X-Organization-Id"
"""Which organization this request acts within.

A header rather than a path prefix (`/orgs/{id}/documents/...`) because the
tenant is *context*, not a resource being addressed — the same reasoning that
puts a request id in a header. It also keeps every URL in the API stable when a
user switches workspaces in the UI.

The tradeoff, stated plainly: a header is easier to forget than a path segment.
So it is *required* — omitting it is a 422 from FastAPI's own validation, never
a silently-chosen default organization. A default would mean a mis-scoped
request quietly succeeds against the wrong tenant, which is the worst available
failure mode in a multi-tenant system.
"""

_bearer_scheme = HTTPBearer(auto_error=False)
"""`auto_error=False` so a missing header reaches our own code.

Left to itself, HTTPBearer raises its own HTTPException with its own body
shape, so unauthenticated responses would look different from every other error
this API returns. Handling it here keeps one error format.
"""


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the bearer token to a live user row.

    The database is consulted on every request rather than trusting the token's
    claims. That costs one indexed primary-key lookup, and buys the ability to
    disable an account *now*: a deactivated user's existing access token stops
    working on their next request instead of at the end of its 30 minutes.
    """
    if credentials is None:
        message = "Not authenticated"
        raise AuthenticationError(message)

    claims = decode_token(credentials.credentials, expected_type="access")

    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except ValueError as exc:
        message = "Could not validate credentials"
        raise AuthenticationError(message) from exc

    user = await session.scalar(select(User).where(User.id == user_id))

    if user is None or not user.is_active:
        # A deleted or deactivated user holding a still-valid token gets the
        # same answer as a forged one. There is nothing useful to distinguish.
        message = "Could not validate credentials"
        raise AuthenticationError(message)

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
"""Declare this in a route signature and the route is authenticated. That is
the entire protection mechanism, and it is visible in the signature."""


async def get_current_membership(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_db)],
    # Named `header_organization_id`, not `organization_id`, and the name is
    # load-bearing: FastAPI matches a dependency's parameters against the
    # route's path template by name, so a parameter called `organization_id`
    # inside a route declaring `/{organization_id}/members` is rejected at
    # import time with "Cannot use `Header` for path param". The alias is what
    # clients actually send; this name only has to avoid every path parameter.
    header_organization_id: Annotated[uuid.UUID, Header(alias=ORGANIZATION_HEADER)],
) -> Membership:
    """Resolve which organization the caller is acting in, and as what.

    This is the tenancy boundary. Every query in every later milestone filters
    by `membership.organization_id`, and this dependency is where that value
    comes from — never from a request body and never from a query parameter,
    both of which a caller could set to somebody else's organization.

    Being handed an id you are not a member of is a 404, not a 403: see
    `OrganizationService.get_membership`.
    """
    return await OrganizationService(session).get_membership(user.id, header_organization_id)


CurrentMembership = Annotated[Membership, Depends(get_current_membership)]
"""The org-scoped equivalent of CurrentUser. Carries both who the caller is
(`.user_id`) and what they may do (`.role`)."""


def require_role(*allowed: Role) -> Callable[[Membership], Awaitable[Membership]]:
    """Build a dependency that admits only the listed roles.

    A factory, because FastAPI dependencies take their arguments from the
    request — so "which roles?" cannot be one of them and has to be bound when
    the route is declared::

        @router.delete("/{org_id}", dependencies=[Depends(require_role(Role.OWNER))])

    Route-level checks are a convenience and a piece of documentation, not the
    enforcement point. The same rules live in `OrganizationService`, because
    workers and agent tools call services directly and never pass through a
    dependency at all.
    """
    allowed_roles = frozenset(allowed)

    async def dependency(membership: CurrentMembership) -> Membership:
        if membership.role not in allowed_roles:
            names = ", ".join(sorted(role.value for role in allowed_roles))
            message = f"This action requires one of: {names}"
            raise AuthorizationError(message)
        return membership

    return dependency
