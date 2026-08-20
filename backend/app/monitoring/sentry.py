"""Sentry, if there is a DSN. Nothing at all if there is not.

Layer: monitoring.

**Optional, and never fatal.** A deployment with no Sentry account is valid, and
failing to start over an absent error reporter would be an outage caused by the
thing meant to observe outages. `configure_sentry` with no DSN returns having done
nothing, and the import is deferred so the SDK is not even loaded.

**What is scrubbed, and why it is set explicitly.**
Sentry's defaults already redact common credential-shaped keys, and this sets
`send_default_pii=False` on top of it. That is the flag controlling whether IP
addresses, cookies and usernames travel with each event — and cookies are the one
that matters here: ADR-0016 put the session's access and refresh tokens in
httpOnly cookies, so an event carrying cookies would ship live credentials to a
third party on every unhandled exception.

`traces_sample_rate` defaults to 0. Performance tracing bills per transaction, and
inheriting a spending decision from a default is how a bill arrives that nobody
chose.
"""

from __future__ import annotations

import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


def configure_sentry(settings: Settings) -> bool:
    """Initialise error reporting. Returns whether it was switched on.

    The return value exists for the test: "configured when a DSN is present, and
    silent when it is not" is the whole contract, and asserting on a log line
    would test the logger.
    """
    if not settings.sentry_dsn:
        return False

    try:
        # Imported here rather than at module scope so a deployment without the
        # SDK installed — or without a DSN — never pays the import, and a broken
        # optional dependency cannot stop the process starting.
        import sentry_sdk  # noqa: PLC0415 — deferred on purpose; see the module docstring
    except ImportError:
        logger.warning(
            "sentry.sdk_missing", detail="SENTRY_DSN is set but sentry-sdk is not installed"
        )
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        # No `release=`. Sentry groups regressions by release, and a wrong value
        # is worse than none — it would attribute every error to whatever string
        # happened to be here. Set it from the build's git SHA at deploy time,
        # via SENTRY_RELEASE, which the SDK reads from the environment itself.
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # See the module docstring. Cookies carry this application's session
        # tokens, so shipping them with an exception report would hand live
        # credentials to a third party.
        send_default_pii=False,
    )
    logger.info("sentry.configured", environment=settings.env)
    return True
