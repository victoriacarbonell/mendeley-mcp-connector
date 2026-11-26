"""Exceptions raised by the connector.

Every message is written so it can be shown as-is to the end user or to the model:
it says what went wrong and, when possible, what to do next. Messages never
contain tokens or client secrets.
"""

from __future__ import annotations


class MendeleyError(Exception):
    """Base class for all connector errors."""


class NotAuthenticatedError(MendeleyError):
    """No stored credentials: the user has not logged in yet."""


class AuthenticationExpiredError(MendeleyError):
    """Stored credentials were rejected and cannot be refreshed automatically."""


class AuthorizationFlowError(MendeleyError):
    """The browser-based authorization step failed (denied, state mismatch, timeout)."""


class RateLimitError(MendeleyError):
    """Mendeley kept answering 429 Too Many Requests after all retries."""


class NotFoundError(MendeleyError):
    """The requested resource does not exist or is not visible to this account."""


class ApiError(MendeleyError):
    """Any other unexpected response from the Mendeley API."""

    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
