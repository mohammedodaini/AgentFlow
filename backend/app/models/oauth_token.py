"""`oauth_tokens` — token secrets, SEPARATE from integration metadata.

Layer: models.

Why this is not two more columns on `integrations`
--------------------------------------------------
It would be, if the only difference were normalisation. The difference is
*sensitivity*, and separating rows by sensitivity is what makes every other
control possible:

- **Least privilege becomes expressible.** A reporting role, a read replica for
  analytics, a support tool that lists connections — none of them need this
  table, and a separate table is something you can withhold. Columns on a table
  people already read are not.
- **`SELECT * FROM integrations` stays safe.** That query appears in support
  sessions, in ad-hoc scripts, and in the logs of anyone debugging. On a joined
  design it prints credentials to somebody else's Google account.
- **Rotation stops touching metadata.** Refreshing a token writes here and
  nowhere else, so nothing that reads connection state is invalidated by a
  routine background write.

**Everything in the two token columns is Fernet ciphertext, encrypted by
`core/security.encrypt_secret` before the value ever reaches SQLAlchemy.** The
encryption is deliberately *not* in this module: a `TypeDecorator` doing it
transparently is the tempting design, and it makes the most dangerous operation
in the system invisible at the call site. It should be visible.

Consequences of that, spelled out because they constrain callers
----------------------------------------------------------------
Fernet is non-deterministic, so these columns cannot be indexed, compared, or made
unique — a token is only ever reached through `integration_id`. And the database
can no longer validate anything about a token's shape: a truncated or wrong-key
value is indistinguishable from a valid one until it is decrypted, which is why
`decrypt_secret` raises rather than returning something plausible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.integration import Integration

REFRESH_SKEW = timedelta(seconds=60)
"""How early an access token is treated as expired.

A token expiring in two seconds is useless: the request carrying it still has to
be built, sent and received, and Google's clock is not ours. Without the skew a
small fraction of calls fail with a 401 that a retry would fix — the kind of
intermittent failure that gets diagnosed as "flaky network" for a year.
"""


class OAuthToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The credential for one integration. Encrypted at rest."""

    __tablename__ = "oauth_tokens"

    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    """One credential per integration, enforced by the database.

    `CASCADE` is the one place in this schema where deleting the parent *should*
    take the child: an orphaned token is a live credential to somebody's account
    with nothing left to say whose it is or why we hold it. Every other table here
    prefers `SET NULL` to preserve an audit trail; a secret is the exception,
    because keeping it is the risk.
    """

    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    """Fernet ciphertext.

    `Text`, not `String(n)`: ciphertext is longer than its plaintext and grows
    with it, and a length limit chosen against today's token format becomes a
    truncated — therefore undecryptable — credential the day a provider lengthens
    theirs.
    """

    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Fernet ciphertext, and the genuinely dangerous one.

    An access token expires in an hour. A refresh token is a long-lived, offline
    credential: anyone holding it can mint access tokens indefinitely, without the
    user present and without anything appearing in their sign-in history.

    Nullable for two reasons that look alike and are not. Google only issues one
    when `access_type=offline` *and* the user has not already granted consent — so
    a re-connect often returns none, and the correct behaviour is to keep the one
    already stored rather than overwrite it with NULL. Separately, some providers
    never issue one at all, which is a permanently valid state rather than an
    error.
    """

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the access token stops working, or NULL if the provider did not say.

    Nullable, and treated as "expired" by `needs_refresh`. That is the safe
    direction: an unnecessary refresh costs one HTTP call, while assuming an
    unknown expiry is still valid costs a failed user-facing operation.
    """

    integration: Mapped[Integration] = relationship(back_populates="tokens")

    def needs_refresh(self, *, now: datetime | None = None) -> bool:
        """Whether the access token should be exchanged before use.

        Asked *before* every call, not in response to a 401. Reacting to a 401
        means every expiry costs a wasted round trip and produces a failure that
        has to be told apart from a genuinely revoked credential — and those two
        are identical at the HTTP layer.
        """
        if self.expires_at is None:
            return True

        return (now or datetime.now(UTC)) >= self.expires_at - REFRESH_SKEW
