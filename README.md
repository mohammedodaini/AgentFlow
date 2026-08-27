# AgentFlow AI

An AI employee for small businesses: connects to Gmail, Calendar, Drive, Slack,
Notion, GitHub, and Stripe; answers questions over your company documents (RAG);
drafts emails, schedules meetings, generates proposals  with human approval
before any dangerous action.

> **Status:** working stack. Authentication with account lockout, scheduled
> backups with a rehearsed restore, TLS termination, and a documented rollback
> path are in place, behind a CI gate that runs lint, formatting, type checks,
> a migrations-match-the-models check, the test suite, and an evaluation gate.
> See [docs/](docs/) for the design and [docs/operations.md](docs/operations.md)
> for running it.

## Documentation map

| Doc | What it covers |
|---|---|
| [docs/folder-guide.md](docs/folder-guide.md) | Every folder: why it exists, what belongs there |
| [docs/architecture.md](docs/architecture.md) | System layers and request/data flow |
| [docs/database.md](docs/database.md) | All tables and relationships |
| [docs/agents.md](docs/agents.md) | Multi-agent AI architecture |
| [docs/packages.md](docs/packages.md) | Every dependency, why, and alternatives |
| [docs/roadmap.md](docs/roadmap.md) | Milestone-by-milestone learning roadmap |
| [docs/operations.md](docs/operations.md) | Deploy, roll back, restore, and triage a live stack |
| [docs/adr/](docs/adr/) | Architecture Decision Records |

## Quickstart (once Milestone 1 lands)

```bash
make up        # start Postgres + Redis in Docker
make install   # install backend deps (requires uv)
make migrate   # apply DB migrations
make dev       # run the API at http://localhost:8000
make check     # lint + typecheck + tests (same as CI)
```

## Stack

Python 3.13 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) · Alembic ·
PostgreSQL + pgvector · Redis · arq · LangGraph · Docker · GitHub Actions
