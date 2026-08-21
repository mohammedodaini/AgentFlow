"""Auth FLOWS: register, login, refresh, logout.

Uses core/security primitives; owns the transaction (register = user +
personal org + owner membership + audit event, atomically).

"Owns the transaction" means it decides what belongs in one — not that it
commits. The commit stays in `get_db()` (see app/db/deps.py), which is what
lets a future flow wrap this one in a larger unit of work.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import TokenPair, TokenService
from app.core.exceptions import AuthenticationError, DuplicateEmailError
from app.core.security import (
    hash_password_async,
    needs_rehash,
    verify_password_async,
)
from app.models.event import EventType
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.event_service import EventService
from app.services.organization_service import OrganizationService

logger = structlog.get_logger(__name__)

_INVALID_CREDENTIALS = "Incorrect email or password"
"""One message for "no such user" and for "wrong password".

Distinguishing them turns the login endpoint into an account-enumeration
oracle: an attacker learns which addresses are registered, which is exactly the
list they want before a credential-stuffing run. The same reasoning applies to
the *timing* of the two paths — see `login`.
"""

_TIMING_EQUALISER_HASH = "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43
"""A syntactically valid Argon2 hash that nothing can match.

Verified against when no user exists, so both paths spend the same ~50 ms in
Argon2. Skip this and "no such user" returns in a millisecond while "wrong
password" takes fifty — a difference any client can measure, which rebuilds the
enumeration oracle that the shared error message just closed.
"""


class AuthService:
    """Registration and session flows.

    Composes two other services rather than reimplementing them:
    `OrganizationService` knows how to create an org with an owner, and
    `TokenService` knows the token lifecycle. This class knows the *order*.
    """

    def __init__(
        self, session: AsyncSession, tokens: TokenService, *, ip_address: str | None = None
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._organizations = OrganizationService(session)
        self._events = EventService(session)
        # M16. The caller's address, for the audit trail. Passed in rather than
        # read here, because a service has no request — and threading it through
        # is what keeps this layer callable from a worker and a test.
        self._ip_address = ip_address

    async def register(self, request: RegisterRequest) -> tuple[User, TokenPair]:
        """Create an account, its personal organization, and a token pair.

        Why an organization is created here rather than later: every other
        table in this application hangs off `organizations`, so a user without
        one cannot upload a document or start an agent run. Making the first
        organization a separate step the client must remember means every
        client gets to forget it, and the resulting half-provisioned accounts
        are indistinguishable from bugs.

        All three rows are written in one transaction. A user with no
        organization, or an organization with no owner, must never be a state
        this system can be observed in.
        """
        email = request.email

        if await self._session.scalar(select(User.id).where(User.email == email)):
            message = "An account with that email already exists"
            raise DuplicateEmailError(message)

        user = User(
            email=email,
            password_hash=await hash_password_async(request.password.get_secret_value()),
            full_name=request.full_name,
        )
        self._session.add(user)
        await self._session.flush()

        organization, _ = await self._organizations.create(
            name=request.full_name or email.split("@")[0],
            owner=user,
        )

        logger.info(
            "auth.registered",
            user_id=str(user.id),
            organization_id=str(organization.id),
        )
        await self._events.record(
            EventType.USER_REGISTERED,
            organization_id=organization.id,
            actor_user_id=user.id,
            ip_address=self._ip_address,
            email=email,
        )
        return user, self._tokens.issue_pair(user.id)

    async def login(self, request: LoginRequest) -> tuple[User, TokenPair]:
        """Verify credentials and issue a token pair.

        Note the shape of the failure handling: every rejection raises the same
        error with the same message, and the no-such-user path still performs a
        hash verification so it costs the same time as a real one.

        **Every hash runs in a thread, and a production audit is what found out
        why.** Argon2 is deliberately expensive — 36.8ms of CPU per verification
        on this machine — and calling it directly from an `async` method runs it
        *on the event loop*, where it blocks every other in-flight request for
        that whole time.

        Measured against a live server before the fix: 72 login attempts took
        `/health/live` from a 3.2ms median to 60.7ms, and 400 attempts took
        `/health/ready` to 496ms. None of that needed an account — the timing
        equaliser above hashes for unknown emails too, which is exactly what makes
        it a free denial of service. `ADR-0019` compounds it: one uvicorn process
        per container means one blocked loop is the entire container, and a
        readiness probe that crosses its timeout gets that container killed.

        `verify_password_async` moves the work into a small bounded pool
        (`app/core/security.py`), which keeps the loop responsive and caps the
        memory — each concurrent Argon2 allocates 64 MiB, and an unbounded pool
        peaks at 2 GB. It does **not** make a flood cheap: refusing the flood is
        the limiter's job, and `/api/v1/auth` is now in `EXPENSIVE_PREFIXES` for
        that reason.

        Deactivated accounts are rejected here too, deliberately with that same
        message. Telling a disabled user "your account is suspended" also tells
        anyone working through a stolen password list which of their guesses
        were correct.
        """
        email = request.email
        password = request.password.get_secret_value()

        user = await self._session.scalar(select(User).where(User.email == email))

        if user is None:
            await verify_password_async(password, _TIMING_EQUALISER_HASH)
            logger.info("auth.login_failed", reason="unknown_email")
            # `record_now`, not `record`: this raises next, and `get_db` rolls
            # back on any exception — so a flushed event would be discarded by the
            # failure it documents. See `EventService.record_now`.
            #
            # No `actor_user_id`, because there is no user. The email is recorded
            # instead: "which addresses is somebody guessing?" is the question a
            # credential-stuffing investigation actually asks.
            await self._events.record_now(
                EventType.USER_SIGN_IN_FAILED,
                ip_address=self._ip_address,
                reason="unknown_email",
                email=email,
            )
            raise AuthenticationError(_INVALID_CREDENTIALS)

        if not await verify_password_async(password, user.password_hash):
            logger.info("auth.login_failed", reason="bad_password", user_id=str(user.id))
            await self._events.record_now(
                EventType.USER_SIGN_IN_FAILED,
                actor_user_id=user.id,
                ip_address=self._ip_address,
                reason="bad_password",
            )
            raise AuthenticationError(_INVALID_CREDENTIALS)

        if not user.is_active:
            logger.info("auth.login_failed", reason="inactive", user_id=str(user.id))
            await self._events.record_now(
                EventType.USER_SIGN_IN_FAILED,
                actor_user_id=user.id,
                ip_address=self._ip_address,
                reason="inactive",
            )
            raise AuthenticationError(_INVALID_CREDENTIALS)

        # Login is the only moment the plaintext password exists, so it is the
        # only moment a hash made with older, cheaper parameters can be
        # upgraded. Users re-hash silently, one at a time, as they sign in.
        if needs_rehash(user.password_hash):
            user.password_hash = await hash_password_async(password)
            logger.info("auth.password_rehashed", user_id=str(user.id))

        logger.info("auth.login_succeeded", user_id=str(user.id))
        # `record`, not `record_now`: this path succeeds, so the event commits
        # with the request — including the silent re-hash above, if it happened.
        await self._events.record(
            EventType.USER_SIGNED_IN, actor_user_id=user.id, ip_address=self._ip_address
        )
        return user, self._tokens.issue_pair(user.id)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, invalidating the old one.

        Thin by design — the interesting logic is rotation and replay
        detection, and that lives in `TokenService`, where the denylist is.
        """
        return await self._tokens.rotate(refresh_token)

    async def logout(self, refresh_token: str) -> None:
        """Revoke a refresh token.

        Only the refresh token. The access token stays valid for up to its
        remaining 30 minutes, because checking a denylist on every request
        would put a Redis round trip in front of every endpoint. That is the
        standard trade, and it is why the access TTL is short — but it does
        mean "log out everywhere, right now" is not something this design can
        honestly promise. See docs/milestones/M3-authentication.md.
        """
        await self._tokens.revoke(refresh_token)
