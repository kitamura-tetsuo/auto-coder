"""Crash-safe state storage for the in-container worker supervisor."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class SupervisorStore:
    """A small, single-writer durable JSON store owned by the supervisor."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"schema_version": 1, "operations": {}, "worker": None, "stopped": False}
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("invalid supervisor state")
        return value

    def write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".supervisor-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
