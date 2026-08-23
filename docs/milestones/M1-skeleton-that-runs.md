# M1: Skeleton that runs

**Status:** complete (2026-08-11) · **Gate:** `make check` green · **Tests:** 12 passing

The goal of M1 is not features. It is a process that starts, configures itself
from the environment, logs in a machine-readable way, answers one HTTP request,
and proves all of that with tests that run in CI.

## What was built

| Module | Responsibility |
|---|---|
| [`app/core/config.py`](../../backend/app/core/config.py) | `Settings` (pydantic-settings) + cached `get_settings()` |
| [`app/core/exceptions.py`](../../backend/app/core/exceptions.py) | `AppError` base: message + machine-readable code |
| [`app/logging/processors.py`](../../backend/app/logging/processors.py) | `add_request_id` structlog processor |
| [`app/logging/config.py`](../../backend/app/logging/config.py) | `configure_logging()`: console in dev, JSON in prod |
| [`app/middleware/request_id.py`](../../backend/app/middleware/request_id.py) | `RequestIDMiddleware` + `request_id_var` ContextVar |
| [`app/middleware/timing.py`](../../backend/app/middleware/timing.py) | `TimingMiddleware`: one structured line per request |
| [`app/api/v1/routes/health.py`](../../backend/app/api/v1/routes/health.py) | `GET /api/v1/health/live` |
| [`app/api/v1/router.py`](../../backend/app/api/v1/router.py) | v1 aggregate router |
| [`app/main.py`](../../backend/app/main.py) | `create_app()` factory + `lifespan` |

## The four decisions worth understanding

**1. An app *factory*, not a module-level `app = FastAPI()`.**
A module-level app is constructed at import time using whatever environment
happened to exist, and every test then shares one mutated instance. Tests that
pass alone and fail together are almost always this bug. `create_app()` gives
each test a clean application, and makes "an app configured differently" an
argument rather than a monkeypatch. `app = create_app()` still exists at the
bottom of `main.py` purely as uvicorn's target.

**2. Settings are cached, and validated once.**
`@lru_cache` on `get_settings()` means injecting settings into a route costs a
dict lookup, not a re-parse of the environment. `APP_ENV` is a `Literal` of
exactly three values, so `APP_ENV=prod` fails at startup with a clear error
instead of silently taking a development branch in production. Tests call
`get_settings.cache_clear()` between cases: that is why `conftest.py` has an
autouse fixture.

**3. The request ID is a ContextVar, and it is the outermost middleware.**
Starlette wraps outward: the *last* middleware registered is the *outermost*.
`RequestIDMiddleware` is registered last so it runs first, setting the
contextvar before `TimingMiddleware` logs. A ContextVar rather than a global
because each concurrent request needs its own value under asyncio. An inbound
`X-Request-ID` is honoured so a trace started by a proxy or the frontend
survives the hop; otherwise a uuid4 is minted. The value is reset in a
`finally` block, skip that and the ID leaks into whatever task reuses the
context.

**4. Liveness touches nothing.**
`/health/live` reports only that the process is alive. If it checked the
database, a brief database blip would look like a dead application and an
orchestrator would restart every container you have. Readiness, which *does*
probe dependencies and returns 503, is a separate endpoint arriving in M2.

## Verified at runtime, not just in tests

```
$ curl -D- -H "X-Request-ID: manual-trace-1" localhost:8000/api/v1/health/live
HTTP/1.1 200 OK
x-request-id: manual-trace-1
{"status":"ok"}
```

```
info  app.startup    app_name=agentflow env=development
info  http.request   duration_ms=1.6 method=GET path=/api/v1/health/live
                     request_id=manual-trace-1 status_code=200
```

The trace ID supplied by the caller appears on the response header *and* on the
log line. That is the whole point of M1: one request is greppable end to end.

## Also changed

- **`pyproject.toml`**: added `[tool.ruff.lint.isort] known-first-party`, so
  `app`/`tests` imports group separately from third-party instead of being
  sorted in among `fastapi` and `httpx`.
- **74 stub modules**: marked `# mypy: ignore-errors` so the gate can pass
  before all 16 milestones exist. See [ADR-0002](../adr/0002-unimplemented-stubs-are-excluded-from-mypy.md).
- **Toolchain**: `uv` installed; the venv runs **Python 3.13.2** (the system
  interpreter is 3.12 and cannot run this project).

## Known gaps, deliberately left for later

- `GET /health/ready` and `app/monitoring/health.py`, M2, once there is a
  database to probe.
- Engine/pool creation in `lifespan()` is a TODO, M2.
- CORS and rate-limit middleware exist as stubs: they arrive with the
  frontend (M13) and hardening (M16).

## Test coverage

12 tests: 8 unit (settings contract, log-processor branches) and 4 e2e (status
and body, request-ID echo, request-ID generation, 404 guard). Overall line
coverage reads 21% because the ~140 unimplemented stubs count as uncovered
lines; among implemented modules it is effectively complete. A real coverage
gate arrives in M4, when the number will mean something.

## Reproduce

```bash
make install   # uv sync --all-extras
make check     # ruff + ruff format + mypy + pytest
make dev       # http://localhost:8000/api/v1/health/live
```
