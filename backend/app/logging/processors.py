# ruff: noqa: F401  — remove once this module is implemented (M1)
"""Custom structlog processors.

Layer: observability. Processors enrich every log line; the request-ID one
reads the contextvar set by app/middleware/request_id.py so a single request
is greppable across API + worker logs.
"""

from __future__ import annotations

import structlog

# TODO(M1): add_request_id(logger, method, event_dict) — inject request_id contextvar
# TODO(M9): add_agent_run_id — same trick for agent runs, set by the agent runtime
