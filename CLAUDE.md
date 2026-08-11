# CLAUDE.md — AgentFlow AI session memory

This file is Claude's persistent memory for this project. **Claude: read this
fully at session start; update the "Current position" and "Progress log"
sections whenever a milestone step completes.** The human should not have to
re-explain context, ever.

## The mentorship contract (never violate)

- Claude is a Senior Staff AI Engineer **mentor**, not a code generator. The
  user is an AI student (solid Python) becoming an agentic-AI engineer.
- Build ONE feature at a time. Per milestone, in order:
  **explain concept → build together → quiz → exercise → review the user's
  attempt → refactor together → only then continue.**
- Never skip ahead. Never dump monolithic code. Always explain WHY,
  industry best practices, and tradeoffs. Refuse to jump ahead if
  foundational pieces are missing.
- When creating any file: explain why it exists, how imports/DI/data flow
  work, and why it's structured that way.

## Decisions already made (don't relitigate; new ADR to change)

Stack: Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic ·
PostgreSQL + pgvector · Redis · **arq** (not Celery) · LangGraph · uv ·
ruff + mypy(strict) · pytest · Next.js frontend (scaffolded at M13).

Key calls (rationale in docs/): modular monolith; pgvector over dedicated
vector DB; JWT access+refresh; REST not GraphQL; multi-tenancy via
organizations/memberships from day one; single agent before multi-agent;
human approval = DB record + LangGraph interrupt.

Design docs: docs/folder-guide.md · architecture.md · database.md ·
agents.md · packages.md · roadmap.md (16 milestones) · adr/.

## Current position

- **Phase:** architecture designed and scaffolded (2026-07-10). Design NOT
  yet formally approved by the user.
- **Next:** Milestone 1 — skeleton that runs (config, structlog, /health,
  CI green). Do not start M1 coding until the pending items below are done.

### Pending from last session
1. Quiz (unanswered — re-ask before starting M1):
   - Q1: Why separate `schemas/` from `models/`? What breaks if a route
     returns an ORM object?
   - Q2: Trace "summarize this 40-page PDF" through the layers; why `202`?
   - Q3: Why is `memberships` a table with a `role` column instead of a
     `users.organization_id` column?
2. Exercise: read the six docs; `git init` + first commit; try `make up`.
3. Open invitation: challenge arq-vs-Celery or pgvector → ADR-0002 if so.

## Progress log

| Date | What happened |
|---|---|
| 2026-07-10 | Full scaffold created: folder tree, configs (pyproject, compose, Makefile, CI, pre-commit, Dockerfile, .env.example), all design docs. Quiz issued, unanswered. Repo not yet under git. |
| 2026-07-12 | User requested named stubs: all ~110 backend modules created with docstrings (purpose/layer/rules), real imports, and milestone-tagged TODOs (M1–M16). No implementations — bodies are TODO comments; each stub carries `# ruff: noqa: F401`, removed when implemented. Quiz still unanswered; repo still not under git. |

## Quiz & exercise history

| Milestone | Quiz result | Exercise result | Notes |
|---|---|---|---|
| Architecture | pending | pending | re-ask at next session start |
