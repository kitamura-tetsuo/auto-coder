"""Durable subject-level stops created by terminal specification remediation."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional


class ReissueRequiredStore:
    """Atomically persist stable Issue identities that must be replaced."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "reissue_required.json"
        self._lock = threading.Lock()

    def _read(self) -> set[int]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()
        subjects = value.get("issue_numbers") if isinstance(value, dict) else None
        if not isinstance(subjects, list):
            return set()
        return {number for number in subjects if isinstance(number, int) and not isinstance(number, bool)}

    def contains(self, issue_number: int) -> bool:
        with self._lock:
            return issue_number in self._read()

    def mark(self, issue_number: int) -> bool:
        """Persist the marker before external side effects; return whether it was new."""
        import fcntl

        with self._lock:
            lock_path = self.path.with_suffix(".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                subjects = self._read()
                if issue_number in subjects:
                    return False
                subjects.add(issue_number)
                temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
                temporary.write_text(json.dumps({"issue_numbers": sorted(subjects)}, indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
                return True
