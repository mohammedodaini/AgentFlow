"""Registration, login and the token lifecycle over HTTP (M3).

End-to-end against real Postgres and real Redis, because the parts most worth
testing here only exist when everything is wired together: that `/users/me` is
genuinely closed without a token, that a rotated refresh token genuinely stops
working, and that a failed login says the same thing no matter *why* it failed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from http import HTTPStatus
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.security import hash_password, verify_password

PASSWORD = "correct horse battery staple"
EMAIL = "ada@example.com"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"


async def register(
    client: AsyncClient, *, email: str = EMAIL, password: str = PASSWORD, **extra: Any
) -> dict[str, str]:
    """Register and return the token pair."""
    response = await client.post(REGISTER_URL, json={"email": email, "password": password, **extra})
    assert response.status_code == HTTPStatus.CREATED, response.text
    tokens: dict[str, str] = response.json()
    return tokens


def auth(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


async def test_registration_returns_a_token_pair(client: AsyncClient) -> None:
    """201 and both tokens, so the client never has to call /login next."""
    response = await client.post(REGISTER_URL, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == HTTPStatus.CREATED
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


async def test_registration_creates_a_personal_organization(client: AsyncClient) -> None:
    """Every user gets a workspace immediately.

    Without one, a freshly registered user cannot upload a document or start an
    agent run — every later feature hangs off an organization.
    """
    tokens = await register(client)

    response = await client.get("/api/v1/organizations", headers=auth(tokens))

    assert response.status_code == HTTPStatus.OK
    memberships = response.json()
    assert len(memberships) == 1
    assert memberships[0]["role"] == "owner"


async def test_duplicate_email_is_rejected_with_409(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(REGISTER_URL, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["error"]["code"] == "duplicate_email"


async def test_email_case_does_not_create_a_second_account(client: AsyncClient) -> None:
    """`Ada@Example.com` is the same person as `ada@example.com`.

    Postgres compares the UNIQUE column case-sensitively, so without
    normalisation at the schema boundary both rows would insert happily and the
    second registration would succeed.
    """
    await register(client)

    response = await client.post(
        REGISTER_URL, json={"email": "Ada@Example.COM", "password": PASSWORD}
    )

    assert response.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize("password", ["short", "elevenchars"])
async def test_a_short_password_is_rejected(client: AsyncClient, password: str) -> None:
    """422 from schema validation, before anything touches the database."""
    response = await client.post(REGISTER_URL, json={"email": EMAIL, "password": password})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_an_enormous_password_is_rejected(client: AsyncClient) -> None:
    """Argon2 is deliberately expensive, so an unbounded field is free DoS."""
    response = await client.post(REGISTER_URL, json={"email": EMAIL, "password": "a" * 5000})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


async def test_a_malformed_email_is_rejected(client: AsyncClient) -> None:
    response = await client.post(REGISTER_URL, json={"email": "not-an-email", "password": PASSWORD})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------


async def test_login_returns_a_token_pair(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == HTTPStatus.OK
    assert response.json()["access_token"]


async def test_wrong_password_and_unknown_email_are_indistinguishable(client: AsyncClient) -> None:
    """The account-enumeration defence, asserted rather than assumed.

    If these two responses ever differ — in status, in code, or in message — an
    attacker can sort a list of email addresses into "registered" and "not",
    which is exactly the input a credential-stuffing run wants.
    """
    await register(client)

    wrong_password = await client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong-one!!"})
    unknown_email = await client.post(
        LOGIN_URL, json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == HTTPStatus.UNAUTHORIZED
    assert wrong_password.json() == unknown_email.json()


async def test_a_401_carries_the_www_authenticate_header(client: AsyncClient) -> None:
    """RFC 9110 requires it; clients read it instead of guessing the scheme."""
    response = await client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --------------------------------------------------------------------------
# Protected routes
# --------------------------------------------------------------------------


async def test_me_requires_a_token(client: AsyncClient) -> None:
    """The whole point of the dependency: no token, no route."""
    response = await client.get(ME_URL)

    assert response.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    "header",
    ["Bearer not-a-token", "Bearer ", "Basic dXNlcjpwYXNz", "not-even-a-scheme"],
)
async def test_me_rejects_malformed_credentials(client: AsyncClient, header: str) -> None:
    response = await client.get(ME_URL, headers={"Authorization": header})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_me_returns_the_profile_without_the_password_hash(client: AsyncClient) -> None:
    """The reason schemas exist. `User` has `password_hash`; `UserRead` must not."""
    tokens = await register(client, full_name="Ada Lovelace")

    response = await client.get(ME_URL, headers=auth(tokens))

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["email"] == EMAIL
    assert body["full_name"] == "Ada Lovelace"
    assert "password_hash" not in body
    assert "argon2" not in str(body).lower()


async def test_a_refresh_token_cannot_open_a_protected_route(client: AsyncClient) -> None:
    """Token-type confusion, over HTTP this time.

    A refresh token lives for a week. If it were accepted as an access token,
    the 30-minute access TTL would protect nothing.
    """
    tokens = await register(client)

    response = await client.get(
        ME_URL, headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_profile_can_be_updated(client: AsyncClient) -> None:
    tokens = await register(client)

    response = await client.patch(ME_URL, json={"full_name": "Ada L."}, headers=auth(tokens))

    assert response.status_code == HTTPStatus.OK
    assert response.json()["full_name"] == "Ada L."


# --------------------------------------------------------------------------
# Token lifecycle
# --------------------------------------------------------------------------


async def test_refresh_issues_a_new_pair(client: AsyncClient) -> None:
    tokens = await register(client)

    response = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == HTTPStatus.OK
    rotated = response.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]

    me = await client.get(ME_URL, headers=auth(rotated))
    assert me.status_code == HTTPStatus.OK


async def test_a_rotated_refresh_token_stops_working(client: AsyncClient) -> None:
    """Single use. This is what makes a stolen refresh token survivable."""
    tokens = await register(client)
    await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

    replay = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})

    assert replay.status_code == HTTPStatus.UNAUTHORIZED


async def test_an_access_token_cannot_be_refreshed(client: AsyncClient) -> None:
    """The other half of the type check — a stolen access token must not be
    upgradeable into an indefinite session."""
    tokens = await register(client)

    response = await client.post(REFRESH_URL, json={"refresh_token": tokens["access_token"]})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_logout_revokes_the_refresh_token(client: AsyncClient) -> None:
    """The honest half of logout — verified against real Redis."""
    tokens = await register(client)

    logout = await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})
    assert logout.status_code == HTTPStatus.NO_CONTENT

    after = await client.post(REFRESH_URL, json={"refresh_token": tokens["refresh_token"]})
    assert after.status_code == HTTPStatus.UNAUTHORIZED


async def test_logout_is_idempotent(client: AsyncClient) -> None:
    """A retried logout, or a garbage token, still reports success.

    Anything else tells an attacker which tokens are real, and breaks clients
    that retry after a dropped response.
    """
    tokens = await register(client)

    first = await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})
    second = await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})
    garbage = await client.post(LOGOUT_URL, json={"refresh_token": "not-a-token"})

    assert first.status_code == second.status_code == HTTPStatus.NO_CONTENT
    assert garbage.status_code == HTTPStatus.NO_CONTENT


async def test_logout_leaves_the_access_token_working(client: AsyncClient) -> None:
    """A documented limitation, pinned so nobody mistakes it for a bug.

    Access tokens are not checked against the denylist — that would put a Redis
    round trip in front of every request. Logging out ends the *session*; the
    current access token dies of old age within 30 minutes.
    """
    tokens = await register(client)
    await client.post(LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]})

    response = await client.get(ME_URL, headers=auth(tokens))

    assert response.status_code == HTTPStatus.OK


# --------------------------------------------------------------------------
# Password hashing must not block the event loop
# --------------------------------------------------------------------------


async def _worst_loop_stall_during(work: Awaitable[Any]) -> tuple[float, float]:
    """Run `work`, watching how long the event loop is ever unavailable.

    Returns `(worst_stall_ms, work_ms)`. The watcher asks for a 1ms sleep in a
    loop; anything much longer than that means something ran to completion on the
    loop while it waited, which is precisely the failure being tested for.
    """
    stalls: list[float] = []
    running = True

    async def watch() -> None:
        previous = time.perf_counter()

        while running:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            stalls.append((now - previous) * 1000)
            previous = now

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0.01)  # let the watcher establish a baseline

    started = time.perf_counter()
    await work
    work_ms = (time.perf_counter() - started) * 1000

    running = False
    await watcher

    return max(stalls), work_ms


async def test_signing_in_does_not_block_the_event_loop(client: AsyncClient) -> None:
    """**A production audit found this, and it was an unauthenticated outage.**

    Argon2 is deliberately expensive. Called directly from an `async` path it runs
    *on the event loop* and stalls every other in-flight request for its whole
    duration — and the timing equaliser in `AuthService.login` hashes for unknown
    emails too, so no account is needed to trigger it. Measured against a live
    server before the fix: 72 login attempts took `/health/live` from a 3.2ms
    median to 60.7ms, and 400 took `/health/ready` to 496ms. One uvicorn process
    per container (ADR-0019) means one blocked loop is the whole container.

    The threshold is **calibrated on the machine running the test**, not a
    constant. Argon2's cost varies by an order of magnitude between a laptop and a
    shared CI runner, so a hard-coded millisecond figure would either be flaky
    there or meaningless here. Asserting the stall is a *fraction of the hash* is
    true on any hardware, and false the moment the hash runs on the loop.
    """
    await register(client)

    # What one hash costs here, measured rather than assumed.
    hashed = hash_password(PASSWORD)
    started = time.perf_counter()
    verify_password("not-the-password", hashed)
    hash_ms = (time.perf_counter() - started) * 1000

    stall_ms, _ = await _worst_loop_stall_during(
        client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})
    )

    assert stall_ms < hash_ms * 0.5, (
        f"the event loop stalled {stall_ms:.1f}ms during a sign-in, and one hash "
        f"costs {hash_ms:.1f}ms — the hash is running on the loop again"
    )


async def test_a_rejected_sign_in_does_not_block_the_event_loop(client: AsyncClient) -> None:
    """The half that matters for abuse: **no account required.**

    An attacker does not log in successfully. They guess, and the equaliser hash
    runs for every guess — so if only the success path had been moved to a thread,
    the denial of service would be entirely unaffected.
    """
    hashed = hash_password(PASSWORD)
    started = time.perf_counter()
    verify_password("not-the-password", hashed)
    hash_ms = (time.perf_counter() - started) * 1000

    stall_ms, _ = await _worst_loop_stall_during(
        client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": PASSWORD})
    )

    assert stall_ms < hash_ms * 0.5, (
        f"the event loop stalled {stall_ms:.1f}ms rejecting an unknown email; "
        f"one hash costs {hash_ms:.1f}ms"
    )


async def test_registering_does_not_block_the_event_loop(client: AsyncClient) -> None:
    """Registration hashes too, and it is reachable without an account by
    definition."""
    hashed = hash_password(PASSWORD)
    started = time.perf_counter()
    verify_password("not-the-password", hashed)
    hash_ms = (time.perf_counter() - started) * 1000

    stall_ms, _ = await _worst_loop_stall_during(
        client.post(REGISTER_URL, json={"email": "fresh@example.com", "password": PASSWORD})
    )

    assert stall_ms < hash_ms * 0.5, (
        f"the event loop stalled {stall_ms:.1f}ms during registration; "
        f"one hash costs {hash_ms:.1f}ms"
    )
