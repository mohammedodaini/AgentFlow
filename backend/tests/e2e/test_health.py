"""First e2e test — proves the app factory + /health wiring (M1).

e2e = through HTTP via httpx.AsyncClient against the ASGI app; no mocks.
"""

from __future__ import annotations

# TODO(M1): test_health_live_returns_200 — build app via create_app(), GET /health/live
