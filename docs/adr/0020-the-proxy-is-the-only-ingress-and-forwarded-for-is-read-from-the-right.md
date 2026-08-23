# ADR-0020: The proxy is the only ingress, and `X-Forwarded-For` is read from the right

- **Status:** accepted
- **Date:** 2026-08-22
- **Milestone:** post-M16 (production audit)

## Context

The M16 production audit listed TLS termination as an open gap and closed the
milestone honestly: `docker-compose.prod.yml` published the API and the frontend
onto host ports over plain HTTP. Every password, access token and session cookie
crossed the network in the clear.

That is bad on its own, and it also makes ADR-0016 hollow. That ADR's whole
argument is that a refresh token belongs in an httpOnly cookie rather than
`localStorage`, because a compromised dependency can read `localStorage` and
cannot read an httpOnly cookie. An attacker reading the wire does not care which
it was.

Adding a reverse proxy raises a second question immediately, because a proxy
changes who the application thinks it is talking to.

## Decision

### One ingress, and everything else binds to loopback

Caddy publishes 80 and 443. The API and the frontend publish on `127.0.0.1`
only; Postgres, Redis and the worker publish nothing.

The published loopback ports are not decoration, `make smoke` and
`make loadtest` use them, and driving the product through TLS with a
self-signed internal CA to measure latency would be measuring the wrong thing.
But `8000:8000`, which is what they were, means `0.0.0.0:8000`: a second,
unencrypted, unproxied front door onto the internet, answering the same routes
as the one that terminates TLS.

Caddy rather than nginx: certificates. nginx needs certbot, a renewal timer and
a reload hook, and its failure mode is a site that goes down ninety days after
somebody set it up and left. Caddy obtains and renews in process.

A consequence worth naming: `/metrics` is mounted at the root, and the proxy
routes only `/api/*` to the API. So the metrics endpoint is no longer publicly
reachable at all: a request for it lands on Next.js and gets a 404. The token
check stays regardless, because that is a property of the application and this
is a property of one deployment.

### `X-Forwarded-For` is read from the right, and zero hops is the default

`client_ip` read the **first** entry of `X-Forwarded-For`, unconditionally. Two
things depend on it: the rate limiter's identity for anonymous traffic, and
`events.ip_address` in the audit trail.

It was wrong in both deployments the product can be in.

**With no proxy**: which is what production *was*: the header comes from the
caller and nowhere else. A client sending a different value on every request
gets a fresh rate-limit bucket every time. The limiter is defeated by one
header, on the same `/api/v1/auth` prefix the audit had just moved into the
expensive cost class specifically to make a login flood expensive. The audit
trail records whatever address the attacker typed.

**With a proxy** it is still wrong, because a proxy *appends*. A forged
`1.2.3.4` arrives as `1.2.3.4, <real client>`. The first entry is the forgery.
The truthful entry is the one our own proxy added, which is the last.

So: the header is read from the right, `TRUSTED_PROXY_HOPS` entries in.
`0`: the default, ignores the header entirely and uses the socket's peer
address. `docker-compose.prod.yml` sets `1`, because Caddy is in front.

**The default is the decision.** A deployment with nothing in front of it is
safe without configuring anything, and a value that makes the header
authoritative is a deliberate act by somebody who knows what is in front. The
opposite default is safe only if every deployer remembers, which is the same
class of assumption as "the operator will renew the certificate".

Caddy is separately configured to *replace* the header rather than append to it,
so the app receives exactly one entry that Caddy wrote. Two independent reasons
the value is trustworthy, because this file is a deployment detail and
`client_ip` is a property of the application.

## Consequences

The residual exposure is stated rather than hidden: with `TRUSTED_PROXY_HOPS=1`,
a request that reaches the API *without* passing through Caddy can supply its
own single-entry header and be believed. The only such route is
`127.0.0.1:8000`, which requires a shell on the host, and an attacker with one
is already past every control in the compose file.

`HTTP_PORT` and `HTTPS_PORT` exist so the stack can be rehearsed where 80 and
443 are taken, and must be left alone otherwise: Caddy builds its HTTP→HTTPS
redirect from the site address, and the ACME HTTP-01 challenge is answered on
port 80 from the public internet regardless of what they say.

## What this cost, and what it found

Verified by running it: 24/24 in a real browser against
`https://localhost:8443`, HTTP 308-redirected to HTTPS, `/metrics` 404 through
the proxy and served directly on the compose network, and a forged
`X-Forwarded-For: 1.2.3.4` sent through Caddy recorded in Postgres as the real
address.

Two bugs surfaced that no test would have found.

**The audit trail still recorded the frontend's own address.** The forwarding
helper was a private function in `api.ts`, called from `apiFetch`'s header
builder, and register, login and token refresh do not use `apiFetch`, because
they have no session yet and build their own headers. So the three endpoints
whose audit records are the entire point of the column recorded
`172.19.0.7`, the web container. A browser registration through the whole stack
is what said so. The helper now lives in its own module and every call site uses
it.

**The test suite was not isolated from a developer's `.env`.** `Settings` reads
the repo-root `.env` that the deploy runbook tells you to create, and the drill
created one, so `test_health.py` failed with `'tlsdrill' != 'dev'`. A previous
fix had cleared one variable with a comment predicting exactly this. Deleting
the variable does not help: pydantic-settings parses the file itself and the
values never pass through `os.environ`. The suite now disables the dotenv source
outright.

A third thing, smaller and the same shape: `make typecheck` ran `mypy app` (186
files) while CI ran `mypy .` (259). The local gate was the weaker one, so a type
error in a test file passed before a push and failed after it. The comment in
`ci.yml` claimed the opposite arrangement.
