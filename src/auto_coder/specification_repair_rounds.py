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


@dataclass(frozen=True)
class RepairRoundApplication:
    """The semantic remediation and automatic-repair authorization for a generation."""

    remediation: str
    previous_rounds: int
    reason: Optional[str] = None
    automatic_repair_authorized: bool = False
    paused: bool = False
    episode: int = 0


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

    def authorize(self, subject_kind: str, subject_number: int, generation: str, remediation: str, limit: int) -> RepairRoundApplication:
        """Durably count, then authorize, one automatic in-place repair."""
        return self.apply(
            subject_kind,
            subject_number,
            generation,
            remediation,
            limit,
            authorize_automatic_repair=True,
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
