# ADR-0016: The browser never holds a JWT

- **Status:** accepted
- **Date:** 2026-08-20
- **Milestone:** M13

## Context

M3 built stateless access tokens (30 minutes) and rotating, revocable refresh
tokens (7 days), returned in a JSON body from `/auth/login`. That was the right
design for an API. M13 adds the first browser client, and a browser has to put
those tokens *somewhere*.

The default answer in a React application is `localStorage`. Every tutorial does
it, every HTTP client library assumes it, and it takes one line.

## Decision

**The tokens are stored in httpOnly cookies set by the Next server, and the
browser never sees them.** `src/lib/api.ts`: the only code that attaches a
credential, imports `server-only`, so importing it from a Client Component is a
build error rather than a silent security regression.

Every write goes through a **Server Action**. Every read happens in a Server
Component.

## Why not localStorage

A refresh token is a seven-day, offline credential that mints access tokens. In
`localStorage` it is readable by any script executing on the page.

That threat is not hypothetical or exotic. It does not require a bug in our code:
one compromised release of one transitive npm dependency, one injected analytics
tag, one `dangerouslySetInnerHTML` over user content, and `localStorage.getItem`
plus a `fetch` exfiltrates it. The attacker then has a week of access from their
own machine, and nothing in our logs distinguishes them from the user.

With httpOnly cookies the same XSS is still bad: the attacker can act as the
user *on that page, while it is open*. It is bounded to the session and the
device. That is the difference between an incident and a breach.

## Consequences

**Every API call goes through the Next server.** A real cost: one extra hop, and
a Node process that has to be up for the UI to work at all. For a product whose
interactions are "ask a question and wait for an agent" this is noise next to the
model call.

**There is no CORS configuration anywhere.** The browser only ever talks to its
own origin, so the backend never allowlists a browser origin, and there are no
preflight requests. A whole category of configuration and a whole category of
bug simply do not exist here.

**The password never enters a client bundle.** Server Actions post the form
directly to the server, so no browser code has ever held it, and the login page
works with JavaScript disabled, which is a pleasant accident rather than a goal.

**`revalidatePath` becomes available**, and it is what keeps the UI correct after
a write. Sending a message re-renders the transcript; approving empties the
inbox. The client-`fetch` alternative needs client state that can disagree with
the server, which is where "I approved it but it's still showing" comes from.

**`sameSite=lax`, not `strict`.** Strict would drop the session cookie on the
OAuth callback: Google redirects the browser to us, a cross-site navigation
and the user would land back apparently signed out having just connected their
calendar. `lax` sends cookies on top-level GET navigations, which is that case,
and withholds them on the cross-site POST that CSRF actually needs.

**Cookie lifetimes are shorter than the tokens they carry** (25 minutes, 6 days).
The browser discards a cookie while its token is still valid, rather than
presenting a dead credential and getting a 401 the user has to interpret.

**Refresh is retried exactly once.** An access token expires mid-session by
design. `apiFetch` refreshes and retries a 401 once: not in a loop, because if
the refreshed token is also rejected the credential is genuinely dead, and
retrying turns one bad session into a request storm against `/auth/refresh`. On
failure the cookies are cleared, so the next request does not repeat the dance.

**A stolen device is still a stolen session**, and nothing here changes that. The
cookie is on the machine. httpOnly defends against remote script access, not
against someone holding the laptop.

**The tokens are stored as-is rather than inside one encrypted session cookie.**
That alternative works and would mean this frontend owned a second key and a
second crypto decision. The backend already signs these tokens and already knows
how to revoke a refresh token by `jti` (M3); keeping them unwrapped leaves
exactly one system responsible for their validity.
