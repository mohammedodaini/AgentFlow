"""Custom structlog processors.

Layer: observability. Processors enrich every log line; the request-ID one
reads the contextvar set by app/middleware/request_id.py so a single request
is greppable across API + worker logs.
"""

from __future__ import annotations

from structlog.typing import EventDict, WrappedLogger

from app.middleware.request_id import request_id_var


def add_request_id(
    logger: WrappedLogger | None,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Stamp the current request ID onto a log line, when there is one.

    Outside a request — worker jobs, startup, shutdown — the key is omitted
    entirely rather than logged as empty. An absent field is honest; a blank
    one invites you to grep for something that was never there.
    """
    request_id = request_id_var.get()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


# TODO(M9): add_agent_run_id — same trick for agent runs, set by the agent runtime
