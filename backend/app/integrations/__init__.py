"""Building OAuth providers, and finding them at request time.

Layer: integrations. The composition root for this package — the same shape as
`create_storage` (M5), `create_embedder` (M6) and `create_llm` (M7): a factory
that reads settings, a thing built once per process in `lifespan()`, and a
dependency that hands it to routes.

Why a registry rather than a factory per request
------------------------------------------------
`OfflineOAuthProvider` is **stateful**. It holds the codes it has issued and the
refresh tokens it still considers live, because that is what an authorization
server is. Building a fresh one per request would mean a code issued by the
connect endpoint was unknown to the callback endpoint — the flow would fail for a
reason having nothing to do with the code under test.

One instance per provider per process is therefore not an optimisation; it is what
makes the offline provider work at all. The real Google provider is stateless and
would not care either way.
"""

from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.integrations.base import OAuthProvider
from app.integrations.google_calendar.oauth import SCOPES as GOOGLE_CALENDAR_SCOPES
from app.integrations.google_calendar.oauth import GoogleCalendarOAuth
from app.integrations.offline import OfflineOAuthProvider
from app.models.integration import Provider

SUPPORTED_PROVIDERS = (Provider.GOOGLE_CALENDAR,)
"""What can actually be connected today.

`Provider` declares seven values so adding the second integration is a deploy
rather than an `ALTER TYPE` (M9's lesson, applied to a different enum). This tuple
is the honest subset — one entry — and it is what the API validates against, so
asking to connect Slack is a clear 404 rather than a redirect to an authorization
server that was never configured.
"""


class OAuthRegistry:
    """The providers this process can drive, one instance each."""

    def __init__(self, providers: dict[Provider, OAuthProvider]) -> None:
        self._providers = providers

    def get(self, provider: Provider) -> OAuthProvider | None:
        return self._providers.get(provider)

    def __contains__(self, provider: Provider) -> bool:
        return provider in self._providers


def create_oauth_registry(settings: Settings) -> OAuthRegistry:
    """Build every supported provider, honouring `OAUTH_PROVIDER`.

    Returns a registry rather than raising when Google is unconfigured: a
    deployment that never connects an integration is perfectly valid, and refusing
    to start over an absent optional credential would turn an unused feature into
    an outage. The connect endpoint is where an unconfigured provider becomes an
    error, because that is where somebody is present to be told which variable is
    missing.
    """
    if settings.oauth_provider == "offline":
        return OAuthRegistry(
            {
                Provider.GOOGLE_CALENDAR: OfflineOAuthProvider(
                    Provider.GOOGLE_CALENDAR.value, list(GOOGLE_CALENDAR_SCOPES)
                )
            }
        )

    return OAuthRegistry({Provider.GOOGLE_CALENDAR: GoogleCalendarOAuth(settings)})


def get_oauth_registry(request: Request) -> OAuthRegistry:
    """Read the registry `lifespan()` stored on the application."""
    registry: OAuthRegistry = request.app.state.oauth_registry
    return registry
