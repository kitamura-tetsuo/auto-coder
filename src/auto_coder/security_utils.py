"""
Security utilities for Auto-Coder.

This module provides functions and constants for security-related operations,
such as redacting sensitive information from logs.
"""

import re
from typing import Any, Dict, List, Union

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


def redact_data(data: Any) -> Any:
    """
    Recursively redact sensitive information from data structures (dicts, lists).

    Args:
        data: Data to redact (dict, list, str, or other)

    Returns:
        Redacted data structure
    """
    if isinstance(data, str):
        return redact_string(data)
    elif isinstance(data, dict):
        return {k: redact_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_data(item) for item in data]
    else:
        return data
