# ADR-0014: The state parameter is the callback's only credential, and tokens live encrypted in their own table

- **Status:** accepted
- **Date:** 2026-08-16
- **Milestone:** M11

## Context

Every credential this system has held so far was its own: a password hash, a JWT
it signed, an API key an operator configured. M11 introduces the first credential
that belongs to **somebody else's account**: a Google refresh token, long-lived
usable offline, and capable of reading a person's calendar without them present
and without anything appearing in their sign-in history.

Two problems arrive with it, and neither has an analogue earlier in the roadmap.

**The OAuth callback is unauthenticated and cannot be otherwise.** It is reached
by Google redirecting the *user's browser*, so no `Authorization` header, no
`X-Organization-Id`, and nothing else we issued survives the round trip. From the
application's point of view it is a request from a stranger saying "here is an
authorization code, please store it".

**A stored token is a liability the moment it is written.** Database dumps,
backups, read replicas, support sessions and `SELECT *` in an ad-hoc script are
all normal, and each of them would otherwise yield a working credential to a
customer's Google account.

## Decision

**`state` is the callback's only credential, and it does four jobs.** It is
`secrets.token_urlsafe(32)` so it cannot be forged; it *carries* the organization
and user binding, because the callback cannot be trusted to tell us; it expires;
and it is consumed exactly once, with `GETDEL`, so two callbacks arriving
together cannot both proceed.

Without the first two properties an attacker sends a victim a crafted callback
URL and *their* Google account becomes the one connected to the victim's
organization, after which the agent reads the attacker's calendar and, from M12
writes to it. That is login CSRF, it requires no access to our systems, and the
`state` check is the entire defence.

The binding lives in **Redis with a TTL**, per `app/db/redis.py`'s rule: this is
data that may disappear. A lost state costs one restarted connect attempt, the
cheapest possible failure, and self-expiring entries mean nothing accumulates.

**Every rejection returns the same message.** Forged, expired, already used, or
never issued are one answer, because distinguishing them would let an attacker
probe which states exist.

**Tokens are encrypted at the application layer, in their own table.** Fernet
(AES-128-CBC with an HMAC), applied by `encrypt_secret` before the value reaches
SQLAlchemy. The separate table is not normalisation: it is what makes least
privilege *expressible*: a reporting role or a support tool can be denied
`oauth_tokens` outright, which is impossible for columns on a table people
already read.

**Encryption is explicit at the call site, not a `TypeDecorator`.** A transparent
column type is the tempting design, and it makes the most dangerous operation in
the system invisible. It should be visible.

**The encryption key is separate from `SECRET_KEY`**, and production refuses both
the published placeholder and the two being equal. One key would mean a single
leak forges every session *and* decrypts every stored credential, and it would
couple two rotations whose urgencies have nothing in common.

**A revoked credential is a normal state with its own status.** `invalid_grant`
gets its own exception type (`OAuthRevokedError`) and its own row state
(`REVOKED`). Users revoke access, change passwords, and leave companies; none of
that reaches us as an event.

**Only `calendar.readonly` is requested.** A write scope now would sit unused
until M12 while every connected user had already granted an agent permission to
alter their diary. The approval machinery earns that scope, so it is requested in
the milestone that builds it.

## Consequences

**A boolean `is_connected` would have been wrong.** It cannot express *connected
but no longer working*, and both ways of forcing it are lies: false makes a real
integration look like it was never set up, true makes the agent keep trying a
credential that can never succeed. `REVOKED` says "this was real, it is broken,
reconnecting is the fix": the only thing a user can act on.

**Refreshing must keep the existing refresh token.** Google returns none on a
refresh, so code that writes the grant back wholesale nulls a long-lived
credential. Everything then works for up to an hour, and the integration dies far
from the change that caused it. The offline provider therefore returns no refresh
token *by default*, so any caller that gets this wrong fails immediately.

**`invalid_grant` arrives with HTTP 400, not 401.** The body decides, not the
status code. A classifier reading only the status would file a dead credential
under "bad request" and retry it forever.

**Expiry is checked before use, not after a 401.** Expired and revoked are
indistinguishable over HTTP, and only one of them should stop the integration.

**Ciphertext is non-deterministic, which constrains the schema.** These columns
can never be indexed, compared, or made UNIQUE; a token is only ever reached
through `integration_id`. Anything that ever needs a lookup by token content
needs a separate deterministic keyed hash and its own decision.

**Key rotation is not implemented, and the code says so.** `MultiFernet` decrypts
with any key in a list while encrypting with the first, which would make rotation
a deploy rather than an outage. One key is honest today; shipping a rotation path
nothing has exercised would not be.

**The offline provider is a real authorization server, not a stub.** It issues
single-use codes, expires tokens, refreshes them, and can be revoked mid-test
because those are the states the service has to handle, and OAuth is the one
feature here impossible to exercise honestly without a browser and a Google
account. It is refused in production more firmly than the offline embedder or
model: those degrade quality, while this one would let a user complete a connect
flow and hold an integration backed by tokens Google never issued.

**A provider outage is 502, not 500.** Found at runtime rather than by a test:
the events endpoint answered 500 because `OAuthError` had no entry in the central
mapping. 500 tells a client its request was fine and nothing more; 502 says an
upstream is down, so retrying may work, and it keeps our bugs and Google's
outages apart in the metrics. `OAuthRevokedError` deliberately never reaches that
mapping; the service converts it to a 404 carrying "reconnect it", because a
retry can never succeed.

**What is verified and what is not.** The tables, the encryption round trip and
its tamper detection, the `state` checks, the token refresh, the revocation path,
Google's error classification and the calendar payload translation are all
exercised: the last two against canned payloads through an injected transport.
What is *not* verified is Google itself: whether the real consent screen, the real
token endpoint and the real calendar API behave as documented. There are no
credentials in this environment, and no test here pretends otherwise.
