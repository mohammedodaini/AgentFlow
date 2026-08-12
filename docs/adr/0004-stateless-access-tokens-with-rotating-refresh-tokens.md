# ADR-0004: Stateless access tokens, rotating revocable refresh tokens

- **Status:** accepted
- **Date:** 2026-08-12
- **Milestone:** M3

## Context

Every request after login has to answer "who is this?". The answer is either
looked up in a store, or carried by the request and verified.

Three facts about AgentFlow shape the choice:

1. **Requests are cheap and frequent.** A chat UI polls, an agent run streams,
   the frontend loads several resources per screen. Anything paid per request
   is paid a great many times.
2. **There will be more than one process.** The API and the arq worker both
   need to know who triggered an agent run, and neither owns the other's
   memory.
3. **A stolen credential must be survivable.** This is a B2B product holding
   customers' documents and connected mailboxes. "Log out" has to mean
   something, and a leaked long-lived token cannot be permanent.

Properties 1 and 2 push toward stateless tokens. Property 3 pushes toward
server-side session state. They genuinely conflict.

## Decision

Two token types with different tradeoffs, issued as a pair:

| | Access | Refresh |
|---|---|---|
| Lifetime | 30 minutes | 7 days |
| Checked against a store? | No | Yes |
| Revocable? | No | Yes |
| Sent on | every request | `/auth/refresh`, `/auth/logout` only |

**Access tokens are stateless.** A signed JWT (HS256), verified with the
signing key and nothing else. No database read, no Redis read.

**Refresh tokens rotate and are revocable.** Every call to `/auth/refresh`
revokes the presented token and issues a new pair, so a refresh token is
single-use. Revocation is a Redis key `revoked:jti:<jti>` whose TTL equals the
token's remaining lifetime.

Both carry a `typ` claim, checked on decode, so the two are not
interchangeable.

## Consequences

**Good.** Authentication costs one HMAC verification and no network round trip
— property 1. Any process holding the signing key can verify a token, so the
worker needs no session store — property 2. A stolen refresh token stops
working the moment the real user next refreshes, and its reuse is *detectable*:
presenting an already-spent token is either theft or a tab race, and it is
logged as `auth.refresh_token_replayed`. Logout genuinely ends the session.

The denylist cannot grow without bound, and that is structural rather than
operational: each entry expires exactly when the token it denies would have
expired anyway. There is no cleanup job to forget to run.

**Bad, and this is the honest cost.** An access token cannot be called back. A
user who logs out leaves a window of up to 30 minutes in which their existing
access token still works. That window is the price of property 1, and 30
minutes is the dial: shorten it for more safety and more refresh traffic,
lengthen it for the reverse.

That statement has one exception worth knowing. `get_current_user` loads the
user row on every request, so *deactivation* does take effect immediately — the
token verifies, then the lookup finds `is_active=False` and rejects it. It is
logout specifically, and role changes, that wait out the window.

**Also bad.** HS256 means every verifier can also mint. That is fine while one
service does both, and becomes wrong the first time an external service needs
to verify a token — at which point this moves to RS256 and the verifier gets
only the public key. `jwt_algorithm` is already configuration for that reason.

## Alternatives rejected

**Server-side sessions (a session id in a cookie, state in Redis).** Genuinely
simpler to reason about, and revocation is instant and total. Rejected on
property 1: every request becomes a Redis round trip, and Redis becomes a hard
dependency of *authentication* rather than of logout — when it blinks, nobody
can use the product at all, instead of nobody being able to sign out.

**Long-lived access tokens with no refresh token.** One credential, no rotation
machinery. Rejected on property 3: the only revocation available is a denylist
consulted on every request, which is the previous option wearing a JWT.

**Checking access tokens against the denylist too.** Buys instant logout and
gives up the entire reason for JWTs. If that becomes a requirement — and for
some compliance regimes it will — the honest move is to adopt server-side
sessions deliberately, rather than bolt a lookup onto every request and keep
calling it stateless.

**No rotation: a refresh token reusable until it expires.** Less code. Rejected
because it makes theft undetectable and unbounded — a copied refresh token
would work for the full seven days with nothing to notice, whereas rotation
guarantees exactly one of the two holders breaks immediately, and that this
fact appears in the logs.

## Notes for later milestones

- Replay currently rejects the single token. The stronger response, from the
  OAuth 2.0 security BCP, is to revoke the whole token *family* — every session
  descended from the same login — on the theory that a replay means one of the
  two holders is an attacker and you cannot tell which. That needs per-user
  token tracking, and is deliberately deferred.
- Nothing here rate-limits `/auth/login`. Password guessing is currently
  bounded only by Argon2's cost. Rate limiting is M16.
