# ruff: noqa: F401  — remove once this module is implemented (M2)
"""Readiness checks — can we actually serve? (DB reachable, Redis reachable.)

Liveness stays trivial in the route; THIS module owns dependency probes so
the route stays thin and the checks are unit-testable.
"""

from __future__ import annotations

# TODO(M2): async check_readiness() -> dict[str, bool] — SELECT 1, Redis PING,
#           each with a short timeout (a hung check is worse than a failed one)
