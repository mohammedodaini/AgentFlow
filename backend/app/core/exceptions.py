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


class NotFoundError(AppError):
    """The addressed resource does not exist, or the caller may not see it.

    Deliberately the same error for both. Answering 403 for a row that exists
    and 404 for one that does not turns any endpoint into an oracle: an
    attacker enumerates ids and learns exactly which ones are real.
    """

    default_code: ClassVar[str] = "not_found"


class AuthenticationError(AppError):
    """We do not know who you are — bad credentials, or a bad/expired token.

    Maps to 401. The message stays deliberately vague at the call sites; see
    `AuthService.login`.
    """

    default_code: ClassVar[str] = "authentication_failed"


class AuthorizationError(AppError):
    """We know who you are, and you may not do this. Maps to 403.

    The distinction from AuthenticationError matters to clients: 401 means
    "refresh your token and retry", 403 means "stop, retrying will not help".
    """

    default_code: ClassVar[str] = "not_authorized"


class ConflictError(AppError):
    """The request collides with existing state. Maps to 409."""

    default_code: ClassVar[str] = "conflict"


class DuplicateEmailError(ConflictError):
    """Registration with an email that already exists.

    A subclass of ConflictError so a handler that only knows about conflicts
    still does the right thing, while callers that care can catch this
    specifically.
    """

    default_code: ClassVar[str] = "duplicate_email"


# TODO(M5): DocumentIngestionError
# TODO(M9): AgentExecutionError
# TODO(M12): ApprovalRequiredError, ApprovalExpiredError
