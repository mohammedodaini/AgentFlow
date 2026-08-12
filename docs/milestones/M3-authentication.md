# M3 — Authentication and org scoping

**Status:** complete (2026-08-12) · **Gate:** `make check` green · **Tests:** 115 passing (was 48)

M2 gave the application state. M3 gives it *identity*: who is calling, which
organization they are acting in, and what they may do there. From here on,
every feature is built behind this boundary.

## What was built

| Module | Responsibility |
|---|---|
| [`app/core/security.py`](../../backend/app/core/security.py) | Argon2id hashing, JWT mint/verify — pure functions, no I/O |
| [`app/core/exceptions.py`](../../backend/app/core/exceptions.py) | `NotFound`/`Authentication`/`Authorization`/`Conflict`/`DuplicateEmail` |
| [`app/api/errors.py`](../../backend/app/api/errors.py) | Domain errors → HTTP status + one error body shape |
| [`app/db/redis.py`](../../backend/app/db/redis.py) | Redis client lifecycle (arrived early — see below) |
| [`app/auth/tokens.py`](../../backend/app/auth/tokens.py) | `TokenService` — issue, rotate, revoke, replay detection |
| [`app/auth/service.py`](../../backend/app/auth/service.py) | `AuthService` — register, login, refresh, logout |
| [`app/auth/dependencies.py`](../../backend/app/auth/dependencies.py) | `CurrentUser`, `CurrentMembership`, `require_role` |
| [`app/services/user_service.py`](../../backend/app/services/user_service.py) | Profile read/update |
| [`app/services/organization_service.py`](../../backend/app/services/organization_service.py) | Org creation, roster, invite, role change, removal + every role rule |
| [`app/schemas/`](../../backend/app/schemas/) | `common`, `auth`, `user`, `organization` — the API boundary |
| [`app/api/v1/routes/`](../../backend/app/api/v1/routes/) | `auth`, `users`, `organizations` |

Twelve new endpoints:

```
POST   /api/v1/auth/register                      201 → token pair
POST   /api/v1/auth/login                         200 → token pair
POST   /api/v1/auth/refresh                       200 → rotated token pair
POST   /api/v1/auth/logout                        204
GET    /api/v1/users/me                           200 → UserRead
PATCH  /api/v1/users/me                           200 → UserRead
POST   /api/v1/organizations                      201 → OrganizationRead
GET    /api/v1/organizations                      200 → [MembershipRead]
GET    /api/v1/organizations/{id}/members         200 → [MemberRead]
POST   /api/v1/organizations/{id}/members         201 → MemberRead
PATCH  /api/v1/organizations/{id}/members/{uid}   200 → MembershipRead
DELETE /api/v1/organizations/{id}/members/{uid}   204
```

## The decisions worth understanding

**1. Two token types with opposite tradeoffs.** Access tokens are stateless and
short (30 min); refresh tokens are revocable, long (7 days), and single-use.
Full reasoning, including what this design *cannot* promise, in
[ADR-0004](../adr/0004-stateless-access-tokens-with-rotating-refresh-tokens.md).

**2. The tenant travels in a header.** `X-Organization-Id`, resolved into a
`Membership` by one dependency. Required, never defaulted — see
[ADR-0005](../adr/0005-organization-scope-travels-in-a-header.md).

**3. Dependencies, not middleware.** Middleware needs a list of paths to skip,
which is a denylist, and a denylist of unprotected routes fails *open*: add a
route, forget the list, and it is public with nothing to notice. A dependency
fails closed — a route without `CurrentUser` in its signature is visibly
unprotected, right there in the function definition.

**4. Role rules live in the service, not the route.** `require_role(...)`
exists and works, but the actual enforcement is inside `OrganizationService`.
Routes are one of three callers; arq workers and agent tools are the others,
and neither passes through a FastAPI dependency. A rule that lives only in the
transport layer does not exist for two thirds of the callers.

**5. Nothing about authorization goes in the token.** No email, no role, no
organization. A claim is a snapshot that keeps being true to whoever reads it
long after it stopped being true — demote an admin and their existing token
would still say "admin" until it expired. The database is consulted per
request, which is also why deactivating a user takes effect immediately.

**6. Redis moved from M5 to M3.** `/auth/logout` has to revoke something; an
endpoint that returns 204 and leaves the token working is worse than no logout
at all. The M2 rule — *probe what you actually use* — then applied itself, so
`/health/ready` gained a Redis check in the same breath.

## The OWASP defences, and the tests that hold them

Each of these closes a specific attack, not a general good practice:

