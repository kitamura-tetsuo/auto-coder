"""Durable circuit breaker for semantic specification repair rounds."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class RepairRoundApplication:
    """The remediation which may safely be applied for one generation."""

    remediation: str
    previous_rounds: int
    reason: Optional[str] = None


class SpecificationRepairRoundStore:
    """Atomically count distinct applied EDIT_IN_PLACE contract generations."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "specification_repair_rounds.json"

    def apply(self, subject_kind: str, subject_number: int, generation: str, remediation: str, limit: int) -> RepairRoundApplication:
        """Record or upgrade a trustworthy current BLOCKED remediation."""
        if limit <= 0:
            raise ValueError("specification repair-round limit must be a positive integer")
        key = f"{subject_kind}:{subject_number}"
        with self._locked():
            state = self._read()
            raw = state.setdefault(key, {"edit_in_place_generations": []})
            if not isinstance(raw, dict):
                raise ValueError(f"Invalid specification repair-round state for {key}")
            generations = raw.get("edit_in_place_generations")
            if not isinstance(generations, list) or any(not isinstance(item, str) for item in generations):
                raise ValueError(f"Invalid specification repair-round generations for {key}")
            previous = len(generations)
            # Reapplication and policy-only review of an already applied contract
            # retain the original disposition and never consume or reinterpret budget.
            if generation in generations:
                return RepairRoundApplication("EDIT_IN_PLACE", previous)
            if remediation != "EDIT_IN_PLACE":
                return RepairRoundApplication(remediation, previous)
            if previous >= limit:
                reason = f"repair_round_limit_exhausted(limit={limit},previously_applied_edit_in_place_rounds={previous})"
                return RepairRoundApplication("REISSUE_REQUIRED", previous, reason)
            generations.append(generation)
            self._write(state)
            return RepairRoundApplication("EDIT_IN_PLACE", previous)

    def count(self, subject_kind: str, subject_number: int) -> int:
        raw = self._read().get(f"{subject_kind}:{subject_number}")
        generations = raw.get("edit_in_place_generations") if isinstance(raw, dict) else None
        return len(generations) if isinstance(generations, list) else 0

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)
