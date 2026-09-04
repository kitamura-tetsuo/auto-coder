"""
Custom exceptions used across Auto-Coder.
"""


class AutoCoderUsageLimitError(RuntimeError):
    """Raised by an LLM client when the provider usage/rate limit is reached.

    BackendManager catches this to rotate to the next backend.
    """

    pass


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
