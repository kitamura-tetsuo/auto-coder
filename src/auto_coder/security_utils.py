"""
Security utilities for Auto-Coder.

This module provides functions and constants for security-related operations,
such as redacting sensitive information from logs.
"""

import re
from typing import List

# Patterns for sensitive data redaction
REDACTION_PATTERNS = [
    r"gh[pousr]_[a-zA-Z0-9]+",  # GitHub tokens
    r"github_pat_[a-zA-Z0-9_]+",  # GitHub PATs
    r"AIza[0-9A-Za-z-_]{35}",  # Google API keys
    r"sk-[a-zA-Z0-9]{48}",  # OpenAI keys (standard)
    r"sk-proj-[a-zA-Z0-9_-]+",  # OpenAI project keys
    r"xox[baprs]-([0-9a-zA-Z]{10,48})?",  # Slack tokens
    r"glpat-[0-9a-zA-Z\-\_]{20}",  # GitLab Personal Access Tokens
]


def redact_string(text: str) -> str:
    """
    Redact sensitive information from a string.

    Args:
        text: String to redact

    Returns:
        Redacted string
    """
    if not text:
        return text

    redacted = text
    for pattern in REDACTION_PATTERNS:
        redacted = re.sub(pattern, "[REDACTED]", redacted)
    return redacted
