# M14: More integrations: one pattern, repeated, and where it did not fit

- **Date:** 2026-08-20
- **Status:** shipped
- **ADRs:** [ADR-0017](../adr/0017-a-perpetual-credential-is-not-an-expired-one.md)

`docs/roadmap.md` describes M14 as *"Slack, Notion, GitHub, Stripe (one pattern,
repeated, by now integrations are routine)"*. That turned out to be true of the
seam and false of the layer underneath it, which is the whole story of this
milestone.

M11's `OAuthProvider` protocol, the `state` security model, the encrypted token
store and the routes all absorbed five new providers without structural change.
The **token store** did not: it assumed every credential expires and every
credential can be refreshed, and three of the five issue credentials that do
neither. See [ADR-0017](../adr/0017-a-perpetual-credential-is-not-an-expired-one.md).

This is also the milestone that finishes M12, which shipped the calendar half of
"Calendar write + Email draft/send behind approval" and said plainly that the
other half waited on Gmail OAuth.

## What was built

| Piece | Files | What it does |
|---|---|---|
| Gmail | `app/integrations/gmail/` | Read mail, create a draft, send an approved draft |
| Slack | `app/integrations/slack/` | List public channels |
| Notion | `app/integrations/notion/` | Search shared pages |
| GitHub | `app/integrations/github/` | List repositories |
| Stripe | `app/integrations/stripe/` | List recent charges |
| Shared Google flow | `app/integrations/google_oauth.py` | One flow, two products |
| Perpetual credentials | `app/models/oauth_token.py` | `is_perpetual`, and the fix |
| `using()` | `app/services/integration_service.py` | A token, and the record if it dies |
| Registry | `app/integrations/__init__.py` | Only what is configured |
| Email agent | `app/agents/email/` | Propose → approve → send |
| Shared execution | `app/agents/execution.py` | The execute graph, and kind dispatch |
| API | `app/api/v1/routes/integrations.py` | Six read endpoints, plus `/providers` |
| Frontend | `frontend/src/app/(app)/integrations/` | Connect, reconnect, disconnect |

**No migration.** M11 declared all seven `Provider` values in the enum against
exactly this day, so five new integrations cost zero schema change and `alembic
check` reports nothing. That is a decision made three milestones ago paying for
itself.

**No new dependencies.** `httpx` was already here. The official SDKs listed as
optional in `docs/packages.md` (`slack-sdk`, `PyGithub`, `stripe`,
`notion-client`) were not added: each would be a package to keep current for one
or two endpoints, and the boundary rule, provider types never leak upward, means
their models would be translated away at the door regardless.

## The five providers, and what each one breaks

Every one of them diverges from Google somewhere that a status-code-first client
gets wrong. Each divergence has a test naming what would break.

**Slack reports failure with HTTP 200.** A reused code, a bad secret, a dead token,
all arrive as `200 OK` with `{"ok": false, "error": "..."}`. `response.is_error`
is False for every one. Written like `GoogleCalendarOAuth`, which checks the status
first, a failed exchange sails past every check and raises `KeyError:
'access_token'` three frames from the provider that refused us. The same applies to
its API: a dead token would have produced an empty channel list rather than an
error: an integration that appears to work and reports that the workspace has no
channels.

**GitHub's token endpoint is not JSON unless you ask.** It defaults to
`application/x-www-form-urlencoded`, so `response.json()` raises, `_json_or_empty`
swallows it, and the error reads "GitHub's token response contained no
access_token": the right provider and the wrong cause. It also reports a bad code
with HTTP 200, like Slack.

**Notion wants the client credentials in an `Authorization: Basic` header**, not
in the body. It is the one provider where getting the *location* of the secret
wrong looks exactly like getting the secret wrong. It has **no scopes at all**
access is granted per page, in a picker, so `SCOPES` is empty and
`Integration.scopes` is `[]` for every Notion connection. Writing something
plausible like `["read_content"]` would put a permission in the audit list that
Notion never issued.

**Stripe hands over a live secret key.** The Connect access token is an ordinary
`sk_live_…` for the connected account: the same string that business's own
engineers keep in production. What bounds it is the `read_only` scope and nothing
else, which is why `StripeClient` has no method that is not a GET. Stripe is also
the only provider here where read-only is a first-class grant.

