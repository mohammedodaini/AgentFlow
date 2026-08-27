# M13: The frontend: the first milestone a person can see

- **Date:** 2026-08-20
- **Status:** shipped
- **ADRs:** [ADR-0016](../adr/0016-the-browser-never-holds-a-jwt.md)

Twelve milestones of API. This is the one where somebody can register, ask a
question about their own documents, read the answer with its citation, and tell
the agent not to put that meeting in their calendar.

## What was built

| Piece | Path | What it does |
|---|---|---|
| Scaffold | `frontend/` | Next 16 App Router · React 19 · TS strict · Tailwind 4 |
| Session | `src/lib/session.ts` | Three httpOnly cookies; the browser holds no JWT |
| API client | `src/lib/api.ts` | Server-only; attaches the credential, refreshes once |
| Writes | `src/lib/actions.ts` | Every mutation, as Server Actions |
| Auth | `src/app/{login,register}` | Sign in, sign up, sign out |
| Chat | `src/app/(app)/chat` | Conversation list, transcript, composer |
| Approvals | `src/app/(app)/approvals` | The inbox: propose, approve, reject |
| Documents | `src/app/(app)/documents` | Upload + indexing status |
| Trace | `src/app/(app)/runs/[id]` | ADR-0012's argument, rendered |
| Smoke test | `frontend/tests/smoke.mjs` | A real browser against the real stack |

## The idea

**The browser never holds a JWT** (ADR-0016). The refresh token is a seven-day
offline credential; in `localStorage` one compromised dependency exfiltrates it
and the attacker has a week of access from their own machine. In an httpOnly
cookie the same XSS is bounded to that page while it is open: the difference
between an incident and a breach.

Two things follow, and both are load-bearing rather than stylistic. Writes are
Server Actions, so the password never enters a client bundle and `revalidatePath`
keeps the UI honest after a write. And there is **no CORS configuration anywhere**,
because the browser only ever talks to its own origin.

## Bugs this milestone found

Three, and two of them predate it.

**1. CI has never run on a push.** `.github/workflows/ci.yml` triggers on
`branches: [main]`; this repository's branch is `master`. From M1 to M13 the
workflow only ever fired on pull requests, and this project has not opened one
so the gate that `make check` enforces locally has never once run in CI. A CI
file that never runs is worse than no CI file, because it looks like a gate.
Found by reading the workflow while adding a frontend job to it.

**2. `.env.example` had drifted five milestones behind.** It calls itself "the
committed contract of every variable the app needs" and was missing 23 of 45
settings, including `TOKEN_ENCRYPTION_KEY` and `OAUTH_PROVIDER`, the two whose
absence stops a production deploy. It also documented `GOOGLE_REDIRECT_URI`,
which no setting reads: somebody would have set it, watched it be ignored, and
had no way to discover the real name is `OAUTH_REDIRECT_BASE_URL`. Fixed in the
commit before this one.

**3. A trace link pointing at a route that does not exist.** The transcript
linked to `/chat/{run_id}/trace`, nesting a run under a conversation id. Caught
by writing the page it pointed at and noticing there was nowhere for it to go.
Now `/runs/{id}`.

## Verified at runtime

`frontend/tests/smoke.mjs` drives Chromium against the real backend, the real arq
worker and real Postgres. Thirteen checks, all passing:

```
✓ register redirects to /chat
✓ session cookies exist
✓ tokens are httpOnly
✓ tokens invisible to document.cookie
✓ the question is shown
✓ an answer came back
✓ a trace link is offered
✓ trace shows the graph nodes
✓ checkpoint is never published
✓ the proposal appears in the inbox
✓ rejecting clears it from the inbox
✓ the upload appears
✓ signed out cannot reach /chat
```

Three of those are security assertions that would regress *silently*: the page
would look identical either way. The answer the browser actually received was
*"Expenses are reimbursed monthly, provided a receipt is attached. [1]"*, cited,
from a document uploaded through the UI moments earlier.

## Gate

```
frontend: pnpm lint · pnpm typecheck · pnpm build   (9 routes)
backend:  unchanged, 713 tests, 97.14%
smoke:    13/13 in a real browser
```

CI now has a `frontend` job, and, for the first time, actually runs on push.

## Known gaps, deliberately left

**No frontend unit tests.** The smoke test covers the paths that matter and the
components are thin: four primitives and five forms, none holding logic worth
isolating. When a component grows a branch worth testing, add the runner then
adding Vitest now would be a test harness with nothing to test.

**No streaming.** `POST /ask/stream` exists (M7) and the chat UI does not use it;
a turn shows "Thinking…" and then the whole answer. Streaming a conversation turn
means writing the message from the stream's completion callback, which M12
already declined to do for the same reason: a stream that fails halfway has shown
the client something it cannot commit.

**No organization switcher.** The first membership is adopted at sign-in and
stored. Everything is tenant-scoped underneath, so this is a UI affordance rather
than a missing capability.

**No optimistic UI.** Sending a message waits for the round trip. `useOptimistic`
would make it feel faster and would need a rollback path for the failure cases
worth doing when somebody complains, not before.

**Accessibility is basic, not audited.** Labels are associated, errors use
`role="alert"`, focus order follows the DOM. Nobody has run a screen reader
through it, and the milestone does not claim they have.

## Reproduce

```bash
make up                       # Postgres + Redis
make dev                      # API, terminal 1
make worker                   # arq, terminal 2
cd frontend && pnpm install && cp .env.local.example .env.local
pnpm dev                      # frontend, terminal 3
```

Then http://localhost:3000, register, upload a document, wait for `ready`, and
ask it something.
