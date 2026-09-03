"""Parsing and validation for provider-issued Codex Cloud task identifiers."""

import re
from typing import Optional

_CODEX_CLOUD_TASK_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])(task_e_[A-Za-z0-9]+)(?![A-Za-z0-9_-])")


def is_valid_codex_cloud_task_id(value: object) -> bool:
    """Return whether *value* has the provider-issued ``task_e_<token>`` shape."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return bool(_CODEX_CLOUD_TASK_PATTERN.fullmatch(candidate))


def extract_codex_cloud_task_id(value: object) -> Optional[str]:
    """Return the first valid Codex Cloud task ID embedded in *value*."""
    if not isinstance(value, str):
        return None
    for match in _CODEX_CLOUD_TASK_PATTERN.finditer(value):
        candidate = match.group(1)
        if is_valid_codex_cloud_task_id(candidate):
            return candidate
    return None
