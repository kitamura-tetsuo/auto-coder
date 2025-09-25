from __future__ import annotations

import os
from contextlib import ContextDecorator
from typing import Dict


class patch_environment(ContextDecorator):
    """Context manager to temporarily update environment variables."""

    def __init__(self, updates: Dict[str, str]):
        self._updates = updates
        self._original: Dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._updates.items():
            self._original[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type, exc, tb) -> None:
        for key in self._updates:
            original = self._original.get(key)
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

