"""Stripe API client — read-only, and structurally so.

Layer: integrations. Returns our shapes, never Stripe's.

**There is no write method here, and there is no `post_json` call.** The
credential this client carries is a live secret key for somebody else's business
(see `oauth.py`), so "read-only" is enforced by the `read_only` scope on Stripe's
side *and* by the absence of any method that could do otherwise on ours. Either
alone would be a single point of failure for the worst outcome in this codebase.

Money is `Decimal`, never `float`
---------------------------------
Stripe sends integer minor units — `amount: 2500` for £25.00 — precisely to avoid
the problem `float` reintroduces. Dividing by 100 into a float would put binary
rounding error into a figure someone reconciles against a bank statement, which
is the same argument `agent_runs.cost_usd` makes for `Numeric`.

Zero-decimal currencies are the trap underneath it: JPY has no minor unit, so
`amount: 2500` means ¥2,500 and dividing by 100 understates it by two orders of
magnitude.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.integrations.base import BaseClient

CHARGES_URL = "https://api.stripe.com/v1/charges"

STRIPE_VERSION = "2024-06-20"
"""Pinned, for the same reason GitHub's version header is: Stripe rolls the
default API version forward for new accounts, and an unpinned client changes
behaviour on a date chosen elsewhere."""

MAX_RESULTS = 25

ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)
"""Currencies with no minor unit, per Stripe's published list.

`amount: 2500` is ¥2,500 in JPY and £25.00 in GBP. A single `/ 100` would report
the yen figure as ¥25 — a hundredfold error in a financial display, produced by
code that looks obviously correct.
"""


@dataclass(frozen=True)
class StripeCharge:
    """One charge, reduced to what this product uses."""

    charge_id: str
    amount: Decimal
    currency: str
    status: str
    description: str | None
    created_at: datetime


class StripeClient(BaseClient):
    """Reads recent charges. Never writes — see the module docstring."""

    extra_headers = {"Stripe-Version": STRIPE_VERSION}

    forbidden_message = (
        "This Stripe account was connected with read-only access, or the "
        "connection was removed from the Stripe dashboard. Reconnect it."
    )

    async def list_charges(
        self, access_token: str, *, limit: int = MAX_RESULTS
    ) -> list[StripeCharge]:
        """The most recent charges on the connected account, newest first."""
        payload = await self.get_json(
            CHARGES_URL,
            access_token=access_token,
            params={"limit": min(limit, MAX_RESULTS)},
        )
        return [_as_charge(item) for item in payload.get("data", [])]


def _as_charge(item: dict[str, Any]) -> StripeCharge:
    """Translate one Stripe charge object into ours."""
    currency = str(item.get("currency", "usd")).lower()
    minor_units = int(item.get("amount", 0))
    divisor = 1 if currency in ZERO_DECIMAL_CURRENCIES else 100

    return StripeCharge(
        charge_id=str(item.get("id", "")),
        # Decimal from an int, never through a float. `Decimal(2500) / 100` is
        # exactly 25; `Decimal(2500 / 100)` is 25.00000000000000000000000001-ish,
        # because the division happened in binary before Decimal ever saw it.
        amount=Decimal(minor_units) / divisor,
        currency=currency.upper(),
        status=str(item.get("status", "unknown")),
        description=item.get("description"),
        # Stripe timestamps are Unix seconds, and `fromtimestamp` without a
        # timezone returns a naive datetime in the *server's* local zone — which
        # is UTC in a container and something else on this laptop, so the bug
        # would only ever appear in development or only ever in production.
        created_at=datetime.fromtimestamp(int(item.get("created", 0)), tz=UTC),
    )
