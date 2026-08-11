# Frontend (placeholder)

This directory is intentionally empty for now.

**Why we don't hand-write frontend boilerplate:** the industry standard is to
scaffold with the framework's official generator when we reach the frontend
milestone:

```bash
npx create-next-app@latest . --typescript --tailwind --eslint --app
```

That command generates `package.json`, `tsconfig.json`, ESLint/Prettier
config, and the app skeleton with correct, current versions — hand-writing
those files invites version drift and subtle misconfiguration.

Planned stack: **Next.js (App Router) · TypeScript · Tailwind CSS · shadcn/ui**,
talking to the backend exclusively through the versioned REST API (`/api/v1`).
