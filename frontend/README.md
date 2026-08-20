# Frontend — Next.js App Router

The UI for AgentFlow. Talks to the backend exclusively through the versioned
REST API (`/api/v1`), and **only from the server**.

```bash
pnpm install
cp .env.local.example .env.local     # BACKEND_URL
pnpm dev                             # needs `make dev` and `make worker` too
```

## The one thing to understand before changing anything

**The browser never holds a JWT.**

The backend issues an access token (30 minutes) and a refresh token (7 days,
rotating, revocable). The obvious thing to do with those in a SPA is
`localStorage.setItem` — and that is what this frontend is shaped to avoid. A
refresh token in `localStorage` is readable by any script that reaches the page,
and exfiltrating it is one line: one compromised dependency release and an
attacker has a week of access from anywhere.

So the tokens live in **httpOnly cookies** that JavaScript cannot read, and every
API call goes through the Next server (`src/lib/api.ts`, which imports
`server-only` and therefore cannot be imported by a Client Component). See
[ADR-0016](../docs/adr/0016-the-browser-never-holds-a-jwt.md).

Two things follow, and both are load-bearing:

- **Writes are Server Actions**, not client `fetch`. The password never enters a
  client bundle, and `revalidatePath` — which only exists server-side — is what
  keeps the UI correct after a write.
- **There is no CORS configuration anywhere**, because the browser only ever
  talks to its own origin.

## Layout

```
src/
├── app/
│   ├── login/ register/        unauthenticated
│   ├── (app)/                  route group — layout.tsx enforces the session
│   │   ├── chat/               conversations, and one transcript
│   │   ├── approvals/          the inbox: what the agent wants permission for
│   │   ├── documents/          upload + indexing status
│   │   └── runs/[id]/          the agent trace, shown to users (ADR-0012)
│   └── page.tsx                a fork: /chat or /login
├── components/                 four primitives, three client forms
└── lib/
    ├── config.ts               BACKEND_URL — server-only, never NEXT_PUBLIC_
    ├── session.ts              the cookies, and why they are shaped that way
    ├── api.ts                  the only code that attaches a credential
    ├── actions.ts              every write the UI can perform
    └── types.ts                the shapes the backend returns
```

Client Components are the exception, not the rule: `auth-form`, `composer`,
`decide-form`, `propose-form`, `upload-form`. Each is client-side for the same
two reasons — a pending state, and showing an error without discarding what the
user typed. The pending state is not decoration: an agent turn takes seconds, and
a form that looks idle gets submitted twice.

## Checks

```bash
pnpm lint            # eslint
pnpm typecheck       # tsc --noEmit, strict
pnpm build           # the real production build
pnpm smoke           # a real browser against the real stack — see below
```

`make web-check` runs the first three; CI runs them on every push.

`pnpm smoke` is different in kind. It drives Chromium through register → ask →
read the trace → propose a calendar change → reject it → upload → sign out, and
asserts three things nothing else can: that the session cookies are httpOnly,
that `document.cookie` cannot see them, and that the run trace never contains
`checkpoint`. All three would regress silently — the page would look identical.

It needs the whole system up, which is why it is not in CI:

```bash
make up && make dev        # terminal 1 — API
make worker                # terminal 2 — ingestion + memory extraction
cd frontend && pnpm start  # terminal 3
make smoke                 # terminal 4
```

## Not shadcn/ui

The placeholder that used to live here named shadcn/ui, written before there was
a UI to look at. shadcn vendors ~20 component files and pulls in Radix; this app
needs a button, a field, a card and two banners. `src/components/ui.tsx` is those,
in 90 lines, as Server Components with no client bundle. Reach for shadcn when
something here needs a dialog or a combobox — not before.
