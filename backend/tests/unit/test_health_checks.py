"""Readiness probe behaviour (M2).

The probe's whole job is turning "anything at all went wrong" into a boolean
without ever raising. That is hard to trust from an integration test — you
cannot easily make a real database hang on demand — so these tests drive it
with stubs that fail in the specific ways production fails.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Self, cast

import pytest

from app.monitoring import health
from app.monitoring.health import (
    PROBE_TIMEOUT_SECONDS,
    check_database,
    check_readiness,
    check_redis,
)

Behaviour = Callable[[], Coroutine[Any, Any, None]]


class _StubSession:
    """The slice of AsyncSession the probe actually touches: a context manager
    with an `execute()`. Stubbed rather than mocked so the failure modes are
    written out in plain code."""

    def __init__(self, on_execute: Behaviour) -> None:
        self._on_execute = on_execute

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    async def execute(self, _statement: object) -> None:
        await self._on_execute()


def _factory(on_execute: Behaviour) -> Any:
    """Stand in for `async_sessionmaker`: called with no args, returns a session."""
    return lambda: _StubSession(on_execute)


async def _succeed() -> None:
    return None


async def _refuse_connection() -> None:
    raise ConnectionRefusedError("connection refused")


async def _hang() -> None:
    await asyncio.sleep(60)


async def test_reports_true_when_the_query_succeeds() -> None:
    assert await check_database(_factory(_succeed)) is True


async def test_reports_false_instead_of_raising() -> None:
    """A probe that raises becomes a 500, which an orchestrator cannot act on."""
    assert await check_database(_factory(_refuse_connection)) is False


def test_the_probe_timeout_is_short_enough_to_be_useful() -> None:
    """Must fit inside any sane orchestrator probe deadline (usually 5-10s)."""
    assert 0 < PROBE_TIMEOUT_SECONDS <= 5.0


async def test_a_hanging_database_times_out_rather_than_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure mode that matters most: unreachable host, no TCP reset.

    Remove the timeout and this test hangs for a full minute — which is exactly
    what the orchestrator's probe would do in production.

    The real timeout is patched down so the suite stays fast; its actual value
    is asserted separately above. A test suite that takes two seconds per probe
    is one nobody runs on every save.
    """
    monkeypatch.setattr(health, "PROBE_TIMEOUT_SECONDS", 0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await check_database(_factory(_hang))
    elapsed = loop.time() - started

    assert result is False
    assert elapsed < 1.0


class _StubRedis:
    """Just the `ping()` the probe calls."""

    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def ping(self) -> bool:
        if not self._reachable:
            raise ConnectionRefusedError("connection refused")
        return True


async def test_redis_probe_reports_reachability() -> None:
    assert await check_redis(cast(Any, _StubRedis(reachable=True))) is True
    assert await check_redis(cast(Any, _StubRedis(reachable=False))) is False


async def test_readiness_names_each_dependency() -> None:
    """`{"database": false}` tells whoever is paged where to look; `false` does not."""
    healthy_redis = cast(Any, _StubRedis(reachable=True))

    assert await check_readiness(_factory(_succeed), healthy_redis) == {
        "database": True,
        "redis": True,
    }
    assert await check_readiness(_factory(_refuse_connection), healthy_redis) == {
        "database": False,
        "redis": True,
    }


async def test_readiness_reports_a_redis_outage_separately() -> None:
    """Two dependencies, two independent answers — that is the point of the dict."""
    checks = await check_readiness(_factory(_succeed), cast(Any, _StubRedis(reachable=False)))

    assert checks == {"database": True, "redis": False}


@pytest.mark.parametrize("exception_type", [ValueError, RuntimeError, OSError])
async def test_any_exception_type_is_absorbed(exception_type: type[Exception]) -> None:
    """Driver failures are not one class — asyncpg, DNS and TLS errors all differ."""

    async def _raise() -> None:
        raise exception_type("boom")

    assert await check_database(_factory(_raise)) is False
