"""
Custom exceptions used across Auto-Coder.
"""


class AutoCoderUsageLimitError(RuntimeError):
    """Raised by an LLM client when the provider usage/rate limit is reached.

    BackendManager catches this to rotate to the next backend.
    """

    pass
