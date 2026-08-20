"""Google Calendar OAuth: the scopes, and nothing else.

Layer: integrations. Implements `OAuthProvider` by inheriting the whole Google
flow from `app/integrations/google_oauth.py`.

**This module used to be two hundred lines.** M11 wrote the flow here because
Calendar was the only Google product connected, and a shared base class for one
implementation is a guess dressed as a design. M14 connects Gmail, and every line
of that flow turned out to be identical apart from the scope list — so it moved,
and what remains here is the part a reviewer actually needs to find: **what this
integration asks permission to do.**

The URL and error-code constants are re-exported so that the obvious import
(`from app.integrations.google_calendar.oauth import TOKEN_URL`) keeps working.
"""

from __future__ import annotations

from app.integrations.google_oauth import (
    AUTHORIZE_URL,
    EMAIL_SCOPE,
    REVOKED_ERRORS,
    TOKEN_URL,
    USERINFO_URL,
    GoogleOAuth,
)

__all__ = [
    "AUTHORIZE_URL",
    "REVOKED_ERRORS",
    "SCOPES",
    "TOKEN_URL",
    "USERINFO_URL",
    "GoogleCalendarOAuth",
]

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    EMAIL_SCOPE,
]
"""Read *and write* events, plus the address of the account that granted it.

M11 requested `calendar.readonly` deliberately, and said why: a write scope would
have sat unused for a milestone while every connected user had already granted an
agent permission to alter their diary. **M12 is the milestone that earns it** —
nothing here can write without an `approvals` row a human decided on.

`calendar.events` rather than the broader `calendar`: it covers reading and
writing events, and stops short of managing calendars themselves (creating them,
deleting them, changing sharing). The agent has no use for that, and a scope
nobody uses is only ever a liability.

**Widening a scope is not free, and it is not silent.** Google issues tokens for
the scopes granted at consent time, so every account connected under M11 holds a
read-only credential and will keep holding one. Those integrations keep working
for reads and will fail on a write — which is why `Integration.scopes` records
what was *granted* rather than what was asked for, and why the milestone note says
plainly that existing users must reconnect.
"""


class GoogleCalendarOAuth(GoogleOAuth):
    """Google's flow, pointed at the Calendar scopes."""

    provider = "google_calendar"
    scope_list = SCOPES
