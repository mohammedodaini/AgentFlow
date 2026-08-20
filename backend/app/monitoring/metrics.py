"""Metrics in Prometheus exposition format, hand-rolled and deliberately small.

Layer: monitoring. Answers the RED questions — Rate, Errors, Duration — plus the
two numbers this product spends real money on: tokens and cost per agent run.

**Why no `prometheus-client`.**
--------------------------------
It is the obvious dependency and it is the right one for most projects. Two things
argue against it here. The exposition format is a documented text protocol of
about fifteen lines to emit, and the library's real value — process collectors,
multiprocess mode, a global default registry — is value this deployment cannot
use: multiprocess mode needs a shared directory and a `gunicorn` worker model,
while this ships one uvicorn process per container and scales by adding
containers.

The library would also bring a global mutable registry, which is precisely the
shape `create_app()` exists to avoid: a module-level singleton means two apps in
one test process share counters, and a test asserting a count depends on which
tests ran before it. `MetricsRegistry` is built per application and lives on
`app.state`.

Stated as a trade rather than a win: this is a hundred lines nobody else
maintains, and it does not implement summaries, exemplars, or the OpenMetrics
format. If this project ever needs those, take the dependency.

**Histogram buckets are chosen, not defaulted.**
-------------------------------------------------
The default buckets in most libraries top out around ten seconds, which is
useless here: an agent run with a real model takes longer than that, and every
one of them would land in `+Inf` where a p95 cannot be computed. The buckets
below go to sixty seconds for that reason, and they are coarse at the bottom
because nobody is paged over the difference between 5ms and 8ms.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import Request

DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
"""Seconds. See the module docstring for why this goes to a minute."""

MAX_LABEL_CARDINALITY = 200
"""A ceiling on distinct label combinations, and the most important number here.

