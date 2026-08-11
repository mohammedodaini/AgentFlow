"""Domain exception hierarchy.

Layer: core (leaf). Services raise these; an API-layer handler maps them to
HTTP status codes. Services must never import HTTPException — that would weld
business logic to the transport.
"""

from __future__ import annotations

# TODO(M1): class AppError(Exception) — base, carries message + machine-readable code
# TODO(M2): NotFoundError(AppError)
# TODO(M3): AuthenticationError, AuthorizationError, DuplicateEmailError
# TODO(M5): DocumentIngestionError
# TODO(M9): AgentExecutionError
# TODO(M12): ApprovalRequiredError, ApprovalExpiredError
