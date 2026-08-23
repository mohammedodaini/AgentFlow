# M11: The first OAuth integration: somebody else's credential

- **Date:** 2026-08-16
- **Status:** shipped
- **ADRs:** [ADR-0014](../adr/0014-the-state-parameter-is-the-callbacks-only-credential.md)

Every credential this system had held was its own: a password hash, a JWT it
signed, an API key an operator configured. This milestone introduces the first one
that belongs to **somebody else's account**.

## What was built

| Piece | File | What it does |
|---|---|---|
| Encryption | `app/core/security.py` | `encrypt_secret` / `decrypt_secret`, Fernet |
| `integrations` | `app/models/integration.py` | One connected product per org; a status lifecycle |
| `oauth_tokens` | `app/models/oauth_token.py` | Ciphertext only, in its own table |
| Migration | `alembic/versions/…8b9d4d253f41_…` | Both tables, two enums, a partial unique index |
| Seam | `app/integrations/base.py` | `OAuthProvider`, `TokenGrant`, two error types |
| Google | `app/integrations/google_calendar/{oauth,client}.py` | The real provider; read-only calendar |
| Offline | `app/integrations/offline.py` | A working authorization server, in memory |
| Registry | `app/integrations/__init__.py` | One provider instance per process |
| Repository | `app/repositories/integration_repository.py` | Tenant-scoped, tokens eagerly loaded |
| Service | `app/services/integration_service.py` | State, callback, refresh, disconnect |
| API | `app/api/v1/routes/integrations.py` | Five endpoints, one deliberately unauthenticated |

## The two ideas

**The callback has exactly one credential, and it is `state`.** Google redirects
the *browser* back to us, so nothing we issued survives: no bearer token, no org
header. `state` therefore has to be unguessable, carry the tenant binding, expire,
and work once. Drop the first two and an attacker mails a victim a crafted callback
URL, and the attacker's Google account becomes the one connected to the victim's
organization. That is login CSRF; it needs no access to our systems.

**A token is a liability from the moment it is written.** Fernet ciphertext, in a
*separate table*, which is not normalisation but the thing that makes least
privilege expressible. A reporting role can be denied `oauth_tokens`; it cannot be
denied two columns on a table it already reads. And the encryption is explicit at
the call site rather than a transparent column type, because the most dangerous
operation in the system should be visible in the code that performs it.

## Bugs this milestone found

Four, and the last two are the interesting ones.

**1. The enum outlived its table in `downgrade()`.** Fifth milestone running (M2,
M5, M9, M10, M11). Autogenerate has still never written that line.

**2. `MissingGreenlet`, for the sixth time in this codebase.** The repository
documents an eager-load contract: every read uses
`selectinload(Integration.tokens)`: and the *create* path violated it. A freshly
constructed `Integration` leaves the relationship unloaded, and after the flush it
is a persistent object whose first `.tokens` access is a lazy SELECT. Under asyncio
that raises, from inside the service line that stores the credential. Fixed by
assigning `tokens=None` before the flush, while the object is still pending. **A
documented contract with one code path that ignores it is not a contract.**

**3. A provider outage answered 500, and only a runtime check found it.** The whole
test suite was green: no test asserted the *status code* of a failed Google call,
because every test touching the client asserted on the exception instead. Driving
the real endpoint with curl returned 500, telling a client its request was fine and
nothing more, when the truth was "an upstream is down, retrying may work".
`OAuthError` now maps to 502. Third milestone running where the runtime pass found
something the tests could not (M7's citations on a refusal, M10's control test,
this).

**4. A subclass that would have inherited the wrong answer.** `OAuthRevokedError`
*is* an `OAuthError`, so the new 502 mapping would have caught it too, inviting a
client to retry a credential that can never work again. The service converts it to
a 404 carrying "reconnect it" before it reaches the mapping. Noticed while writing
the test for bug 3, and now pinned by its own test.

## Verified at runtime

Real uvicorn, curl, no mocks:

```
1. connect  → https://offline.agentflow.test/authorize?state=6I7qW9aApgy…
2. callback with NO auth header at all → 200, status "active",
   account ada@example.test, scopes [calendar.readonly, userinfo.email]
3. replaying the same callback         → 401
4. a forged state                      → 401
5. what Postgres actually holds:
     access_token  gAAAAABqgOkH…
     refresh_token gAAAAABqgOkH…
     expires_at    not null
6. reading events (offline tokens cannot reach Google) → 502
7. disconnect → status "disconnected", oauth_tokens count 0
```

Step 2 is the milestone: the callback works with no credential except `state`.
Step 5 is the point of the encryption: that is what a database dump yields. Step 7
is the disposal rule: the row survives for the audit trail, the credential does not.

## Gate

```
ruff · ruff format · mypy --strict (228 files) · alembic check · make eval
667 tests, 2 skipped · 97.77% coverage (gate 97%)
```

**`make eval` unchanged** against M8's committed baseline, as it should be, M11
touches no prompt and no retrieval path.

## Known gaps, deliberately left

**Google itself is unverified.** The tables, the encryption round trip and its
tamper detection, the `state` checks, refresh, revocation, Google's error
classification and the calendar payload translation are all exercised: the last two
against canned payloads through an injected transport. Whether the real consent
screen, token endpoint and Calendar API behave as documented is not tested, because
there are no credentials in this environment. No test here pretends otherwise, and
the offline provider is refused in production so nobody can ship the pretence.

**Key rotation is not wired up.** `MultiFernet` would make it a deploy rather than
an outage. One key is honest today; a rotation path nothing has exercised would not
be.

**Read-only, and the scope says so.** `calendar.readonly` only. M12's approvals are
what earn a write scope, and requesting it now would mean every connected user had
already granted an agent permission to alter their diary.

**No token cleanup job.** A `DISCONNECTED` row keeps its history and loses its
credential immediately, so nothing dangerous accumulates, but nothing prunes old
rows either. That is a retention decision, and it belongs with the other one M16
owns.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8099
```

```bash
URL=$(curl -s localhost:8099/api/v1/integrations/google_calendar/connect \
  -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" | jq -r .authorize_url)

# The offline authorization server embeds state and code in the redirect, so the
# consent screen is the one thing you do not have to click through.
curl "localhost:8099/api/v1/integrations/google_calendar/callback?state=…&code=…"
```

Set `OAUTH_PROVIDER=google` with real `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` to
drive the same flow against Google, and register
`http://localhost:8099/api/v1/integrations/google_calendar/callback` as an
authorized redirect URI, exactly, because Google matches it character for character.
