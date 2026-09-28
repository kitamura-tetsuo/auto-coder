"""Durable episodes for automatic specification repair rounds."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .runtime_locks import ensure_lock_directory, lock_path

PAUSE_REASON = "automatic_repair_paused(repair_round_limit_reached)"
_ACTIVE_INVOCATIONS: set[str] = set()
_ACTIVE_INVOCATIONS_LOCK = threading.Lock()


@dataclass(frozen=True)
class RepairRoundApplication:
    """The semantic remediation and automatic-repair authorization for a generation."""

    remediation: str
    previous_rounds: int
    reason: Optional[str] = None
    automatic_repair_authorized: bool = False
    paused: bool = False
    episode: int = 0
    operation_identity: Optional[str] = None
    observation: Optional[str] = None
    editor_error: Optional[str] = None
    invocation_in_progress: bool = False
    before_state: Optional[str] = None


@dataclass(frozen=True)
class AuthoritativeRepairState:
    """Fresh contract content plus submission and ownership authority."""

    content: str
    decision_binding: str
    submission_active: bool = True
    ownership_valid: bool = True
    manifest_valid: bool = True


def classify_repair_observation(
    before_state: AuthoritativeRepairState,
    after_state: Optional[AuthoritativeRepairState],
) -> tuple[str, Optional[str]]:
    """Classify fresh authority before comparing contract content."""
    if after_state is None:
        return "UNVERIFIED", None
    if not after_state.submission_active or not after_state.ownership_valid:
        return "SUPERSEDED", after_state.content
    if not after_state.manifest_valid:
        return "UNVERIFIED", after_state.content
    outcome = "NO_CONTRACT_CHANGE" if after_state.content == before_state.content else "CONTRACT_CHANGED"
    return outcome, after_state.content


class SpecificationRepairRoundStore:
    """Persist immutable generation-to-episode assignments and bounded rounds.

    A paused episode is never reopened.  A generation that has appeared before
    always selects its original episode, including after a later episode exists.
    """

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / repository / "specification_repair_rounds.json"
        self.repository = repository

    def apply(
        self,
        subject_kind: str,
        subject_number: int,
        generation: str,
        remediation: str,
        limit: int,
        *,
        authorize_automatic_repair: bool = False,
    ) -> RepairRoundApplication:
        """Associate a current decision, optionally authorizing its automatic repair.

        Observation/publication never consumes allowance.  The caller that is
        about to initiate an automatic contract edit must opt in; the count is
        then committed before authorization is returned.
        """
        if limit <= 0:
            raise ValueError("specification repair-round limit must be a positive integer")
        key = f"{subject_kind}:{subject_number}"
        with self._locked():
            state = self._read()
            subject = self._subject(state, key)
            associations = subject["generation_episodes"]
            episodes = subject["episodes"]
            assert isinstance(associations, dict) and isinstance(episodes, list)

            episode_number = associations.get(generation)
            changed = False
            if episode_number is None:
                if not episodes or self._episode(episodes, len(episodes))["status"] == "paused":
                    episodes.append({"status": "active", "counted_generations": [], "pause_trigger_generation": None})
                episode_number = len(episodes)
                associations[generation] = episode_number
                changed = True
            if not isinstance(episode_number, int) or episode_number < 1:
                raise ValueError(f"Invalid specification repair episode association for {key}")
            episode = self._episode(episodes, episode_number)
            counted = episode["counted_generations"]
            assert isinstance(counted, list)
            previous = len(counted)

            paused = episode["status"] == "paused"
            authorized = False
            reason: Optional[str] = PAUSE_REASON if paused and remediation == "EDIT_IN_PLACE" else None
            if remediation == "EDIT_IN_PLACE" and not paused and generation not in counted and (authorize_automatic_repair or previous >= limit):
                if previous >= limit:
                    episode["status"] = "paused"
                    episode["pause_trigger_generation"] = generation
                    paused = True
                    reason = PAUSE_REASON
                    changed = True
                else:
                    # The durable write below happens before authorization is returned.
                    counted.append(generation)
                    authorized = True
                    changed = True
            if changed:
                self._write(state)
            return RepairRoundApplication(remediation, previous, reason, authorized, paused, episode_number)

    def authorize(
        self,
        subject_kind: str,
        subject_number: int,
        generation: str,
        remediation: str,
        limit: int,
        before_state: Optional[str] = None,
    ) -> RepairRoundApplication:
        """Atomically persist the count, operation identity, and before-state."""
        if limit <= 0:
            raise ValueError("specification repair-round limit must be a positive integer")
        if before_state is None:
            return self.apply(
                subject_kind,
                subject_number,
                generation,
                remediation,
                limit,
                authorize_automatic_repair=True,
            )
        key = f"{subject_kind}:{subject_number}"
        operation_identity = f"{subject_kind}:{subject_number}:{generation}"
        with self._locked():
            state = self._read()
            subject = self._subject(state, key)
            associations = subject["generation_episodes"]
            episodes = subject["episodes"]
            assert isinstance(associations, dict) and isinstance(episodes, list)
            episode_number = associations.get(generation)
            changed = False
            if episode_number is None:
                if not episodes or self._episode(episodes, len(episodes))["status"] == "paused":
                    episodes.append({"status": "active", "counted_generations": [], "pause_trigger_generation": None})
                episode_number = len(episodes)
                associations[generation] = episode_number
                changed = True
            if not isinstance(episode_number, int):
                raise ValueError("Invalid specification repair episode association")
            episode = self._episode(episodes, episode_number)
            counted = episode["counted_generations"]
            assert isinstance(counted, list)
            previous = len(counted)
            paused = episode["status"] == "paused"
            authorized = False
            reason = PAUSE_REASON if paused and remediation == "EDIT_IN_PLACE" else None
            operations = subject.setdefault("operations", {})
            if not isinstance(operations, dict):
                raise ValueError("Invalid specification repair operations")
            operation = operations.get(generation)
            if remediation == "EDIT_IN_PLACE" and not paused and generation not in counted:
                if previous >= limit:
                    episode["status"] = "paused"
                    episode["pause_trigger_generation"] = generation
                    paused = True
                    reason = PAUSE_REASON
                    changed = True
                else:
                    operation = {
                        "operation_identity": operation_identity,
                        "before_state": before_state,
                        "phase": "AUTHORIZED",
                        "observation": None,
                        "after_state": None,
                        "editor_error": None,
                    }
                    operations[generation] = operation
                    counted.append(generation)
                    authorized = True
                    changed = True
            elif not isinstance(operation, dict):
                raise ValueError("Invalid specification repair operation")
            if changed:
                self._write(state)
            observation = operation.get("observation") if isinstance(operation, dict) else None
            editor_error = operation.get("editor_error") if isinstance(operation, dict) else None
        with _ACTIVE_INVOCATIONS_LOCK:
            if authorized:
                _ACTIVE_INVOCATIONS.add(operation_identity)
            active = operation_identity in _ACTIVE_INVOCATIONS
        return RepairRoundApplication(
            remediation,
            previous,
            reason,
            authorized,
            paused,
            episode_number,
            operation_identity,
            observation if isinstance(observation, str) else None,
            editor_error if isinstance(editor_error, str) else None,
            active and not authorized,
            operation.get("before_state") if isinstance(operation, dict) and isinstance(operation.get("before_state"), str) else None,
        )

    @staticmethod
    def finish_invocation(operation_identity: Optional[str]) -> None:
        """Release process-local live-editor ownership before observation."""
        if operation_identity is None:
            return
        with _ACTIVE_INVOCATIONS_LOCK:
            _ACTIVE_INVOCATIONS.discard(operation_identity)

    def observe(
        self,
        subject_kind: str,
        subject_number: int,
        generation: str,
        observation: str,
        after_state: Optional[str],
        editor_error: Optional[str],
    ) -> RepairRoundApplication:
        """Idempotently settle an authorized invocation from authoritative evidence."""
        key = f"{subject_kind}:{subject_number}"
        with self._locked():
            state = self._read()
            subject = self._subject(state, key)
            operations = subject.get("operations")
            operation = operations.get(generation) if isinstance(operations, dict) else None
            if not isinstance(operation, dict):
                raise ValueError("Specification repair operation is unavailable")
            existing = operation.get("observation")
            if existing not in {None, "UNVERIFIED"}:
                if existing != observation or operation.get("after_state") != after_state:
                    raise ValueError("Specification repair observation conflict")
            else:
                operation.update(
                    {
                        "phase": "OBSERVED",
                        "observation": observation,
                        "after_state": after_state,
                        "editor_error": editor_error,
                    }
                )
                self._write(state)
            episodes = subject["episodes"]
            associations = subject["generation_episodes"]
            if not isinstance(episodes, list) or not isinstance(associations, dict):
                raise ValueError("Invalid specification repair episode state")
            episode_number = associations.get(generation)
            if not isinstance(episode_number, int):
                raise ValueError("Specification repair generation is unavailable")
            episode = self._episode(episodes, episode_number)
            counted = episode["counted_generations"]
            if not isinstance(counted, list):
                raise ValueError("Invalid specification repair generation count")
            count = len(counted)
            return RepairRoundApplication(
                "EDIT_IN_PLACE",
                count,
                automatic_repair_authorized=False,
                paused=episode["status"] == "paused",
                episode=episode_number,
                operation_identity=str(operation["operation_identity"]),
                observation=str(operation["observation"]),
                editor_error=operation.get("editor_error") if isinstance(operation.get("editor_error"), str) else None,
            )

    def count(self, subject_kind: str, subject_number: int, episode: Optional[int] = None) -> int:
        raw = self._read().get(f"{subject_kind}:{subject_number}")
        if not isinstance(raw, dict):
            return 0
        # Read legacy state without mutating it.
        legacy = raw.get("edit_in_place_generations")
        if isinstance(legacy, list):
            return len(legacy)
        episodes = raw.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            return 0
        selected = episode or len(episodes)
        current = self._episode(episodes, selected)
        counted = current.get("counted_generations")
        return len(counted) if isinstance(counted, list) else 0

    def is_paused(self, subject_kind: str, subject_number: int, generation: str) -> bool:
        raw = self._read().get(f"{subject_kind}:{subject_number}")
        if not isinstance(raw, dict):
            return False
        associations, episodes = raw.get("generation_episodes"), raw.get("episodes")
        selected = associations.get(generation) if isinstance(associations, dict) else None
        return isinstance(selected, int) and isinstance(episodes, list) and self._episode(episodes, selected).get("status") == "paused"

    def _subject(self, state: dict[str, object], key: str) -> dict[str, object]:
        raw = state.get(key)
        if raw is None:
            raw = {"version": 2, "episodes": [], "generation_episodes": {}}
            state[key] = raw
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid specification repair-round state for {key}")
        legacy = raw.get("edit_in_place_generations")
        if isinstance(legacy, list):
            if any(not isinstance(item, str) for item in legacy):
                raise ValueError(f"Invalid specification repair-round generations for {key}")
            raw.clear()
            raw.update(
                {
                    "version": 2,
                    "episodes": [{"status": "active", "counted_generations": legacy, "pause_trigger_generation": None}],
                    "generation_episodes": {item: 1 for item in legacy},
                }
            )
        if not isinstance(raw.get("episodes"), list) or not isinstance(raw.get("generation_episodes"), dict):
            raise ValueError(f"Invalid specification repair episode state for {key}")
        return raw

    @staticmethod
    def _episode(episodes: list[object], number: int) -> dict[str, object]:
        if number > len(episodes) or not isinstance(episodes[number - 1], dict):
            raise ValueError("Invalid specification repair episode")
        episode = episodes[number - 1]
        assert isinstance(episode, dict)
        if episode.get("status") not in {"active", "paused"} or not isinstance(episode.get("counted_generations"), list):
            raise ValueError("Invalid specification repair episode")
        return episode

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        runtime_path = lock_path(self.repository, self.path, "specification-repair-rounds")
        ensure_lock_directory(runtime_path)
        with runtime_path.open("a", encoding="utf-8") as stream:
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
