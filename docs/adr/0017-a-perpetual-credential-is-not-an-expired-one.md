# ADR-0017: A credential that never expires is not an expired one

- **Status:** accepted
- **Date:** 2026-08-20
- **Milestone:** M14

## Context

M11 built the OAuth seam against exactly one provider, and said so: "adding the
second provider is a matter of writing one class." M14 added five — Gmail, Slack,
Notion, GitHub and Stripe — which is the first honest test of that claim.

The claim held for the *shape*. `IntegrationService`, the `state` security model,
the encrypted token store and the routes needed no structural change. What did not
hold was an assumption buried a layer below the seam, in the token store:
**every credential expires, and every credential can be refreshed.**

That is true of Google. It is false of three of the five new providers:

| Provider | `expires_in` | `refresh_token` |
|---|---|---|
| Google (Gmail, Calendar) | yes | yes, on first consent |
| Stripe Connect | no | yes |
| **Slack** (without rotation) | **no** | **no** |
| **Notion** | **no** | **no** |
| **GitHub** OAuth App (default) | **no** | **no** |

`OAuthToken.needs_refresh` returned True for a NULL `expires_at`, which M11
documented as the safe direction — an unnecessary refresh costs one HTTP call,
while assuming an unknown expiry is still valid costs a user-facing failure. With
Google that reasoning is correct.

Combined with `get_fresh_token`, which marks an integration `REVOKED` when there
is nothing to refresh with, it meant the **first use** of a freshly connected
Slack workspace destroyed the credential and told the user to reconnect — which
produced another credential of exactly the same shape, and the same result.
Forever.

Nothing in the test suite could see it. `OfflineOAuthProvider` always issued a
Google-shaped grant, so the failing combination never existed in a test.

## Decision

**A credential with no stated expiry and no refresh token is perpetual, not
expired.** Both conditions together: `expires_at IS NULL` alone also matches a
Google response that arrived malformed, and a refresh token is what makes renewal
possible at all. It is precisely the absence of both that means "nothing to renew,
and nothing said this would stop working".

`needs_refresh` returns False for such a token, so it is never sent to a refresh
endpoint and never revoked for failing to have one. M11's eager-refresh behaviour
is preserved for the case it was written for — no expiry *but* a refresh token.

**The test double must be able to produce the shape the real provider issues.**
`OfflineOAuthProvider(..., perpetual=True)` came before the fix, not after. A
double that only produces the shape the code already handles is a double that
certifies the bug — and this one had, through four milestones.

**Getting a token and noticing it died are one operation.** Under M11, `REVOKED`
had exactly one writer: the refresh path. A perpetual credential is never
refreshed, so nothing would ever have recorded one dying. The call would fail
with a 502, the row would stay `ACTIVE`, the integrations page would keep saying
"connected", and every later call would fail identically and invisibly.

    async with service.using(org_id, Provider.SLACK) as token:
        channels = await SlackClient().list_channels(token)

`using()` yields a live token and converts an `OAuthRevokedError` raised *inside*
the block into a recorded revocation plus a `NotFoundError` saying "reconnect it".
A 502 would invite a retry that cannot succeed.

**A revocation is committed, not flushed.** Every caller goes on to raise, and
`get_db` rolls the session back on any exception — so a flush was discarded by the
very failure that prompted it. This is the same rule ADR-0015 reached for approval
decisions, from a third direction: **a fact learned about the outside world must
survive the failure it caused.**

**Provider divergence belongs in the provider, not in the shared base.** Each of
the five differs somewhere a status-code-first client gets wrong — Slack and
GitHub report failure with HTTP 200, GitHub answers form-encoded unless asked for
JSON, Notion wants Basic auth on the token endpoint and issues no refresh token at
all. Each is handled in its own module, with the reason written where somebody
debugging will find it. `BaseClient` gained two hooks (`extra_headers`,
`forbidden_message`) and nothing provider-specific.

**Read-only unless the milestone earns the write.** M11 requested
`calendar.readonly` and M12 earned `calendar.events` by building approvals first.
M14 keeps that: Slack, Notion, GitHub and Stripe are read-only, and the one write
— sending email — goes through the existing approval row.

## Consequences

**No migration.** M11 declared all seven `Provider` values in the enum against
exactly this day, so five new integrations cost zero schema change. `alembic
check` reports nothing, which is the payoff of a decision made three milestones
ago.

**GitHub sees public repositories only, and that is the correct trade.** Classic
OAuth has no read-only scope for private repositories: `repo` grants read *and
write* to code, issues, pull requests and settings across everything the user can
reach. Requesting it in order to render a list of names would mean everyone who
connected GitHub had handed this application the ability to force-push to their
employer's codebase. M14 asks for `read:user`. The listing is smaller than a user
might expect, and the milestone note says so rather than leaving it a puzzle.

**Stripe's access token is a live secret key**, bounded by the `read_only` scope
*and* by `StripeClient` having no method that is not a GET. Either alone would be
a single point of failure for the worst outcome in this codebase.

**`Integration.scopes` was silently wrong for comma-separating providers.** M11
split on whitespace because RFC 6749 says space-delimited and Google obeys it;
Slack and GitHub send commas, so the first Slack connection would have stored one
long string as a single "scope". Nothing raises — it is display and audit data —
so the damage is a permissions list that is wrong in the one place a human looks.

**Google's flow became shared code.** M11 wrote it inside
`google_calendar/oauth.py`, correctly: a base class abstracting a single case is a
guess. Gmail is the second Google product and every line was identical apart from
the scope list, so it moved to `google_oauth.py` and both products became
four-line subclasses. The scope lists stayed in their own modules, because a scope
list is what a reviewer must be able to find without knowing the base class
exists.

**The email agent needed no new approval machinery**, which is what ADR-0015
claimed and had not demonstrated. `ApprovalService` gained one method that differs
from the calendar one in two lines. `AgentService.resume_calendar_run` became
`resume_approved_run` and dispatches on the stored `kind` — and because a lookup
now sits between the approval and the executor, the invariant ADR-0015 called
"identical by construction" is now also *checked*: the row's action must equal the
checkpoint's, or the run refuses.

**One deliberate exception to tracing the whole action.** An email body is not
recorded in `agent_steps`. That table is operational data, exported to whatever
observability stack a deployment runs; `approvals` is tenant-scoped. The body
still exists in `agent_runs.input` because the user typed it there — this reduces
copies from three to one, and does not hide anything.

**Google Drive stays unimplemented and unlisted.** Nothing in this product reads a
file from Drive, and an integration that connects successfully and then has no
endpoint to call is worse than one that says it is not available.

## What is not verified

The same limit as every milestone since M6, and it is worth being exact.

**No provider has ever been contacted.** There are no client ids or secrets in this
environment, so every flow here runs against `OfflineOAuthProvider`. What is
verified is the plumbing: state, code exchange, storage, encryption, expiry,
refusal, revocation, and the shape of each provider's responses as documented.
What is *not* verified is that each provider behaves as documented.

The per-provider quirks are encoded from documentation and asserted against canned
payloads. If Slack changes its error envelope, these tests keep passing.

**No email has been sent.** `test_email_approvals.py` drives the loop with a fake
Gmail client, and runtime verification stopped at the approval. The send path is
exercised; a real message reaching a real inbox is not.

M14 also added `_no_outbound_network` to `tests/conftest.py`, after an e2e test was
found making a real request to slack.com — and then caught a second one. A suite
that quietly depends on a third party is green on a good day, red during somebody
else's incident, and green again by the time anyone looks.
