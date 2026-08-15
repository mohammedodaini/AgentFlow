"""What a run cost, in money.

Layer: llm. Pure arithmetic over token counts `agent_steps` already records.

Why the rates are configuration and not a table in this file
------------------------------------------------------------
M9 set `agent_runs.cost_usd` to `Decimal(0)` and said why: *"A guessed rate here
would be worse than none: it would appear in reports, get trusted, and be wrong by
whatever margin the guessed rate was off."* M12 owns pricing, and that sentence
still holds — shipping a hardcoded price list would be exactly the thing M9 refused
to do, three milestones later and with more confidence.

So the *arithmetic* is the deliverable and the *rates* are operator input. With no
rates configured the answer is `0.000000`, which is the same number M9 stored and
means the same thing: nobody has told this system what it pays. The difference is
that supplying two environment variables now produces a real figure, per run,
derived from counts that were measured rather than estimated.

`Decimal`, never `float`
------------------------
Money in binary floating point accumulates error that surfaces as a bill nobody can
reconcile. `agent_runs.cost_usd` is `Numeric(10, 6)` for the same reason, and the
conversion from tokens has to stay in `Decimal` end to end — one `float` anywhere
in the chain reintroduces the problem the column type was chosen to avoid.
"""

from __future__ import annotations

from decimal import Decimal

TOKENS_PER_UNIT = Decimal(1_000_000)
"""Rates are quoted per million tokens, which is how every provider publishes them.

Keeping them in that unit means an operator copies a number off a pricing page
instead of converting it — and a conversion done by hand, once, into an environment
variable is one nobody ever checks again.
"""

COST_PLACES = Decimal("0.000001")
"""Six decimal places, matching `Numeric(10, 6)`.

Rounding happens here rather than in the database, so the value logged, returned in
an API response and written to the column is one number. A figure that rounded only
on the way in would let a response and a report disagree in the sixth decimal —
exactly the kind of discrepancy that costs an afternoon.
"""


def cost_of(
    *, input_tokens: int, output_tokens: int, input_rate: Decimal, output_rate: Decimal
) -> Decimal:
    """What those tokens cost, at those rates.

    Input and output are priced separately because every provider prices them
    separately, usually with output several times dearer. A single blended rate
    would be wrong for every workload whose shape differs from whatever mix the
    blend was computed against — and a RAG answer (long context, short answer) has a
    very different shape from a summary.
    """
    if input_tokens < 0 or output_tokens < 0:
        message = f"token counts cannot be negative: {input_tokens=}, {output_tokens=}"
        raise ValueError(message)

    total = (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / (
        TOKENS_PER_UNIT
    )
    return total.quantize(COST_PLACES)
