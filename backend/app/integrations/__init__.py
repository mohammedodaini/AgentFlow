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
makes the offline provider work at all. The real providers are stateless and would
not care either way.

M14: a provider is registered only if it is configured
-------------------------------------------------------
M11 registered Google unconditionally and let a missing `GOOGLE_CLIENT_ID`
surface as an authorize URL with an empty `client_id` — a consent screen that
loads and then reports that the application does not exist, several steps away
from the variable nobody set.

With five providers that becomes the common case: almost every deployment will
configure one or two. So the registry holds what can actually be driven, and
asking for anything else is a clean 404 naming the two variables to set.
"""

from __future__ import annotations

from fastapi import Request

from app.core.config import Settings
from app.integrations.base import OAuthProvider
from app.integrations.github.oauth import SCOPES as GITHUB_SCOPES
from app.integrations.github.oauth import GitHubOAuth
from app.integrations.gmail.oauth import SCOPES as GMAIL_SCOPES
from app.integrations.gmail.oauth import GmailOAuth
from app.integrations.google_calendar.oauth import SCOPES as GOOGLE_CALENDAR_SCOPES
from app.integrations.google_calendar.oauth import GoogleCalendarOAuth
from app.integrations.notion.oauth import SCOPES as NOTION_SCOPES
from app.integrations.notion.oauth import NotionOAuth
from app.integrations.offline import OfflineOAuthProvider
from app.integrations.slack.oauth import SCOPES as SLACK_SCOPES
from app.integrations.slack.oauth import SlackOAuth
from app.integrations.stripe.oauth import SCOPES as STRIPE_SCOPES
from app.integrations.stripe.oauth import StripeOAuth
from app.models.integration import Provider

SUPPORTED_PROVIDERS = (
    Provider.GMAIL,
    Provider.GOOGLE_CALENDAR,
    Provider.SLACK,
    Provider.NOTION,
    Provider.GITHUB,
    Provider.STRIPE,
)
"""What this codebase implements. Six of the seven `Provider` values.

`GOOGLE_DRIVE` is absent, and that is a decision rather than an oversight: nothing
in this product reads a file from Drive, and an integration that connects
successfully and then has no endpoint to call is worse than one that says it is
not available. It stays in the enum so adding it later is a deploy, not an
`ALTER TYPE`.
"""

CREDENTIAL_VARIABLES: dict[Provider, tuple[str, str]] = {
    Provider.GMAIL: ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    Provider.GOOGLE_CALENDAR: ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    Provider.SLACK: ("SLACK_CLIENT_ID", "SLACK_CLIENT_SECRET"),
    Provider.NOTION: ("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET"),
    Provider.GITHUB: ("GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"),
    Provider.STRIPE: ("STRIPE_CLIENT_ID", "STRIPE_CLIENT_SECRET"),
}
"""Which environment variables configure each provider, for the error message.

Gmail and Google Calendar share a pair, because they are the same OAuth client
with different scopes — one Google Cloud project, two products.
"""

PERPETUAL_PROVIDERS = frozenset({Provider.SLACK, Provider.NOTION, Provider.GITHUB})
"""Providers whose credentials never expire and cannot be refreshed.

Used to give the *offline* provider the same credential shape the real one issues.
Without this the offline flow would hand back a Google-shaped grant for Slack, and
the bug M14 fixed in `OAuthToken.needs_refresh` would have stayed invisible to
every test in the suite — because the test double only ever produced the shape the
code already handled.
"""


class OAuthRegistry:
    """The providers this process can drive, one instance each."""

    def __init__(self, providers: dict[Provider, OAuthProvider]) -> None:
        self._providers = providers

    def get(self, provider: Provider) -> OAuthProvider | None:
        return self._providers.get(provider)

    def __contains__(self, provider: Provider) -> bool:
        return provider in self._providers

    def configured(self) -> list[Provider]:
        """Which providers this deployment can actually connect.

        Returned in `SUPPORTED_PROVIDERS` order rather than dict order, so the
        connect screen does not reorder itself when an operator sets a new
        variable.
        """
        return [provider for provider in SUPPORTED_PROVIDERS if provider in self._providers]


def create_oauth_registry(settings: Settings) -> OAuthRegistry:
    """Build every provider this deployment has credentials for.

    Returns a registry rather than raising when nothing is configured: a
    deployment that never connects an integration is perfectly valid, and refusing
    to start over an absent optional credential would turn an unused feature into
    an outage. The connect endpoint is where an unconfigured provider becomes an
    error, because that is where somebody is present to be told which variable is
    missing.
    """
    if settings.oauth_provider == "offline":
        scopes_by_provider = {
            Provider.GMAIL: GMAIL_SCOPES,
            Provider.GOOGLE_CALENDAR: GOOGLE_CALENDAR_SCOPES,
            Provider.SLACK: SLACK_SCOPES,
            Provider.NOTION: NOTION_SCOPES,
            Provider.GITHUB: GITHUB_SCOPES,
            Provider.STRIPE: STRIPE_SCOPES,
        }
        return OAuthRegistry(
            {
                provider: OfflineOAuthProvider(
                    provider.value,
                    list(scopes),
                    perpetual=provider in PERPETUAL_PROVIDERS,
                )
                for provider, scopes in scopes_by_provider.items()
            }
        )

    google_configured = bool(
        settings.google_client_id and settings.google_client_secret.get_secret_value()
    )
    candidates: dict[Provider, tuple[bool, OAuthProvider]] = {
        Provider.GMAIL: (google_configured, GmailOAuth(settings)),
        Provider.GOOGLE_CALENDAR: (google_configured, GoogleCalendarOAuth(settings)),
        Provider.SLACK: (
            bool(settings.slack_client_id and settings.slack_client_secret.get_secret_value()),
            SlackOAuth(settings),
        ),
        Provider.NOTION: (
            bool(settings.notion_client_id and settings.notion_client_secret.get_secret_value()),
            NotionOAuth(settings),
        ),
        Provider.GITHUB: (
            bool(settings.github_client_id and settings.github_client_secret.get_secret_value()),
            GitHubOAuth(settings),
        ),
        Provider.STRIPE: (
            bool(settings.stripe_client_id and settings.stripe_client_secret.get_secret_value()),
            StripeOAuth(settings),
        ),
    }

    return OAuthRegistry(
        {provider: oauth for provider, (configured, oauth) in candidates.items() if configured}
    )


def get_oauth_registry(request: Request) -> OAuthRegistry:
    """Read the registry `lifespan()` stored on the application."""
    registry: OAuthRegistry = request.app.state.oauth_registry
    return registry
