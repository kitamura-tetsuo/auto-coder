"""Durable, overlap-safe identities for adversarial-validation attempts."""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class AdversarialValidationAttempt:
    attempt_id: str = ""
    sequence: int = 0


class AdversarialValidationAttemptRepository:
    """Allocate attempts atomically and update only the identified attempt."""

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "adversarial_validation_attempts.json"
        self.lock_path = self.storage_path.with_suffix(".lock")
        self.transition_lock_path = self.storage_path.with_suffix(".transition.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def serialized_transition(self) -> Iterator[None]:
        """Fence publication and merge transitions across processes.

        Attempt-state reads alone cannot close the check-to-merge race. Every
        durable publication and every final merge authority check uses this
        separate lock, while ordinary state mutations retain their short lock.
        """
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transition_lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.transition_lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, object]:
        if not self.storage_path.exists():
            return {"next_sequence": 1, "attempts": []}
        with self.storage_path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or not isinstance(state.get("attempts"), list) or not isinstance(state.get("next_sequence"), int):
            raise RuntimeError("Adversarial-validation attempt state is invalid")
        return state

    def _write(self, state: dict[str, object]) -> None:
        temporary = self.storage_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_path)

    def start(self, pr_number: int, head_sha: str) -> AdversarialValidationAttempt:
        # Starting an attempt participates in the same transition fence as
        # review-thread mutation/publication. Once an attempt has established
        # authority, a newer start cannot appear midway through those effects.
        with self.serialized_transition():
            with self._locked():
                state = self._read()
                next_sequence = state["next_sequence"]
                if not isinstance(next_sequence, int):
                    raise RuntimeError("Adversarial-validation attempt sequence is invalid")
                sequence = next_sequence
                attempt = AdversarialValidationAttempt(uuid.uuid4().hex, sequence)
                attempts = state["attempts"]
                assert isinstance(attempts, list)
                attempts.append({"id": attempt.attempt_id, "sequence": sequence, "pr_number": pr_number, "head_sha": head_sha, "status": "IN_PROGRESS", "started_at": time.time()})
                state["next_sequence"] = sequence + 1
                self._write(state)
                return attempt

    def finish(self, attempt_id: str, status: str) -> None:
        with self._locked():
            state = self._read()
            attempts = state["attempts"]
            assert isinstance(attempts, list)
            for item in attempts:
                if isinstance(item, dict) and item.get("id") == attempt_id:
                    item["status"] = status
                    item["finished_at"] = time.time()
                    self._write(state)
                    return
            raise RuntimeError("Adversarial-validation attempt identity is unknown")

    def latest_published_sequence(self, pr_number: int, head_sha: str) -> int:
        """Return the newest start-ordered attempt with a durable verdict."""
        with self._locked():
            attempts = self._read()["attempts"]
        assert isinstance(attempts, list)
        return max(
            (int(item["sequence"]) for item in attempts if isinstance(item, dict) and item.get("pr_number") == pr_number and item.get("head_sha") == head_sha and item.get("published") is True),
            default=0,
        )

    def latest_completed_sequence(self, pr_number: int, head_sha: str) -> int:
        """Return the newest start-ordered attempt with a terminal result."""
        with self._locked():
            attempts = self._read()["attempts"]
        assert isinstance(attempts, list)
        return max(
            (int(item["sequence"]) for item in attempts if isinstance(item, dict) and item.get("pr_number") == pr_number and item.get("head_sha") == head_sha and isinstance(item.get("status"), str) and item.get("status") != "IN_PROGRESS"),
            default=0,
        )

    def mark_published(self, attempt_id: str) -> None:
        """Mark only one attempt's result as durable without touching siblings."""
        with self._locked():
            state = self._read()
            attempts = state["attempts"]
            assert isinstance(attempts, list)
            for item in attempts:
                if isinstance(item, dict) and item.get("id") == attempt_id:
                    item["published"] = True
                    self._write(state)
                    return
            raise RuntimeError("Adversarial-validation attempt identity is unknown")
