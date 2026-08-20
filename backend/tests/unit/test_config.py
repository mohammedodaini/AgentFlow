"""Settings behaviour (M1).

Configuration is the first thing that runs and the first thing to break a
deploy. These tests pin the contract documented in `.env.example`.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_defaults_are_development_friendly() -> None:
    """A bare environment yields a runnable local config, not a crash."""
    settings = Settings()

    assert settings.app_name == "agentflow"
    assert settings.log_level == "INFO"
    assert "postgresql+asyncpg://" in settings.database_url
    assert settings.redis_url.startswith("redis://")


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env vars win over defaults — the core of 12-factor config."""
    monkeypatch.setenv("APP_NAME", "custom-name")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.app_name == "custom-name"
    assert settings.log_level == "DEBUG"


def enter_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set `APP_ENV=production` and satisfy every guard that comes with it.

    The list grows with the milestones — a real signing key at M3, a real
    embedding provider at M6, a real model at M7, a real OAuth provider and
    encryption key at M11 — and each addition breaks every older test that merely
    set `APP_ENV`. Collecting them here means the next guard is one edit rather
    than a hunt through the suite for tests that claim to be production and are no
    longer allowed to be. It has now paid for itself four times.

    Tests *about* a specific guard do not use this; they set the one variable they
    are asserting on, so the failure they trigger is unambiguous.
    """
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "f" * 64)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("OAUTH_PROVIDER", "live")
    # A generated key, distinct from both the published placeholder and
    # SECRET_KEY — production refuses all three of "placeholder", "missing" and
    # "same as the signing key", and this helper has to clear every one.
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # M16. `/metrics` publishes traffic rates, error counts, latency and spend,
    # and production refuses to serve that unauthenticated.
    monkeypatch.setenv("METRICS_TOKEN", "metrics-token-not-a-real-one")


def test_app_env_maps_to_env_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """`APP_ENV` is the documented variable name; `settings.env` is the field."""
    enter_production(monkeypatch)

    assert Settings().env == "production"


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail at startup on a typo'd environment, never silently at request time."""
    monkeypatch.setenv("APP_ENV", "prod")  # not one of the three valid values

    with pytest.raises(ValidationError):
        Settings()


def test_is_development_tracks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log rendering branches on this, so it gets its own test."""
    monkeypatch.setenv("APP_ENV", "development")
    assert Settings().is_development is True

    enter_production(monkeypatch)
    assert Settings().is_development is False


def test_get_settings_is_cached() -> None:
    """One parse per process — settings are a cheap dependency to inject."""
    assert get_settings() is get_settings()


# --------------------------------------------------------------------------
# production guards — the defaults that are right for a laptop and wrong for users
# --------------------------------------------------------------------------
#
# Every one of these guards the same failure mode, which is why they are tested
# together: a default that makes a fresh clone work, and that would make the
# product quietly bad rather than visibly broken. Nothing would error. Search
# would return worse results, or answers would be quoted fragments — and no
# alert exists for "still running, just worse".


def test_production_refuses_the_offline_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """It matches shared words, never shared meaning, so "how do I claim
    expenses?" would miss a chunk titled "reimbursement policy"."""
    enter_production(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")

    with pytest.raises(ValidationError, match="EMBEDDING_PROVIDER"):
        Settings()


def test_production_refuses_openai_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caught at startup rather than on the first upload. Otherwise the process
    starts, accepts documents, and fails inside the worker — so the user sees
    `status=failed` on their file instead of an operator seeing a bad deploy."""
    enter_production(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings()


def test_production_refuses_the_offline_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """M7. The offline provider quotes retrieved sentences instead of
    generating, so shipping it would answer every question with a fragment of a
    document and no explanation."""
    enter_production(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "offline")

    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        Settings()


def test_production_refuses_anthropic_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the process starts healthy, passes its readiness probe, and
    fails only when a user asks something — so the deploy looks fine and the
    feature is simply broken."""
    enter_production(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings()


def test_development_tolerates_every_offline_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the contract, and the reason the guards are scoped to
    production: `git clone && make dev && pytest` must work with no API keys,
    no network, and no ceremony."""
    monkeypatch.setenv("APP_ENV", "development")

    settings = Settings()

    assert settings.embedding_provider == "hashing"
    assert settings.llm_provider == "offline"
