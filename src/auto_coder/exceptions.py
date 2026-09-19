"""
Custom exceptions used across Auto-Coder.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CloudSubmissionNotStartedError(RuntimeError):
    """Cloud dispatch conclusively ended without submitting remote work."""


class AutoCoderUsageLimitError(RuntimeError):
    """Raised by an LLM client when the provider usage/rate limit is reached.

    BackendManager catches this to rotate to the next backend.
    """

    pass


class ClaudeFollowupDeferralReason(str, Enum):
    """Machine-readable reasons why a Claude follow-up was deferred."""

    QUOTA_INSUFFICIENT = "QUOTA_INSUFFICIENT"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    PROVIDER_USAGE_LIMIT = "PROVIDER_USAGE_LIMIT"


class DeliveryCertainty(str, Enum):
    """Whether the provider can have accepted a deferred assignment."""

    NOT_SENT = "NOT_SENT"
    INDETERMINATE = "INDETERMINATE"


@dataclass(eq=False)
class ClaudeFollowupUsageLimitError(AutoCoderUsageLimitError):
    """Structured Claude existing-session quota deferral."""

    reason: ClaudeFollowupDeferralReason
    repository: Optional[str]
    backend_name: str
    credential_context: str
    observed_at: float
    retry_not_before: float
    delivery_certainty: DeliveryCertainty
    blocking_windows: tuple[str, ...] = field(default_factory=tuple)
    reset_times: tuple[float, ...] = field(default_factory=tuple)
    diagnostic: str = ""

    def __post_init__(self) -> None:
        AutoCoderUsageLimitError.__init__(
            self,
            f"Claude follow-up deferred ({self.reason.value}, " f"{self.delivery_certainty.value}): {self.diagnostic}",
        )


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