| Defence | Attack it closes | Test |
|---|---|---|
| Argon2id, per-hash salt | Offline cracking; identical hashes revealing shared passwords | `test_the_same_password_hashes_differently_every_time` |
| Identical error for bad password / unknown email | Account enumeration | `test_wrong_password_and_unknown_email_are_indistinguishable` |
| Dummy hash verified when no user exists | Enumeration by *timing* — the same oracle, rebuilt | `_TIMING_EQUALISER_HASH` in `auth/service.py` |
| `algorithms=[...]` whitelist | `alg: none`, RS256→HS256 confusion | `test_an_unsigned_token_is_rejected` |
| `typ` claim checked on decode | Using a 7-day refresh token as an access token | `test_a_refresh_token_cannot_open_a_protected_route` |
| Refresh rotation | A stolen refresh token working for 7 days undetected | `test_a_rotated_refresh_token_stops_working` |
| Email lowercased at the schema boundary | Two accounts for one human, because Postgres compares case-sensitively | `test_email_case_does_not_create_a_second_account` |
| Password max length | Argon2 is expensive by design → free DoS | `test_an_enormous_password_is_rejected` |
| `SECRET_KEY` validated at startup | Signing with a known key: anyone can mint a token for anyone | `test_production_refuses_the_placeholder_secret` |
| 404 (not 403) for a foreign organization | Enumerating tenant ids | `test_a_stranger_cannot_read_another_organization` |
| Admin cannot grant or touch ownership | Privilege escalation, in one step or two | `test_an_admin_cannot_grant_ownership` |
| Last owner cannot be demoted or leave | An ownerless org only a DBA can fix | `test_the_last_owner_cannot_leave` |

## Things this milestone got wrong first

**The placeholder secret was 9 bytes.** PyJWT warned on every token: RFC 7518
§3.2 wants at least 32 for HS256. The validator had been checking only for the
*exact* placeholder string, so any short custom key would have sailed through.
Both fixed — length is now checked too, and the placeholder was lengthened so
local runs are quiet.

**FastAPI refused to start.** `get_current_membership` named its header
parameter `organization_id`, and any route declaring `/{organization_id}/...`
raised `Cannot use Header for path param` at import time — FastAPI matches a
dependency's parameters against the path template *by name*. Renamed to
`header_organization_id`; the alias clients send is unchanged.

**E2E tests were about to write into the development database.** M2's tests
only read, so pointing the app at the dev stack was harmless. M3's create
users. The autouse fixture now repoints `DATABASE_URL` at `agentflow_test` and
`REDIS_URL` at database 1, and the test-database machinery moved from
`tests/integration/conftest.py` up to `tests/conftest.py`, where every layer
can reach it.

## Verified at runtime, not just in tests

```
$ curl -s localhost:8000/api/v1/health/ready
{"status":"ready","checks":{"database":true,"redis":true}}

$ curl -s -X POST .../auth/register -d '{"email":"...","password":"...","full_name":"Ada Lovelace"}'
{"access_token":"eyJhbGciOiJIUzI1NiIs...","refresh_token":"...","token_type":"bearer"}

$ curl -s .../users/me -H "Authorization: Bearer $ACCESS"
{"id":"019ff64e-1a49-712e-9659-94ebf1caad00","email":"...","full_name":"Ada Lovelace",
 "is_active":true,"is_verified":false,"created_at":"2026-08-12T14:09:01.520218Z"}

$ curl -s .../organizations -H "Authorization: Bearer $ACCESS"
[{"organization":{"slug":"ada-lovelace","plan":"free",...},"role":"owner"}]

$ curl -s -o /dev/null -w '%{http_code}' .../users/me      # no token
401

$ curl -s -X POST .../auth/refresh -d "{\"refresh_token\":\"$OLD\"}"   # replayed
{"error":{"code":"authentication_failed","message":"Could not validate credentials"}}
```

No `password_hash` in the profile response, and a registered user already owns
an organization — the two properties this milestone exists to guarantee.

## Test coverage

115 tests, up from 48:

- **unit (+20)** — Argon2 behaviour, JWT claim set, tampering, `alg: none`,
  both directions of type confusion, the startup secret guards.
- **e2e (+45)** — the full auth surface and the full org-scoping surface,
  including every privilege-escalation case in the table above.
- Redis probe tests join the readiness suite.

## Known gaps, deliberately left for later

- **No rate limiting on `/auth/login`.** Password guessing is bounded only by
  Argon2's cost. M16.
- **No email verification.** `is_verified` exists and is always `false`;
  nothing sends mail yet, which is also why `MemberInvite` can only add users
  who already have accounts.
- **No password reset.** Needs the same mail path.
- **Replay revokes one token, not the family.** The OAuth BCP's stronger
  response needs per-user token tracking — see the notes in ADR-0004.
- **Audit `events` rows are TODOs.** Every mutating service method is marked;
  the table lands in M16.
- **`updated_at` on role changes** is ORM-only, inherited from M2.
- **Test isolation is still truncate-based**, not transactional. M4.

## Reproduce

```bash
make up && make migrate
make check     # ruff + ruff format + mypy + pytest
make dev       # http://localhost:8000/docs
```
