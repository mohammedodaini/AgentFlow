# =============================================================================
# Production image for the API. Multi-stage: build deps in one layer, ship a
# slim runtime. We will wire this into deployment in a later milestone; it
# exists now so the target shape of "production" is visible from day one.
# =============================================================================
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# **The lockfile is copied, and M16 found out it was not.** This stage used to
# copy `pyproject.toml` alone, which meant `uv sync` re-resolved from scratch on
# every build — and this project's specifiers have no upper bounds (`mypy>=1.13`,
# and every other line in that file), so two builds of the same commit, a week
# apart, could ship different major versions of anything.
#
# That is not a theoretical risk here: `uv lock` silently moved mypy from 1.x to
# 2.3 during M6, which is recorded in CLAUDE.md. The same thing happening to
# `anthropic` or `sqlalchemy` between a staging build and a production build is a
# release that cannot be reproduced and a bug that cannot be bisected.
#
# `--frozen` is the half that makes it enforcement rather than intent: it refuses
# to update the lockfile, so a `pyproject.toml` edited without re-locking fails
# the build instead of quietly resolving something new.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.14-slim

# Never run containers as root.
RUN useradd --create-home appuser

# **The storage mountpoint is created here, owned by appuser, and M16 found out
# why by watching every upload return 500.**
#
# Docker seeds a fresh named volume from whatever is at the mountpoint *in the
# image* — including its ownership. With no directory there, it creates one owned
# by root, and a container running as uid 1000 cannot write to it. The symptom is
# `StorageError: Could not store object: Permission denied` on POST /documents,
# and nothing else in the stack is unhealthy: Postgres is fine, Redis is fine, the
# API answers every other route.
#
# It is invisible in development, because `make dev` runs as the developer and
# writes to a directory they own. It appears the first time the product is run the
# way it will actually be deployed — which is the argument for having done that
# before calling a deploy done.
RUN mkdir -p /var/lib/agentflow/storage && chown -R appuser:appuser /var/lib/agentflow

USER appuser
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

# No `--reload`, and no `--workers`. Reload watches the filesystem and doubles
# the process tree for nothing in a container. Workers are the orchestrator's
# job: one process per container scales by adding containers, and it is also what
# lets `MetricsRegistry` be a plain in-process object rather than needing
# Prometheus multiprocess mode and a shared directory.
# `--no-server-header`, and M16 found out it was needed by looking at a real
# response. `SecurityHeadersMiddleware` sets `Server: agentflow`, and uvicorn
# writes its *own* `Server: uvicorn` at the protocol layer, after the application
# response — so both were sent, and the banner the middleware existed to suppress
# was still there. Middleware cannot remove it; only this flag can.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
