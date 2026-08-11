"""Domain exception hierarchy.

Layer: core (leaf). Services raise these; an API-layer handler maps them to
HTTP status codes. Services must never import HTTPException — that would weld
business logic to the transport.

That rule is not cosmetic. Services in this codebase have three callers: HTTP
routes, background workers, and agent tools. An HTTPException raised inside a
service would be meaningless to the latter two.
"""

from __future__ import annotations

from typing import ClassVar


class AppError(Exception):
    """Base for every error this application raises deliberately.

    Carries a human `message` and a stable machine-readable `code`. The code is
    what clients branch on; the message is what humans read. Keeping them
    separate means you can reword a message without breaking a consumer.
    """

    default_code: ClassVar[str] = "app_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message: str = message
        self.code: str = code or self.default_code

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# TODO(M2): NotFoundError(AppError)
# TODO(M3): AuthenticationError, AuthorizationError, DuplicateEmailError
# TODO(M5): DocumentIngestionError
# TODO(M9): AgentExecutionError
# TODO(M12): ApprovalRequiredError, ApprovalExpiredError
