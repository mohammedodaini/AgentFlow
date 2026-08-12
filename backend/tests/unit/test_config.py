"""Settings behaviour (M1).

Configuration is the first thing that runs and the first thing to break a
deploy. These tests pin the contract documented in `.env.example`.
"""

from __future__ import annotations

import pytest
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


def test_app_env_maps_to_env_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """`APP_ENV` is the documented variable name; `settings.env` is the field."""
    monkeypatch.setenv("APP_ENV", "production")
    # M3: production refuses to start on the placeholder signing key, so any
    # test claiming to be production has to supply a real one.
    monkeypatch.setenv("SECRET_KEY", "f" * 64)

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

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "f" * 64)  # see the note in the test above
    assert Settings().is_development is False


def test_get_settings_is_cached() -> None:
    """One parse per process — settings are a cheap dependency to inject."""
    assert get_settings() is get_settings()
