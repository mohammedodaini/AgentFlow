"""Gmail OAuth: the scopes, and why one of them is not the obvious one.

Layer: integrations. Implements `OAuthProvider` by inheriting the Google flow
from `app/integrations/google_oauth.py` — Gmail is the second Google product
here, and it is what turned that flow into shared code.

`gmail.compose` instead of `gmail.send`
---------------------------------------
`gmail.send` is the narrower scope and the obvious pick, and it is not what the
email agent uses. `app/agents/email/tools.py` creates a draft and then sends that
draft, because the two-step version has the better failure mode: when the send
fails, the message is sitting in the user's own Drafts folder where they can see
it and finish it by hand. A failed `messages.send` leaves nothing, and the user is
told an email they approved did not go, with no way to recover the text.

That costs one extra request and one wider scope, and the scope is the part worth
weighing: `gmail.compose` can read and delete drafts as well as create them.
Nothing here does either, and a user reading their consent screen sees a broader
permission than the feature strictly needs. It is the right trade for a message
that cannot be unsent, and it is a trade rather than a free win.

Neither scope lets this application read the inbox — `gmail.readonly` is requested
separately, so the ability to *read* mail and the ability to *send* it appear as
two grants on the consent screen rather than one word covering both.

What is deliberately not requested: `https://mail.google.com/`, the full-mailbox
scope that permits permanent deletion. Nothing here deletes mail, and a scope
nobody uses is only ever a liability.
"""

from __future__ import annotations

from app.integrations.google_oauth import EMAIL_SCOPE, GoogleOAuth

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    EMAIL_SCOPE,
]
"""Read the mailbox, create and send drafts, and know whose account it is.

See the module docstring for why `gmail.compose` rather than `gmail.send`. These
are genuinely broad permissions — broader than anything else M14 requests — and
they are what the email agent needs to draft a reply a human can read before it
goes anywhere.
"""


class GmailOAuth(GoogleOAuth):
    """Google's flow, pointed at the Gmail scopes."""

    provider = "gmail"
    scope_list = SCOPES
