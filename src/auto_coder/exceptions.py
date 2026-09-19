"""
Custom exceptions used across Auto-Coder.
"""


class CloudSubmissionNotStartedError(RuntimeError):
    """Cloud dispatch conclusively ended without submitting remote work."""


from enum import Enum
from typing import Optional


class QuotaReason(str, Enum):
    QUOTA_INSUFFICIENT = "QUOTA_INSUFFICIENT"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    PROVIDER_USAGE_LIMIT = "PROVIDER_USAGE_LIMIT"


class DeliveryCertainty(str, Enum):
    NOT_SENT = "NOT_SENT"
    INDETERMINATE = "INDETERMINATE"
    DELIVERED = "DELIVERED"


class AutoCoderUsageLimitError(RuntimeError):
    """Raised by an LLM client when the provider usage/rate limit is reached.

    BackendManager catches this to rotate to the next backend.
    """

    pass


class ClaudeUsageDeferralError(AutoCoderUsageLimitError):
    """Raised by ClaudeRoutineClient when usage limits defer a follow-up."""

    def __init__(
        self,
        message: str,
        reason: QuotaReason,
        certainty: DeliveryCertainty,
        retry_not_before: float,
        repository: Optional[str] = None,
        backend_name: Optional[str] = None,
        credential_context: Optional[str] = None,
        observation_time: Optional[float] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.certainty = certainty
        self.retry_not_before = retry_not_before
        self.repository = repository
        self.backend_name = backend_name
        self.credential_context = credential_context
        self.observation_time = observation_time


class AutoCoderTimeoutError(RuntimeError):
    """Raised by an LLM client when a command timeout occurs.

    This indicates that the LLM command exceeded the configured timeout
    and was terminated.
    """

    pass


class AutoCoderRetryableBackendError(RuntimeError):
    """A provider transport outage that should be deferred by the scheduler.

    Unlike an implementation failure, this exception says that the coding agent
    could not complete its turn.  Callers must preserve it across orchestration
    boundaries so the target remains eligible without consuming attempt state.
    """

    pass
