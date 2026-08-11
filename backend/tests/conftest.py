"""Shared pytest fixtures for the whole suite.

Only fixtures that more than one test module needs belong here. A fixture used
by a single file belongs in that file — a bloated conftest is how test suites
become impossible to reason about.

`_test_env` is autouse because `get_settings()` is `lru_cache`d: without
clearing that cache between tests, the first test to call it would freeze
configuration for every test that follows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force `APP_ENV=test` and give every test a clean settings cache."""
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app() -> FastAPI:
    """A freshly built application.

    Built per-test through the factory rather than imported as a module-level
    singleton — that is the entire reason `create_app()` exists.
    """
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP client speaking to the ASGI app in-process (no network, no server)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
