# ADR-0005: Organization scope travels in a request header

- **Status:** accepted
- **Date:** 2026-08-12
- **Milestone:** M3

## Context

[docs/database.md](../database.md) settled the schema question: almost every
table hangs off `organizations`, and a user reaches one through `memberships`.
What M3 had to settle is the *transport* question — how a request says which
organization it is acting in.

This is not a small choice. Every query in every later milestone filters by
`organization_id`, so wherever that value comes from becomes the tenancy
boundary of the entire product. Two properties matter:

1. **It must be impossible to forget.** A route that omits the filter returns
   another customer's data. The mechanism has to make omission loud.
2. **A user belongs to several organizations.** A consultant with three clients
   switches context in the UI without logging out, so the scope cannot be
   attached to the session.

## Decision

An `X-Organization-Id` header, resolved once by `get_current_membership` in
`app/auth/dependencies.py`, which returns the caller's `Membership` — carrying
both the organization id and the caller's role in it.

The header is **required** on scoped endpoints: FastAPI's own validation
returns 422 when it is missing. There is no default organization.

Where a route also carries an organization id in its path —
`/organizations/{id}/members` — the two must agree, or the request is refused
with 404.

Exactly two authenticated endpoints are unscoped, and both by necessity:
`GET /users/me` (identity, not tenant data) and `GET|POST /organizations` (you
cannot scope a request to an organization you have not chosen or created yet).

## Consequences

**Good.** Property 1 is satisfied by construction: `CurrentMembership` in a
route signature *is* the tenancy check, and a route that omits it is visibly
unscoped in its own definition and in the OpenAPI schema. Property 2 costs the
client one header, and no URL changes when a user switches workspace — which
also means bookmarks and deep links survive.

Because the id is resolved into a `Membership`, the role arrives with it. Every
scoped route gets its authorization data for free, without a second query.

Being handed an organization you do not belong to is a **404, not a 403**. 403
would confirm the organization exists, which turns any scoped endpoint into an
oracle for enumerating tenant ids.

**Bad.** A header is easier to forget than a path segment, and it is invisible
in a URL — you cannot paste an org-scoped link to a colleague and have it just
work. Requiring the header rather than defaulting converts that from a silent
correctness bug into a loud 422, but the ergonomic cost is real and it lands on
every client author.

**Also bad.** Two sources for one value now exist on nested routes, and they
have to be reconciled by hand in each. `_require_same_organization` in
`app/api/v1/routes/organizations.py` does that today; a future route that
forgets it would authorize against the header and act on the path — the exact
confused-deputy bug this ADR exists to prevent. That is a wart, and the
mitigation is that scoped *collections* should not repeat the org id in their
paths at all: `/documents`, not `/organizations/{id}/documents`.

## Alternatives rejected

**A path prefix: `/organizations/{organization_id}/documents/...`.** Explicit,
linkable, and impossible to omit — genuinely the strongest option on property
1. Rejected because it puts the tenant in every URL of the product, so every
link breaks when a user switches workspace and every client builds every path
by string concatenation. Worth revisiting if the header's ergonomics prove
worse in practice than they look now.

**Baked into the JWT.** One claim, zero extra lookups. Rejected on property 2:
a token pinned to one organization means switching workspace requires a new
token, and revoking a membership would not take effect until the old token
expires. Consistent with ADR-0004, which keeps *all* authorization data out of
the token for precisely this reason.

**A query parameter or a request body field.** Rejected outright. Both are part
of the resource being addressed rather than the context addressing it, both
land in access logs and browser history, and a body field cannot scope a GET.

**Defaulting to the user's first (or only) organization.** The tempting
convenience. Rejected as the worst option available: a client that forgets the
header would silently succeed against the wrong tenant, and a bug that succeeds
is a bug nobody reports.
