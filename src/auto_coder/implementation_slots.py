"""Durable ownership and serialization for logical implementations."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .issue_context import resolve_issue_oracles
from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ImplementationOwner:
    """Stable identity of an Issue-rooted or standalone-PR implementation."""

    kind: str
    number: int

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.number}"


class ImplementationSlotUnavailable(RuntimeError):
    """Raised when an independent implementation cannot reserve a slot."""


class ImplementationOwnerResolutionError(RuntimeError):
    """Raised when resolving a PR owner is uncertain and must fail closed."""


class ImplementationSlotRepository:
    """Persist active owners and atomically reserve capacity across processes."""

    def __init__(self, repo_name: str, max_implementations: int, storage_path: Optional[Path] = None):
        if isinstance(max_implementations, bool) or max_implementations < 1:
            raise ValueError("max_concurrent_implementations must be a positive integer")
        self.repo_name = repo_name
        self.max_implementations = max_implementations
        runtime_root = os.environ.get("AUTO_CODER_RUNTIME_ROOT")
        default_root = Path(runtime_root) / "state" if runtime_root else Path.home() / ".auto-coder"
        self.storage_path = storage_path or default_root / repo_name / "implementation_slots.json"
        self.lock_path = self.storage_path.with_suffix(".lock")
        self._thread_lock = threading.RLock()
        self._owner_locks: Dict[str, threading.RLock] = {}
        self._serialization_depth = threading.local()
        self._execution_context = threading.local()

    def resolve_owner(self, candidate_type: str, data: Dict[str, Any], github_client: Any) -> ImplementationOwner:
        number = data.get("number")
        if not isinstance(number, int):
            raise ImplementationOwnerResolutionError("Candidate has no valid numeric identity")
        if candidate_type == "issue":
            return ImplementationOwner("issue", number)
        if candidate_type != "pr":
            raise ImplementationOwnerResolutionError(f"Unsupported candidate type: {candidate_type}")

        resolution = resolve_issue_oracles(github_client, self.repo_name, pr_data=data)
        if resolution.error:
            raise ImplementationOwnerResolutionError(resolution.error)
        if resolution.issues:
            # A PR can mention several Issues, but the authoritative/root owner is
            # the first relationship selected by the shared hierarchical resolver.
            return ImplementationOwner("issue", resolution.issues[0].number)
        provider_owner = self._provider_owner_for_pr(number, data.get("body", ""))
        if provider_owner is not None:
            return provider_owner
        return ImplementationOwner("pr", number)

    def _provider_owner_for_pr(self, pr_number: int, body: object) -> Optional[ImplementationOwner]:
        """Resolve a source-less PR from durable provider-run membership."""
        session_ids: set[str] = set()
        if isinstance(body, str):
            session_ids.update(re.findall(r"jules\.google\.com/(?:session|task)/([A-Za-z0-9_-]+)", body))
            session_ids.update(re.findall(r"\bSession ID:\s*([A-Za-z0-9_-]+)", body, re.IGNORECASE))
        with self._state_lock():
            records = self._read()
        for key, record in records.items():
            kind = record.get("kind")
            number = record.get("number")
            implementation_prs = record.get("implementation_prs", [])
            provider_sessions = record.get("provider_sessions", [])
            if not isinstance(implementation_prs, list) or any(isinstance(value, bool) or not isinstance(value, int) for value in implementation_prs) or not isinstance(provider_sessions, list) or any(not isinstance(value, str) for value in provider_sessions):
                raise ImplementationSlotUnavailable("Cannot safely parse provider implementation membership")
            if pr_number in implementation_prs or session_ids.intersection(provider_sessions):
                if not isinstance(kind, str) or isinstance(number, bool) or not isinstance(number, int):
                    raise ImplementationSlotUnavailable(f"Cannot safely parse implementation owner {key}")
                return ImplementationOwner(kind, number)
        return None

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            with io.open(self.lock_path, "a+", encoding="utf-8") as lock_file:
                os.chmod(self.lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> Dict[str, Dict[str, object]]:
        if not self.storage_path.exists():
            return {}
        try:
            with io.open(self.storage_path, encoding="utf-8") as state_file:
                value = json.load(state_file)
            if not isinstance(value, dict):
                raise ValueError("slot state root must be an object")
            return value
        except Exception as exc:
            # Corrupt/unreadable state cannot safely be interpreted as empty.
            raise ImplementationSlotUnavailable(f"Cannot safely read implementation slot state: {exc}") from exc

    def _write(self, owners: Dict[str, Dict[str, object]]) -> None:
        temporary = self.storage_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as state_file:
            json.dump(owners, state_file, indent=2, sort_keys=True)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, self.storage_path)

    @staticmethod
    def _capacity_usage(owners: Dict[str, Dict[str, object]]) -> tuple[int, bool]:
        """Return normal slot usage and whether the durable emergency lane is held."""
        normal = 0
        emergency_in_use = False
        for record in owners.values():
            emergency = record.get("emergency", False)
            if not isinstance(emergency, bool):
                raise ImplementationSlotUnavailable("Cannot safely parse implementation slot capacity class")
            if emergency:
                if emergency_in_use:
                    raise ImplementationSlotUnavailable("Implementation slot state contains multiple emergency owners")
                emergency_in_use = True
            else:
                normal += 1
        return normal, emergency_in_use

    def reserve(self, owner: ImplementationOwner, implementation_pr: Optional[int] = None) -> bool:
        """Reserve *owner* and durably record a PR known to belong to it."""
        if implementation_pr is not None and (owner.kind == "pr" or isinstance(implementation_pr, bool) or not isinstance(implementation_pr, int)):
            raise ValueError("implementation_pr must identify a PR belonging to a non-PR implementation owner")
        with self._state_lock():
            owners = self._read()
            if owner.key in owners:
                if implementation_pr is not None:
                    record = owners[owner.key]
                    known_prs = record.setdefault("implementation_prs", [])
                    if not isinstance(known_prs, list):
                        raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
                    if implementation_pr not in known_prs:
                        known_prs.append(implementation_pr)
                        self._write(owners)
                return True
            normal_usage, _ = self._capacity_usage(owners)
            if normal_usage >= self.max_implementations:
                return False
            owners[owner.key] = {
                "kind": owner.kind,
                "number": owner.number,
                "implementation_prs": [implementation_pr] if implementation_pr is not None else [],
                "provider_sessions": [],
            }
            self._write(owners)
            return True

    def reserve_new(self, owner: ImplementationOwner) -> bool:
        """Atomically reserve *owner* only when it is not already active."""
        with self._state_lock():
            owners = self._read()
            normal_usage, _ = self._capacity_usage(owners)
            if owner.key in owners or normal_usage >= self.max_implementations:
                return False
            owners[owner.key] = {
                "kind": owner.kind,
                "number": owner.number,
                "implementation_prs": [],
                "provider_sessions": [],
            }
            self._write(owners)
            return True

    def start_execution(
        self,
        owner: ImplementationOwner,
        *,
        implementation_pr: Optional[int] = None,
        bypass_capacity: bool = False,
        bypass_active_execution: bool = False,
        allow_urgent_emergency: bool = False,
    ) -> Optional[str]:
        """Atomically admit and durably identify one mutating execution.

        Capacity and duplicate-execution admission are deliberately independent:
        explicit ``--only`` work may bypass the former, while only the additional
        operator ``--force`` flag may bypass the latter.
        """
        if implementation_pr is not None and (owner.kind == "pr" or isinstance(implementation_pr, bool) or not isinstance(implementation_pr, int)):
            raise ValueError("implementation_pr must identify a PR belonging to a non-PR implementation owner")
        execution_id = uuid.uuid4().hex
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                normal_usage, emergency_in_use = self._capacity_usage(owners)
                use_emergency = False
                if normal_usage >= self.max_implementations and not bypass_capacity:
                    if not allow_urgent_emergency or emergency_in_use:
                        return None
                    use_emergency = True
                record = {
                    "kind": owner.kind,
                    "number": owner.number,
                    "implementation_prs": [],
                    "provider_sessions": [],
                    "executions": [],
                    "emergency": use_emergency,
                }
                owners[owner.key] = record
            executions = record.setdefault("executions", [])
            if not isinstance(executions, list) or any(not isinstance(value, dict) or not isinstance(value.get("id"), str) for value in executions):
                raise ImplementationSlotUnavailable("Cannot safely parse active implementation executions")
            if executions and not bypass_active_execution:
                return None
            known_prs = record.setdefault("implementation_prs", [])
            if not isinstance(known_prs, list):
                raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
            if implementation_pr is not None and implementation_pr not in known_prs:
                known_prs.append(implementation_pr)
            executions.append({"id": execution_id, "pid": os.getpid(), "started_at": time.time()})
            self._write(owners)
        active = getattr(self._execution_context, "owners", {})
        active[owner.key] = execution_id
        self._execution_context.owners = active
        return execution_id

    def finish_execution(self, owner: ImplementationOwner, execution_id: str) -> None:
        """Remove only the named execution, preserving its owner and siblings."""
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return
            executions = record.setdefault("executions", [])
            if not isinstance(executions, list) or any(not isinstance(value, dict) or not isinstance(value.get("id"), str) for value in executions):
                raise ImplementationSlotUnavailable("Cannot safely parse active implementation executions")
            remaining = [value for value in executions if value["id"] != execution_id]
            if len(remaining) != len(executions):
                record["executions"] = remaining
                self._write(owners)
        active = getattr(self._execution_context, "owners", {})
        if active.get(owner.key) == execution_id:
            active.pop(owner.key, None)

    def current_execution_id(self, owner: ImplementationOwner) -> Optional[str]:
        """Return this thread's enclosing execution for reentrant lifecycle work."""
        return getattr(self._execution_context, "owners", {}).get(owner.key)

    def active_execution_ids(self, owner: ImplementationOwner) -> tuple[str, ...]:
        """Return durable active execution identities for an owner."""
        with self._state_lock():
            record = self._read().get(owner.key)
        if record is None:
            return ()
        executions = record.get("executions", [])
        if not isinstance(executions, list) or any(not isinstance(value, dict) or not isinstance(value.get("id"), str) for value in executions):
            raise ImplementationSlotUnavailable("Cannot safely parse active implementation executions")
        return tuple(value["id"] for value in executions)

    def release(self, owner: ImplementationOwner) -> None:
        with self._state_lock():
            owners = self._read()
            if owners.pop(owner.key, None) is not None:
                self._write(owners)

    def record_implementation_pr(self, owner: ImplementationOwner, pr_number: int) -> bool:
        """Record PR membership only when *owner* is already reserved."""
        if isinstance(pr_number, bool) or not isinstance(pr_number, int):
            raise ValueError("pr_number must identify a PR belonging to an implementation owner")
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            known_prs = record.setdefault("implementation_prs", [])
            if not isinstance(known_prs, list):
                raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
            if pr_number not in known_prs:
                known_prs.append(pr_number)
                self._write(owners)
            return True

    def record_provider_session(self, owner: ImplementationOwner, session_id: str) -> bool:
        """Persist a provider session as evidence of logical ownership."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            provider_sessions = record.setdefault("provider_sessions", [])
            if not isinstance(provider_sessions, list):
                raise ImplementationSlotUnavailable("Cannot safely parse provider session membership")
            if session_id not in provider_sessions:
                provider_sessions.append(session_id)
                self._write(owners)
            return True

    def active_owners(self) -> tuple[ImplementationOwner, ...]:
        with self._state_lock():
            records = self._read()
        owners = []
        for record in records.values():
            kind = record.get("kind")
            number = record.get("number")
            if not isinstance(kind, str) or isinstance(number, bool) or not isinstance(number, int):
                raise ImplementationSlotUnavailable("Cannot safely parse implementation slot state")
            owners.append(ImplementationOwner(kind, number))
        return tuple(owners)

    def _record_open_pr_memberships(self, github_client: Any) -> None:
        """Persist supported PR ownership oracles before startup reconciliation."""
        active_owner_keys = {owner.key for owner in self.active_owners()}
        for pull_request in github_client.get_open_pull_requests(self.repo_name):
            owner = self.resolve_owner("pr", pull_request, github_client)
            pr_number = pull_request.get("number")
            # Every non-PR owner can have implementation PRs.  This includes
            # provider-owned recurrent runs as well as Issue-owned work.
            if owner.kind == "pr" or owner.key not in active_owner_keys:
                continue
            if isinstance(pr_number, bool) or not isinstance(pr_number, int):
                raise ImplementationOwnerResolutionError("Open pull request has no valid numeric identity")
            if not self.record_implementation_pr(owner, pr_number):
                raise ImplementationSlotUnavailable(f"Could not retain active implementation owner {owner.key}")

    def reconcile(self, github_client: Any, discover_open_prs: bool = False) -> None:
        """Release only owners whose complete authoritative lifecycle is terminal."""
        if discover_open_prs:
            try:
                self._record_open_pr_memberships(github_client)
            except Exception as exc:
                # Startup has not yet discovered candidates. If the complete
                # open-PR set or any supported ownership oracle is unavailable,
                # no existing reservation can safely be declared terminal.
                logger.warning(f"Could not discover open implementation PRs; retaining all slots: {exc}")
                return
        for owner in self.active_owners():
            try:
                # Lifecycle reconciliation must never erase live execution
                # identities, including forced siblings of the execution that
                # just completed.
                if self.active_execution_ids(owner):
                    continue
                if owner.kind == "issue":
                    item = github_client.get_issue(self.repo_name, owner.number)
                    details = github_client.get_issue_details(item)
                    if details.get("state", "").lower() != "closed":
                        continue

                    # Closing the source Issue is not terminal while any PR in
                    # the same logical implementation is still active (for
                    # example, a sibling PR waiting for CI or review).  Use the
                    # existing authoritative Issue-to-PR relationship lookup;
                    # an unavailable lookup raises and retains the slot.
                    # Timeline relationships are not the only supported ownership
                    # oracle: branch metadata can also associate a PR with its
                    # source Issue.  PR membership learned during owner resolution
                    # is therefore persisted with the slot and reconciled together
                    # with timeline relationships after a restart.
                    with self._state_lock():
                        record = self._read().get(owner.key, {})
                        recorded_prs = record.get("implementation_prs", [])
                    if not isinstance(recorded_prs, list) or any(isinstance(number, bool) or not isinstance(number, int) for number in recorded_prs):
                        raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
                    linked_pr_numbers = set(github_client.get_linked_prs(self.repo_name, owner.number, strict=True))
                    linked_pr_numbers.update(recorded_prs)
                    linked_prs_terminal = True
                    for pr_number in linked_pr_numbers:
                        pull_request = github_client.get_pull_request(self.repo_name, pr_number)
                        pr_details = github_client.get_pr_details(pull_request)
                        if pr_details.get("state", "").lower() != "closed" and pr_details.get("merged") is not True:
                            linked_prs_terminal = False
                            break
                    if linked_prs_terminal:
                        self.release(owner)
                        logger.info(f"Released terminal logical implementation slot {owner.key}")
                elif owner.kind == "pr":
                    item = github_client.get_pull_request(self.repo_name, owner.number)
                    details = github_client.get_pr_details(item)
                    if details.get("state", "").lower() == "closed" or details.get("merged") is True:
                        self.release(owner)
                        logger.info(f"Released terminal logical implementation slot {owner.key}")
                else:
                    # Provider-owned implementations are reconciled by their
                    # provider lifecycle scanner. Uncertain lifecycle evidence
                    # must retain capacity rather than be treated as a PR.
                    continue
            except Exception as exc:
                logger.warning(f"Could not reconcile logical implementation {owner.key}; retaining its slot: {exc}")

    @contextmanager
    def serialize(self, owner: ImplementationOwner) -> Iterator[None]:
        """Prevent simultaneous mutation paths for one owner across processes."""
        with self._thread_lock:
            owner_lock = self._owner_locks.setdefault(owner.key, threading.RLock())
        with owner_lock:
            depths = getattr(self._serialization_depth, "owners", {})
            depth = depths.get(owner.key, 0)
            depths[owner.key] = depth + 1
            self._serialization_depth.owners = depths
            if depth:
                try:
                    yield
                finally:
                    depths[owner.key] -= 1
                return

            mutation_lock_path = self.storage_path.parent / f"implementation-{owner.kind}-{owner.number}.lock"
            mutation_lock_path.parent.mkdir(parents=True, exist_ok=True)
            with io.open(mutation_lock_path, "a+", encoding="utf-8") as lock_file:
                os.chmod(mutation_lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    depths.pop(owner.key, None)
