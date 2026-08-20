"""Integration API shapes. Token values NEVER appear in any response schema.

Layer: schemas — the API boundary.

That rule is enforced structurally rather than by care: `IntegrationRead` has no
token field to fill in, so publishing one would take somebody adding both a field
and a line to populate it. The whitelist is the mechanism (see
`schemas/common.py`), and this is the table where it matters most — the
return-the-ORM-object mistake would serialise `oauth_tokens` straight into a JSON
response.

Nothing here exposes the *ciphertext* either. It is not directly usable without
the key, but publishing it hands an attacker an offline target: unlimited attempts
against a value whose plaintext is a credential, with no rate limit and nothing
logged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.models.integration import IntegrationStatus, Provider
from app.schemas.common import APIModel


class IntegrationRead(APIModel):
    """One connected account, as the outside world sees it."""

    id: uuid.UUID
    provider: Provider
    status: IntegrationStatus
    external_account_id: str | None = Field(
        default=None,
        description="Which account on the provider's side — an email address for Google",
    )
    scopes: list[str] = Field(description="What the user actually granted, per the provider")
    created_at: datetime

    @property
    def needs_reconnect(self) -> bool:
        """Whether a human has to act.

        Derived rather than stored, because it is a question about the *current*
        status — and a stored copy would be one more thing that can disagree with
        it.
        """
        return self.status is IntegrationStatus.REVOKED


class ConnectStart(APIModel):
    """Where to send the user to grant consent.

    A URL in a JSON body rather than a 302, and the choice is deliberate. A
    redirect issued in response to an XHR is followed by the browser invisibly —
    the SPA gets an opaque CORS failure instead of a consent screen. Handing back
    the URL lets the client choose between a full-page navigation and a popup,
    which is a product decision rather than one the API should make for it.
    """

    authorize_url: str
    provider: Provider


class ProviderRead(APIModel):
    """One provider this deployment can connect, for the connect screen.

    Exists so the frontend does not carry its own copy of the supported list. A
    hard-coded list in the UI drifts the moment an operator sets a variable, and
    the failure is a button that leads to a 404.
    """

    provider: Provider
    scopes: list[str] = Field(description="What connecting it will request permission for")


class CalendarEventRead(APIModel):
    """One calendar event, in our shape rather than Google's.

    `starts_at` is nullable because an all-day event has a date and no time.
    Rendering that as midnight would be a quiet lie — see
    `integrations/google_calendar/client.py`.
    """

    event_id: str
    title: str
    starts_at: datetime | None
    ends_at: datetime | None
    all_day: bool
    url: str | None


class SlackChannelRead(APIModel):
    """One public Slack channel."""

    channel_id: str
    name: str = Field(description="Including the leading '#'")
    topic: str | None
    member_count: int | None
    is_archived: bool


class NotionPageRead(APIModel):
    """One Notion page the integration was given access to."""

    page_id: str
    title: str
    url: str | None
    last_edited_at: datetime | None


class GitHubRepositoryRead(APIModel):
    """One GitHub repository.

    `private` is always False in practice at M14, because the connection requests
    no repository scope — see `integrations/github/oauth.py`. The field is here
    rather than omitted because the *data* has it, and a schema that hides a field
    to match today's scope is one that lies the day the scope changes.
    """

    full_name: str
    description: str | None
    private: bool
    url: str
    updated_at: datetime | None


class StripeChargeRead(APIModel):
    """One Stripe charge, with the amount as a decimal.

    `Decimal`, so it serialises as `25.00` rather than a float that may render as
    `24.999999999999996`. Stripe's own integer minor units are converted in the
    client — including the zero-decimal currencies, where dividing by 100 is a
    hundredfold error.
    """

    charge_id: str
    amount: Decimal
    currency: str
    status: str
    description: str | None
    created_at: datetime


class EmailMessageRead(APIModel):
    """One message from the connected mailbox."""

    message_id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime | None
