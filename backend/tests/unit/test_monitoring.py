"""The metrics registry and the Sentry seam (M16).

The registry is hand-rolled (see `app/monitoring/metrics.py` for why), so the
exposition format is *this* codebase's responsibility rather than a library's —
which makes these tests the only thing standing between a subtly malformed
histogram and a dashboard whose every quantile is wrong.
"""

from __future__ import annotations

import pytest
import sentry_sdk

from app.core.config import Settings
from app.monitoring.metrics import MetricsRegistry, _escape
from app.monitoring.sentry import configure_sentry


def test_a_histogram_emits_cumulative_buckets() -> None:
    """**Prometheus histogram buckets are cumulative**: each `le` counts everything
    at or below it.

    Emitting per-bucket counts instead produces a histogram that renders without
    error and whose every quantile is wrong — the failure mode that looks like
    working monitoring.
    """
    registry = MetricsRegistry()

    for seconds in (0.005, 0.03, 0.2, 3.0):
        registry.observe_request("GET", "/x", 200, seconds)

    body = registry.render()
    buckets = {
        line.split('le="')[1].split('"')[0]: int(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if "duration_seconds_bucket" in line
    }

    assert buckets["0.01"] == 1
    assert buckets["0.05"] == 2
    assert buckets["0.25"] == 3
    assert buckets["+Inf"] == 4
    # Non-decreasing, which is what "cumulative" means and what a broken
    # implementation violates.
    ordered = [buckets[key] for key in ("0.01", "0.05", "0.25", "+Inf")]
    assert ordered == sorted(ordered)


def test_the_sum_and_count_agree_with_the_observations() -> None:
    registry = MetricsRegistry()

    for seconds in (0.1, 0.2, 0.3):
        registry.observe_request("GET", "/x", 200, seconds)

    body = registry.render()
    total = next(
        float(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if "duration_seconds_sum" in line
    )
    count = next(
        int(line.rsplit(" ", 1)[1])
        for line in body.splitlines()
        if "duration_seconds_count" in line
    )

    assert count == 3
    assert total == pytest.approx(0.6)


def test_buckets_reach_a_minute() -> None:
    """The default buckets in most libraries top out around ten seconds, which is
    useless here: an agent run with a real model takes longer than that, and every
    one would land in `+Inf` where a p95 cannot be computed."""
    registry = MetricsRegistry()
    registry.observe_request("POST", "/agent-runs", 200, 45.0)

    body = registry.render()

    assert 'le="60.0"} 1' in body


def test_cardinality_is_bounded() -> None:
    """**The line between a metrics endpoint and an outage.**

    An untemplated route would grow one series per request forever. Dropping the
    label keeps the observation and bounds the memory; "other" is a visible
    symptom somebody can investigate, and an OOM is not.
    """
    registry = MetricsRegistry()

    for index in range(500):
        registry.observe_request("GET", f"/{index}", 200, 0.01)

    series = [line for line in registry.render().splitlines() if "http_requests_total{" in line]

    assert len(series) < 250
    assert any('route="other"' in line for line in series)


def test_label_values_are_escaped() -> None:
    """A quote in a label value produces a line Prometheus rejects — and the
    backslash has to be escaped *first*, or escaping a quote would then have its
    own backslash escaped in turn."""
    assert _escape('a"b') == 'a\\"b'
    assert _escape("a\\b") == "a\\\\b"
    assert _escape('a\\"b') == 'a\\\\\\"b'


def test_agent_spend_is_counted() -> None:
    """The two numbers this product pays for. A dashboard of request rates that
    cannot show token spend is monitoring the cheap half."""
    registry = MetricsRegistry()
    registry.observe_agent_run("rag", "succeeded", tokens=1200, cost_usd=0.0034)
    registry.observe_agent_run("rag", "succeeded", tokens=800, cost_usd=0.0021)

    body = registry.render()

    assert 'agentflow_agent_tokens_total{agent="rag"} 2000' in body
    assert 'agentflow_agent_runs_total{agent="rag",status="succeeded"} 2' in body
    assert 'agentflow_agent_cost_usd_total{agent="rag"} 0.005500' in body


def test_an_empty_registry_still_renders() -> None:
    """A scrape of a process that has served nothing must be valid, or the first
    scrape after a deploy breaks the dashboard."""
    body = MetricsRegistry().render()

    assert body.endswith("\n")
    assert "agentflow_uptime_seconds" in body


# --- sentry --------------------------------------------------------------


def test_no_dsn_means_no_sentry() -> None:
    """A deployment with no Sentry account is valid. Failing over an absent error
    reporter would be an outage caused by the thing meant to observe outages."""
    assert configure_sentry(Settings(sentry_dsn="")) is False


def test_a_dsn_switches_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """`init` is patched, and it has to be.

    The first version of this test called the real one, which installed a
    **global** Sentry client for the rest of the pytest process — it then captured
    exceptions from unrelated tests and printed "Sentry is attempting to send 2
    pending events" on the way out, trying to reach an ingest host that does not
    exist. A unit test that mutates process-wide state is a test that changes the
    result of whatever runs after it.
    """
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: None)

    assert configure_sentry(Settings(sentry_dsn="https://public@o0.ingest.sentry.io/0")) is True


def test_pii_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The flag that matters most here.**

    `send_default_pii` controls whether cookies travel with an event — and
    ADR-0016 put this application's access and refresh tokens in httpOnly
    cookies. An event carrying cookies would ship live credentials to a third
    party on every unhandled exception.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    configure_sentry(Settings(sentry_dsn="https://public@o0.ingest.sentry.io/0"))

    assert captured["send_default_pii"] is False
    assert captured["traces_sample_rate"] == 0.0