Unbounded label cardinality is *the* way a metrics endpoint takes down the thing
it monitors: a path label containing a UUID means one time series per request,
memory grows without limit, and the scrape eventually times out. Routes are
templated below (`/runs/{id}`, not `/runs/9f3a…`), so this ceiling should never be
approached — it is the backstop for the day a route slips through untemplated.
"""


@dataclass
class Histogram:
    """Cumulative bucket counts, plus sum and count."""

    buckets: dict[float, int] = field(default_factory=lambda: dict.fromkeys(DURATION_BUCKETS, 0))
    total: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        """Count one observation in the *smallest* bucket that contains it.

        Per-bucket here, cumulative at render time — and getting that division
        wrong is how this first shipped. The original incremented every bucket the
        value fell under, which is already cumulative, and `render` then
        accumulated a second time: with observations at 5ms, 30ms, 200ms and 3s,
        the `le="0.05"` bucket reported 3 instead of 2.

        Nothing raises. The histogram renders, the dashboard draws, and every
        quantile is wrong in the direction that makes the service look slower than
        it is — which is the kind of monitoring bug that gets a real regression
        dismissed as "the metrics are always weird".
        """
        self.total += value
        self.count += 1

        for bound in DURATION_BUCKETS:
            if value <= bound:
                self.buckets[bound] += 1
                return


class MetricsRegistry:
    """Every counter this process holds. One per application, not global.

    Guarded by a lock. The event loop is single-threaded, so contention is nil,
    but `BaseHTTPMiddleware` and arq workers can both touch this and a
    read-modify-write across an `await` is not atomic without one. The lock costs
    nanoseconds and removes a class of bug that only appears under load.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._durations: dict[tuple[str, str], Histogram] = defaultdict(Histogram)
        self._agent_runs: dict[tuple[str, str], int] = defaultdict(int)
        self._tokens: dict[str, int] = defaultdict(int)
        self._cost_usd: dict[str, float] = defaultdict(float)
        self._started = time.time()

    def observe_request(self, method: str, route: str, status: int, seconds: float) -> None:
        """Record one HTTP request."""
        with self._lock:
            if len(self._requests) >= MAX_LABEL_CARDINALITY:
                # Drop the label rather than the observation: an untemplated route
                # would otherwise grow one series per request forever. "other" is
                # a visible symptom somebody can investigate; an OOM is not.
                route = "other"

            self._requests[method, route, status] += 1
            self._durations[method, route].observe(seconds)

    def observe_agent_run(self, agent: str, status: str, *, tokens: int, cost_usd: float) -> None:
        """Record one finished agent run — the numbers this product pays for."""
        with self._lock:
            self._agent_runs[agent, status] += 1
            self._tokens[agent] += tokens
            self._cost_usd[agent] += cost_usd

    def render(self) -> str:
        """The Prometheus text exposition format."""
        with self._lock:
            lines: list[str] = [
                "# HELP agentflow_uptime_seconds Seconds since this process started.",
                "# TYPE agentflow_uptime_seconds gauge",
                f"agentflow_uptime_seconds {time.time() - self._started:.1f}",
                "# HELP agentflow_http_requests_total HTTP requests by method, route and status.",
                "# TYPE agentflow_http_requests_total counter",
            ]

            for (method, route, status), count in sorted(self._requests.items()):
                labels = f'method="{_escape(method)}",route="{_escape(route)}",status="{status}"'
                lines.append(f"agentflow_http_requests_total{{{labels}}} {count}")

            lines += [
                "# HELP agentflow_http_request_duration_seconds Request duration.",
                "# TYPE agentflow_http_request_duration_seconds histogram",
            ]

            for (method, route), histogram in sorted(self._durations.items()):
                labels = f'method="{_escape(method)}",route="{_escape(route)}"'
                cumulative = 0

                for bound in DURATION_BUCKETS:
                    # Prometheus histogram buckets are *cumulative*: each `le`
                    # counts everything at or below it. Emitting per-bucket counts
                    # instead produces a histogram that renders without error and
                    # whose every quantile is wrong.
                    cumulative += histogram.buckets[bound]
                    lines.append(
                        f"agentflow_http_request_duration_seconds_bucket"
                        f'{{{labels},le="{bound}"}} {cumulative}'
                    )

                lines.append(
                    f'agentflow_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} '
                    f"{histogram.count}"
                )
                lines.append(
                    f"agentflow_http_request_duration_seconds_sum{{{labels}}} {histogram.total:.6f}"
                )
                lines.append(
                    f"agentflow_http_request_duration_seconds_count{{{labels}}} {histogram.count}"
                )

            lines += [
                "# HELP agentflow_agent_runs_total Agent runs by agent and terminal status.",
                "# TYPE agentflow_agent_runs_total counter",
            ]
            for (agent, run_status), runs in sorted(self._agent_runs.items()):
                # `run_status`, not `status` — that name is bound to an `int` by
                # the HTTP loop above, and reusing it here made mypy report an
                # assignment error rather than the shadowing that caused it.
                lines.append(
                    f'agentflow_agent_runs_total{{agent="{_escape(agent)}",'
                    f'status="{_escape(run_status)}"}} {runs}'
                )

            lines += [
                "# HELP agentflow_agent_tokens_total Tokens consumed, by agent.",
                "# TYPE agentflow_agent_tokens_total counter",
            ]
            for agent, tokens in sorted(self._tokens.items()):
                lines.append(f'agentflow_agent_tokens_total{{agent="{_escape(agent)}"}} {tokens}')

            lines += [
                "# HELP agentflow_agent_cost_usd_total Spend, by agent.",
                "# TYPE agentflow_agent_cost_usd_total counter",
            ]
            for agent, cost in sorted(self._cost_usd.items()):
                lines.append(
                    f'agentflow_agent_cost_usd_total{{agent="{_escape(agent)}"}} {cost:.6f}'
                )

            return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    """Escape a label value per the exposition format.

    Backslash first, or escaping a quote would then have its own backslash
    escaped — turning `a"b` into `a\\\\"b` and producing a line Prometheus rejects.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def route_template(request: Request) -> str:
    """The route *pattern*, never the concrete path.

    `/api/v1/agent-runs/{run_id}`, not `/api/v1/agent-runs/9f3a…`. This single
    substitution is what keeps cardinality bounded — a UUID in a label is one time
    series per request, and a metrics endpoint that grows without limit takes down
    the process it was added to observe.

    **Built from the path and its parameters, not from `route.path`.** The obvious
    implementation reads the matched route's `path`, and it is wrong in this
    application: routers are nested (`_IncludedRouter`), so the matched route's
    `path` is relative to *its* router and comes out as `/agent-runs/{run_id}`
    with the `/api/v1` prefix missing. Two routers defining the same relative path
    — a `/health` under v1 and a v2 — would then silently share one time series,
    and every dashboard would show a route that does not exist.

    Substitution is **whole-segment**, which is the part that looks like
    over-engineering and is not. A plain `path.replace(value, "{name}")` on
    `/api/v1/items/1` replaces the `1` in `v1` as well, producing
    `/api/v{id}/items/{id}` — a label that is wrong, stable, and would never be
    questioned.
    """
    if request.scope.get("route") is None:
        # Matched no route (a 404). Bucketed together on purpose: a scanner
        # probing a thousand random paths must not create a thousand series.
        return "unmatched"

    params: dict[str, object] = request.scope.get("path_params") or {}

    if not params:
        return request.url.path

    templates = {str(value): "{" + name + "}" for name, value in params.items()}
    return "/".join(templates.get(segment, segment) for segment in request.url.path.split("/"))


def get_registry(request: Request) -> MetricsRegistry:
    """Read the registry `lifespan()` stored on the application."""
    registry: MetricsRegistry = request.app.state.metrics
    return registry
