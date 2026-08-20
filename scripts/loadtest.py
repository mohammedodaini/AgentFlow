"""Measure latency and error rate against a running API (M16).

    make loadtest                      # against http://localhost:8000
    python scripts/loadtest.py --url http://localhost:8001 --concurrency 32

**What this is for, and what it is not.** It answers "does this fall over, and
where does the time go?" for one process on one machine. It is not a capacity
model: there is no think time, no connection churn, no realistic mix of tenants,
and the client shares a laptop with the server it is measuring. Every number it
prints is therefore a *lower bound on latency and an upper bound on throughput*,
which is the honest way round — a load test that flatters the system is worse
than none.

**Percentiles, never a mean.** A mean latency hides exactly the requests people
notice. A p99 of two seconds with a mean of 40ms is a system where one user in a
hundred waits two seconds, and the mean says everything is fine.

**Errors are counted separately from slowness**, and a 429 is counted apart from
both. Under load the rate limiter is *supposed* to refuse traffic; folding those
into an error rate would make a working limiter look like a failing service, and
folding them into latency would make the system look faster the more it refused.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

DEFAULT_URL = "http://localhost:8000"

SERVER_ERROR = 500
RATE_LIMITED = 429

REFUSAL_THRESHOLD = 0.1
"""Above this share of 429s, a scenario is measuring the limiter rather than the
endpoint, and the table says so instead of quietly reporting it as latency."""


@dataclass
class Results:
    """What one scenario produced."""

    name: str
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter[int] = field(default_factory=Counter)
    failures: int = 0

    @property
    def rate_limited(self) -> int:
        return self.statuses[RATE_LIMITED]

    @property
    def errors(self) -> int:
        """5xx plus transport failures. 4xx is the client being told no, which is
        the service working."""
        return self.failures + sum(
            count for code, count in self.statuses.items() if code >= SERVER_ERROR
        )

    def percentile(self, fraction: float) -> float:
        if not self.latencies_ms:
            return 0.0

        ordered = sorted(self.latencies_ms)
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return ordered[index]


async def hammer(
    client: httpx.AsyncClient,
    results: Results,
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    payload: dict[str, object] | None,
    requests: int,
) -> None:
    """One worker's share of a scenario."""
    for _ in range(requests):
        started = time.perf_counter()

        try:
            response = await client.request(method, path, headers=headers, json=payload)
        except httpx.HTTPError:
            results.failures += 1
            continue

        results.latencies_ms.append((time.perf_counter() - started) * 1000)
        results.statuses[response.status_code] += 1


async def scenario(
    base_url: str,
    name: str,
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    payload: dict[str, object] | None,
    concurrency: int,
    total: int,
) -> Results:
    results = Results(name=name)
    per_worker = max(total // concurrency, 1)

    # One client shared by every worker, so connections are pooled the way a real
    # client's would be. A client per worker measures TLS handshakes instead.
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        started = time.perf_counter()
        await asyncio.gather(
            *(
                hammer(
                    client,
                    results,
                    method=method,
                    path=path,
                    headers=headers,
                    payload=payload,
                    requests=per_worker,
                )
                for _ in range(concurrency)
            )
        )
        elapsed = time.perf_counter() - started

    done = len(results.latencies_ms) + results.failures
    print(
        f"  {name:<26} {done:>5} reqs  {done / elapsed:>7.1f} rps  "
        f"p50 {results.percentile(0.50):>7.1f}ms  "
        f"p95 {results.percentile(0.95):>7.1f}ms  "
        f"p99 {results.percentile(0.99):>7.1f}ms  "
        f"errors {results.errors}  429s {results.rate_limited}"
    )

    # **The trap this tool sets for itself, caught out loud.** The first run of
    # this script against the container stack refused 341 of 400 requests and
    # reported the agent scenario at "1335 rps, p50 4.6ms" — which was 48 rate
    # limit responses being served very quickly.
    #
    # Those numbers are not slightly optimistic; they measure a completely
    # different thing, and they look like the best result in the file. So the tool
    # says so rather than leaving it to whoever reads the table to notice the last
    # column.
    if results.rate_limited > done * REFUSAL_THRESHOLD:
        print(
            f"  {'':26} ^ {results.rate_limited}/{done} were refused by the rate "
            "limiter. These are NOT latency figures for this endpoint — re-run "
            "with RATE_LIMIT_PER_MINUTE raised, or RATE_LIMIT_ENABLED=false."
        )

    return results


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--requests", type=int, default=400)
    arguments = parser.parse_args()

    async with httpx.AsyncClient(base_url=arguments.url, timeout=30.0) as client:
        probe = await client.get("/api/v1/health/ready")

        if probe.status_code != httpx.codes.OK:
            print(f"{arguments.url} is not ready: {probe.status_code} {probe.text[:120]}")
            return 1

        # A real account, because an unauthenticated load test measures the auth
        # middleware and nothing else — and every interesting path in this API is
        # behind it.
        email = f"loadtest-{int(time.time())}@agentflow.dev"
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "correct-horse-battery-staple", "full_name": "Load"},
        )
        token = registered.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        organizations = await client.get("/api/v1/organizations", headers=auth)
        auth["X-Organization-Id"] = organizations.json()[0]["organization"]["id"]

    print(f"\n{arguments.url}  ·  concurrency {arguments.concurrency}\n")

    await scenario(
        arguments.url,
        "health (no auth, no db)",
        method="GET",
        path="/api/v1/health/live",
        headers={},
        payload=None,
        concurrency=arguments.concurrency,
        total=arguments.requests,
    )
    await scenario(
        arguments.url,
        "readiness (probes both)",
        method="GET",
        path="/api/v1/health/ready",
        headers={},
        payload=None,
        concurrency=arguments.concurrency,
        total=arguments.requests,
    )
    await scenario(
        arguments.url,
        "list runs (auth + query)",
        method="GET",
        path="/api/v1/agent-runs?limit=20",
        headers=auth,
        payload=None,
        concurrency=arguments.concurrency,
        total=arguments.requests,
    )
    agent = await scenario(
        arguments.url,
        "supervised agent run",
        method="POST",
        path="/api/v1/agent-runs/supervised",
        headers=auth,
        payload={"instruction": "How are expenses reimbursed?"},
        concurrency=min(arguments.concurrency, 8),
        total=arguments.requests // 8,
    )

    print()

    if agent.errors:
        print(f"FAIL — {agent.errors} server errors under load")
        return 1

    print("no server errors under load")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
