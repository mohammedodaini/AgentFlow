"""Structured-logging processors (M1).

The request-ID processor is what makes a single request greppable across API
and worker logs, so both of its branches (set / unset) are pinned here.
"""

from __future__ import annotations

from typing import Any

from app.logging.processors import add_request_id
from app.middleware.request_id import request_id_var


def test_add_request_id_injects_the_current_value() -> None:
    token = request_id_var.set("abc-123")
    try:
        event_dict: dict[str, Any] = {"event": "something happened"}

        result = add_request_id(None, "info", event_dict)

        assert result["request_id"] == "abc-123"
    finally:
        request_id_var.reset(token)


def test_add_request_id_is_a_noop_outside_a_request() -> None:
    """Worker and startup logs have no request; they must not grow an empty key."""
    event_dict: dict[str, Any] = {"event": "worker started"}

    result = add_request_id(None, "info", event_dict)

    assert "request_id" not in result