**Gmail lists message ids and nothing else.** No subject, no sender, no date;
each needs a second request, so ten messages cost eleven calls. `MAX_RESULTS` is
10 for that reason: the bound is a request budget, not a page size. And a message
body must be **base64url**: standard base64 produces `+` and `/`, which Gmail
rejects with `400 Invalid value for ByteString`, and most short messages contain
neither character, so `b64encode` works in testing and fails on the first message
whose bytes happen to encode one.

## The scope decisions, and one that costs the user something

M11 requested `calendar.readonly` and M12 earned the write by building approvals
first. M14 keeps that rule, and it has a visible price.

**GitHub sees public repositories only.** Classic OAuth has no read-only scope for
private repositories, `repo` is read *and write* to code, issues, pull requests
webhooks and settings, across every repository the user can reach. Asking for it
so that a list of names could include private repos would mean everyone who
connected GitHub had handed this application the ability to force-push to their
employer's codebase. M14 asks for `read:user`. The listing is smaller than a user
expects, and that is the trade, stated rather than hidden.

**Gmail asks for `gmail.compose`, which is broader than `gmail.send`.** The agent
creates a draft and then sends that draft, because a failed send then leaves the
message in the user's own Drafts folder rather than losing the text. `compose` can
also read and delete drafts, and nothing here does either, so a user reading their
consent screen sees a wider permission than the feature strictly needs. It is the
right trade for a message that cannot be unsent, and it is a trade.

## The email agent

M12's deferred half, and it needed no new approval machinery, which is what
ADR-0015 claimed and had not demonstrated. `ApprovalService` gained one method
differing from the calendar one in two lines.

```
  compose ──► propose ──► END          run 1 → paused_for_approval
                  │
          [ approvals row ]            ← a human reads the actual message
                  │
             execute ──► END           run 2 → succeeded, mail sent
```

`resume_calendar_run` became `resume_approved_run` and dispatches on the stored
`kind`. Because a registry lookup now sits between the approval and the executor,
the invariant ADR-0015 described as "identical by construction" became worth
*checking*: the approval row's action must equal the run checkpoint's, or the run
refuses and nothing is sent. It cannot fire today: the two are written in one
transaction from the same dict, and the consequence if it ever did is a human
authorising one message while another goes out.

**One deliberate exception to M12's tracing rule.** The email body is not written
into `agent_steps`. That table is operational data, exported to whatever
observability stack a deployment runs; `approvals` is tenant-scoped and returned
only through checked endpoints. Being exact: the body is still in
`agent_runs.input` because the user typed it there, so this reduces copies from
three to one rather than hiding anything.

**The parser is deterministic and strict**, reading
`<address> about <subject> saying <body>` and refusing everything looser: the same
reason as M12's date parser, with sharper stakes. A half-understood calendar
instruction puts a meeting at the wrong time; a half-understood email instruction
sends the wrong words to a real person, under the user's name, with no unsend.

## Bugs this milestone found

Six. The first two are inherited from M11, and neither was visible to a test.

**1. Every Slack, Notion and GitHub integration would have died on first use.**
`needs_refresh()` treated a NULL `expires_at` as expired; those three providers
issue no expiry and no refresh token, so `get_fresh_token` marked the integration
`REVOKED` and told the user to reconnect, which produced an identical credential
and an identical result. Forever.

Invisible because `OfflineOAuthProvider` always issued a Google-shaped grant, so
the failing combination existed in no test. `perpetual=True` was written *before*
the fix; reverting the fix now fails seven tests, and the log reads:

```
integration.connected   provider=slack
integration.revoked     provider=slack reason=no_refresh_token
```

Two lines apart.

**2. A revocation did not survive the request that discovered it. Found with
curl.** `_mark_revoked` *flushed*, and every caller then raises `NotFoundError`
and `get_db` rolls the session back on any exception, which is the whole point of
session-per-request. So the failure that prompted the write discarded it. The API
said "access was revoked, reconnect it" while the row stayed `ACTIVE`, and every
later call rediscovered the same thing.

Every existing test missed it because the integration tests call the service
directly and assert inside the same transaction, where a flush *is* visible. Only
an HTTP round trip shows it. Fixed by committing: the ADR-0015 rule reached from a
third direction, and the regression test lives in `tests/e2e/` for that reason.

**3. `REVOKED` had one writer, on a path three providers never take.** M11 only
recorded a revocation when a *refresh* was rejected. A perpetual credential is
never refreshed, so a Slack token going bad would have been recorded nowhere:
502, row still `ACTIVE`, page still saying "connected", forever. `using()` makes
getting a token and noticing it died one operation.

**4. `Integration.scopes` was wrong for every comma-separating provider.** M11
split on whitespace because RFC 6749 says space-delimited and Google obeys it.
Slack and GitHub send commas, so the first Slack connection would have stored
`chat:write,channels:read` as a single 35-character scope. Nothing raises: it is
display and audit data, so the damage is a permissions list that is wrong in the
one place a human goes to find out what they granted.

**5. The shared base class carried Google Calendar's 403 message.** M11 hard-coded
"This Google account was connected without permission to change the calendar.
Reconnect it to grant write access" into `BaseClient.post_json`. Correct with one
provider; wrong the moment there were six: a 403 from Slack would have advised a
user to reconnect a calendar they may never have connected. Moved to an
overridable `forbidden_message`.

**6. The test suite was making real network requests, and nobody knew.** An e2e
test connected Slack and called `/slack/channels`; the offline server had issued a
token no real Slack would honour, so the request went out over the wire, took
252ms, came back 401, and failed for a reason unrelated to the code under test.
That is the mild version: the dangerous one is the same request *succeeding*
leaving a suite that is green on a good day, red during somebody else's incident,
and green again by the time anyone looks.

`_no_outbound_network` in `tests/conftest.py` now refuses any real outbound HTTP
and names the URL. It immediately caught a second one: an e2e test whose raw SQL
`UPDATE` was invisible to the shared identity map, so the request read a stale
`expires_at`, decided no refresh was needed, and went to googleapis.com. That test
passed or failed depending on what had run before it.

## Verified at runtime

Real uvicorn, real Postgres, curl, and a real restart.

```
1. /integrations/providers → six providers with their scopes; google_drive → 404
2. connect × 6            → gmail, google_calendar, slack, notion, github, stripe
                            all active, callback carrying no auth header at all
3. slack after connecting → status=active     (M11: revoked, on first use)
4. slack/channels         → 404 "Access to this slack account was revoked."
   slack after that       → status=revoked    (M11: still active, bug 2)
5. propose an email       → paused_for_approval, whole message in the action
6. an unparseable one     → succeeded, no approval, and a message showing the form
7. reject                 → run cancelled, "The action was rejected.", nothing sent
8. approve, no Gmail      → 404 naming the action; approval stayed approved
```

And across a genuinely restarted process:

```
propose → kill the server → process gone → start a new one
  → the approval is still pending, and the body is still intact
```

**In a real browser**, `make smoke`, 21 checks, 21 passing, including the four
that would regress silently: cookies httpOnly, tokens invisible to
`document.cookie`, the trace never leaking `checkpoint`, and no token string ever
rendered on the integrations page.

## Gate

```
ruff · ruff format · mypy --strict (240 files) · alembic check · make eval
830 tests, 2 skipped · 97.31% coverage (gate 97%)
frontend: lint · typecheck · build (10 routes) · smoke 21/21
```

`make eval` unchanged against M8's baseline, M14 touches no prompt and no
retrieval path.

## Known gaps, deliberately left

**No provider has ever been contacted.** There are no client ids or secrets in
this environment, so every flow runs against the in-memory authorization server.
The plumbing is verified; that each provider behaves as its documentation says is
not. If Slack changes its error envelope, these tests keep passing.

**No email has ever been sent.** The loop is driven end to end with a fake Gmail
client, and runtime verification stopped at the approval. The send path is
exercised; a message reaching a real inbox is not.

**Google Drive is not implemented and not offered.** Nothing here reads a file
from Drive, and an integration that connects and then has no endpoint to call is
worse than one that says it is unavailable. It stays in the `Provider` enum so
adding it is a deploy.

**Slack, Notion, GitHub and Stripe are read-only.** Posting to a channel is the
obvious next feature and it is not built, for the reason M11 gave about the
calendar: a write scope granted a milestone before anything uses it means every
connected workspace has already said yes to something nobody has reviewed. The
approval machinery is ready: a second action kind is a `kind` and an executor.

**Nothing paginates.** Every listing returns one page. A Slack workspace with
4,000 channels shows the first 100.

## Reproduce

```bash
make up
cd backend && uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000
uv run arq app.workers.settings.WorkerSettings   # in another shell
cd frontend && pnpm start                        # in a third
```

Then open http://localhost:3000/integrations, connect anything, and watch the
browser leave for `offline.agentflow.test`: a domain RFC 2606 reserves and no DNS
resolves, so a stray offline URL in a real deployment fails loudly instead of
quietly reaching somebody's server.
