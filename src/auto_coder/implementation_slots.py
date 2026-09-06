"""Durable ownership and serialization for logical implementations."""

from __future__ import annotations

import copy
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
from typing import Any, Dict, Iterator, NoReturn, Optional

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


class ImplementationHierarchyConflict(RuntimeError):
    """Raised when a direct parent or child already owns implementation state."""


class ImplementationHierarchyUnavailable(RuntimeError):
    """Raised when current direct hierarchy evidence cannot authorize admission."""


class ImplementationOwnerResolutionError(RuntimeError):
    """Raised when resolving a PR owner is uncertain and must fail closed."""


@dataclass(frozen=True)
class ProcessIdentity:
    """OS identity that distinguishes a process from a reused numeric PID."""

    pid: int
    boot_id: str
    start_ticks: int
    state: str


class ImplementationSlotRepository:
    """Persist active owners and atomically reserve capacity across processes."""

    _SHARED_FILE_MODE = 0o660

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
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._raise_permission_error(self.storage_path.parent, exc)
        with self._thread_lock:
            try:
                lock_fd = self._open_lock_file(self.lock_path)
            except OSError as exc:
                self._raise_permission_error(self.lock_path, exc)
            with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock_file:
                self._establish_shared_permissions(lock_file.fileno(), self.lock_path)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    self._raise_permission_error(self.lock_path, exc)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _open_lock_file(self, lock_path: Path) -> int:
        """Open the lock, atomically publishing fully prepared metadata if new."""
        try:
            return os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            pass

        bootstrap = lock_path.with_name(f".{lock_path.name}.{uuid.uuid4().hex}.tmp")
        bootstrap_fd = os.open(bootstrap, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self._establish_shared_permissions(bootstrap_fd, bootstrap)
            try:
                os.link(bootstrap, lock_path)
            except FileExistsError:
                pass
        finally:
            os.close(bootstrap_fd)
            try:
                os.unlink(bootstrap)
            except FileNotFoundError:
                pass
        return os.open(lock_path, os.O_RDWR)

    def _read(self) -> Dict[str, Dict[str, object]]:
        try:
            os.stat(self.storage_path)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            self._raise_permission_error(self.storage_path, exc)
        try:
            with io.open(self.storage_path, "r", encoding="utf-8") as state_file:
                value = json.load(state_file)
            if not isinstance(value, dict):
                raise ValueError("slot state root must be an object")
            return value
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise ImplementationSlotUnavailable(f"Cannot safely parse implementation slot state at '{self.storage_path}': {exc}") from exc
        except OSError as exc:
            self._raise_permission_error(self.storage_path, exc)

    def _write(self, owners: Dict[str, Dict[str, object]]) -> None:
        temporary = self.storage_path.with_suffix(".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, self._SHARED_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                self._establish_shared_permissions(state_file.fileno(), temporary)
                json.dump(owners, state_file, indent=2, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.storage_path)
        except OSError as exc:
            self._raise_permission_error(temporary if temporary.exists() else self.storage_path, exc)

    def _establish_shared_permissions(self, fd: int, path: Path) -> None:
        """Apply the containing directory's group and a non-world shared mode."""
        try:
            directory_gid = os.stat(self.storage_path.parent).st_gid
            metadata = os.fstat(fd)
            if metadata.st_gid != directory_gid:
                os.fchown(fd, -1, directory_gid)
            if metadata.st_mode & 0o777 != self._SHARED_FILE_MODE:
                os.fchmod(fd, self._SHARED_FILE_MODE)
        except OSError as exc:
            self._raise_permission_error(path, exc)

    @staticmethod
    def _raise_permission_error(path: Path, exc: OSError) -> NoReturn:
        raise ImplementationSlotUnavailable(f"Cannot safely establish or use implementation slot shared-state permissions for '{path}': {exc}") from exc

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

    def available_normal_slots(self) -> int:
        """Return normal capacity from the current cross-process state."""
        with self._state_lock():
            normal_usage, _ = self._capacity_usage(self._read())
        return max(0, self.max_implementations - normal_usage)

    def normal_capacity_snapshot(self) -> tuple[int, tuple[int, int]]:
        """Return capacity and an identity that changes on every state replacement."""
        with self._state_lock():
            normal_usage, _ = self._capacity_usage(self._read())
            try:
                state = self.storage_path.stat()
                identity = (state.st_ino, state.st_mtime_ns)
            except FileNotFoundError:
                identity = (0, 0)
        return max(0, self.max_implementations - normal_usage), identity

    def start_execution(
        self,
        owner: ImplementationOwner,
        *,
        implementation_pr: Optional[int] = None,
        bypass_capacity: bool = False,
        bypass_active_execution: bool = False,
        allow_urgent_emergency: bool = False,
        github_client: Optional[Any] = None,
    ) -> Optional[str]:
        """Atomically admit and durably identify one mutating execution.

        Capacity and duplicate-execution admission are deliberately independent:
        explicit ``--only`` work may bypass the former, while only the additional
        operator ``--force`` flag may bypass the latter.
        """
        if implementation_pr is not None and (owner.kind == "pr" or isinstance(implementation_pr, bool) or not isinstance(implementation_pr, int)):
            raise ValueError("implementation_pr must identify a PR belonging to a non-PR implementation owner")
        execution_id = uuid.uuid4().hex
        process_identity = self._current_process_identity()
        with self._state_lock():
            owners = self._read()
            admission_hierarchy: Optional[tuple[Optional[int], tuple[int, ...]]] = None
            reclaimed = self._remove_stale_executions(owners)
            if reclaimed:
                # Persist cleanup even when capacity or duplicate admission
                # returns early below.
                self._write(owners)
            record = owners.get(owner.key)
            established_record: Optional[Dict[str, object]] = None
            if record is not None and record.get("admission_pending", False):
                established = record.get("admission_established", False)
                if not isinstance(established, bool):
                    raise ImplementationHierarchyUnavailable(f"Interrupted hierarchy admission for {owner.key} has invalid establishment state")
                # Every pre-existing logical owner is authoritative lifecycle
                # ownership. The establishment marker describes admission
                # progress; it never licenses this attempt to retire the record.
                established_record = copy.deepcopy(record)
                if owner.kind != "issue" or github_client is None:
                    raise ImplementationHierarchyUnavailable(f"Interrupted hierarchy admission for {owner.key} requires authoritative GitHub evidence")
                admission_hierarchy = self._parse_stored_hierarchy(record)
            if record is None:
                if owner.kind == "issue":
                    if github_client is not None:
                        first_hierarchy = self._read_issue_hierarchy(github_client, owner.number)
                        second_hierarchy = self._read_issue_hierarchy(github_client, owner.number)
                        if first_hierarchy != second_hierarchy:
                            raise ImplementationHierarchyUnavailable(f"Direct hierarchy changed while admitting issue:{owner.number}")
                        self._raise_hierarchy_conflict(owner, owners, second_hierarchy)
                        admission_hierarchy = second_hierarchy
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
                if admission_hierarchy is not None:
                    record["admission_pending"] = True
                    record["admission_hierarchy"] = {
                        "parent": admission_hierarchy[0],
                        "children": list(admission_hierarchy[1]),
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
            execution = {"id": execution_id, "pid": os.getpid(), "started_at": time.time()}
            if process_identity is not None:
                execution.update({"boot_id": process_identity.boot_id, "process_start_ticks": process_identity.start_ticks})
            executions.append(execution)
            self._write(owners)
            if admission_hierarchy is not None:
                try:
                    committed_hierarchy = self._read_issue_hierarchy(github_client, owner.number)
                    self._raise_hierarchy_conflict(owner, owners, committed_hierarchy)
                    if committed_hierarchy != admission_hierarchy:
                        raise ImplementationHierarchyUnavailable(f"Direct hierarchy changed while committing issue:{owner.number}")
                except (ImplementationHierarchyConflict, ImplementationHierarchyUnavailable):
                    if established_record is None:
                        owners.pop(owner.key, None)
                    else:
                        owners[owner.key] = established_record
                    self._write(owners)
                    raise
        active = getattr(self._execution_context, "owners", {})
        active[owner.key] = execution_id
        self._execution_context.owners = active
        return execution_id

    @staticmethod
    def _parse_stored_hierarchy(record: Dict[str, object]) -> tuple[Optional[int], tuple[int, ...]]:
        evidence = record.get("admission_hierarchy")
        if not isinstance(evidence, dict):
            raise ImplementationHierarchyUnavailable("Interrupted admission has no valid durable hierarchy evidence")
        parent = evidence.get("parent")
        children = evidence.get("children")
        if parent is not None and (isinstance(parent, bool) or not isinstance(parent, int)):
            raise ImplementationHierarchyUnavailable("Interrupted admission has an invalid durable parent identity")
        if not isinstance(children, list) or any(isinstance(number, bool) or not isinstance(number, int) for number in children):
            raise ImplementationHierarchyUnavailable("Interrupted admission has invalid durable child membership")
        if len(children) != len(set(children)):
            raise ImplementationHierarchyUnavailable("Interrupted admission has duplicate durable child membership")
        return parent, tuple(children)

    @staticmethod
    def _raise_hierarchy_conflict(
        owner: ImplementationOwner,
        owners: Dict[str, Dict[str, object]],
        hierarchy: tuple[Optional[int], tuple[int, ...]],
    ) -> None:
        related = set(hierarchy[1])
        if hierarchy[0] is not None:
            related.add(hierarchy[0])
        conflicts = sorted(number for number in related if number != owner.number and f"issue:{number}" in owners)
        if conflicts:
            raise ImplementationHierarchyConflict(f"issue:{owner.number} conflicts with active direct parent/child issue:{conflicts[0]}")

    def _read_issue_hierarchy(self, github_client: Any, issue_number: int) -> tuple[Optional[int], tuple[int, ...]]:
        """Read and strictly validate cache-bypassing direct hierarchy evidence."""
        parent_reader = getattr(github_client, "get_parent_issue_number_strict", None)
        child_reader = getattr(github_client, "get_direct_sub_issues_strict", None)
        if not callable(parent_reader) or not callable(child_reader):
            raise ImplementationHierarchyUnavailable("Cache-bypassing GitHub parent and direct-child readers are required")
        try:
            parent = parent_reader(self.repo_name, issue_number)
            children_payload = child_reader(self.repo_name, issue_number)
            confirmed_parent = parent_reader(self.repo_name, issue_number)
            confirmed_children_payload = child_reader(self.repo_name, issue_number)
            final_parent = parent_reader(self.repo_name, issue_number)
            final_children_payload = child_reader(self.repo_name, issue_number)
        except Exception as exc:
            raise ImplementationHierarchyUnavailable(f"Cannot establish current direct hierarchy for issue:{issue_number}: {exc}") from exc
        if parent is not None and (isinstance(parent, bool) or not isinstance(parent, int)):
            raise ImplementationHierarchyUnavailable("GitHub returned an invalid direct parent identity")
        if confirmed_parent is not None and (isinstance(confirmed_parent, bool) or not isinstance(confirmed_parent, int)):
            raise ImplementationHierarchyUnavailable("GitHub returned an invalid confirmed direct parent identity")
        if final_parent is not None and (isinstance(final_parent, bool) or not isinstance(final_parent, int)):
            raise ImplementationHierarchyUnavailable("GitHub returned an invalid final direct parent identity")
        if confirmed_parent != parent:
            raise ImplementationHierarchyUnavailable(f"Direct parent changed while reading hierarchy for issue:{issue_number}")
        if final_parent != confirmed_parent:
            raise ImplementationHierarchyUnavailable(f"Direct parent changed during final hierarchy confirmation for issue:{issue_number}")
        if parent == issue_number or not isinstance(children_payload, list) or not isinstance(confirmed_children_payload, list) or not isinstance(final_children_payload, list):
            raise ImplementationHierarchyUnavailable("GitHub returned contradictory direct hierarchy evidence")
        children = self._parse_direct_children(issue_number, children_payload)
        confirmed_children = self._parse_direct_children(issue_number, confirmed_children_payload)
        if confirmed_children != children:
            raise ImplementationHierarchyUnavailable(f"Direct children changed while reading hierarchy for issue:{issue_number}")
        final_children = self._parse_direct_children(issue_number, final_children_payload)
        if final_children != confirmed_children:
            raise ImplementationHierarchyUnavailable(f"Direct children changed during final hierarchy confirmation for issue:{issue_number}")
        return parent, children

    @staticmethod
    def _parse_direct_children(issue_number: int, payload: list[object]) -> tuple[int, ...]:
        children: list[int] = []
        for child in payload:
            number = child.get("number") if isinstance(child, dict) else None
            if isinstance(number, bool) or not isinstance(number, int) or number == issue_number:
                raise ImplementationHierarchyUnavailable("GitHub returned an invalid direct child identity")
            children.append(number)
        if len(children) != len(set(children)):
            raise ImplementationHierarchyUnavailable("GitHub returned duplicate direct-child membership")
        return tuple(sorted(children))

    @staticmethod
    def _read_process_identity(pid: int) -> Optional[ProcessIdentity]:
        """Read a Linux process identity, returning ``None`` when it is uncertain.

        The kernel boot ID and ``/proc/<pid>/stat`` start time together remain
        stable for a process lifetime and prevent a reused PID from being
        mistaken for the execution which originally wrote the record.
        """
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            closing_parenthesis = stat.rfind(")")
            if not boot_id or closing_parenthesis < 0:
                return None
            fields_after_name = stat[closing_parenthesis + 2 :].split()
            # The suffix begins at field 3 (state); process start time is field 22.
            state = fields_after_name[0]
            start_ticks = int(fields_after_name[19])
            if len(state) != 1:
                return None
            return ProcessIdentity(pid=pid, boot_id=boot_id, start_ticks=start_ticks, state=state)
        except (OSError, UnicodeError, ValueError, IndexError):
            return None

    def _current_process_identity(self) -> Optional[ProcessIdentity]:
        return self._read_process_identity(os.getpid())

    def _execution_is_stale(self, execution: Dict[str, object]) -> bool:
        """Return true only when strong OS identity proves an execution is dead."""
        pid = execution.get("pid")
        boot_id = execution.get("boot_id")
        start_ticks = execution.get("process_start_ticks")
        if isinstance(pid, bool) or not isinstance(pid, int) or not isinstance(boot_id, str) or isinstance(start_ticks, bool) or not isinstance(start_ticks, int):
            return False
        current = self._read_process_identity(pid)
        if current is not None:
            identity_changed = current.boot_id != boot_id or current.start_ticks != start_ticks
            # Zombie and dead tasks retain a procfs identity until their parent
            # reaps them, but cannot execute and are conclusively no longer live.
            return identity_changed or current.state in {"Z", "X", "x"}
        try:
            current_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            process_exists = Path(f"/proc/{pid}").exists()
        except OSError:
            return False
        # A changed boot is conclusive. Within the same boot, an absent procfs
        # entry is conclusive; an existing but unreadable entry is uncertain.
        return bool(current_boot_id) and (current_boot_id != boot_id or not process_exists)

    def _remove_stale_executions(self, owners: Dict[str, Dict[str, object]]) -> tuple[str, ...]:
        """Remove only executions conclusively shown stale from locked state."""
        removed = []
        for owner_key, record in owners.items():
            executions = record.setdefault("executions", [])
            if not isinstance(executions, list) or any(not isinstance(value, dict) or not isinstance(value.get("id"), str) for value in executions):
                raise ImplementationSlotUnavailable("Cannot safely parse active implementation executions")
            remaining = []
            for execution in executions:
                if self._execution_is_stale(execution):
                    removed.append(str(execution["id"]))
                    logger.info(f"Reclaimed stale implementation execution {execution['id']} for {owner_key}")
                else:
                    remaining.append(execution)
            record["executions"] = remaining
        return tuple(removed)

    def reclaim_stale_executions(self) -> tuple[str, ...]:
        """Atomically reclaim executions whose strong process identity is dead."""
        with self._state_lock():
            owners = self._read()
            removed = self._remove_stale_executions(owners)
            if removed:
                self._write(owners)
            return removed

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
                if record.get("admission_pending", False):
                    record["admission_established"] = True
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

    def _release_if_idle(self, owner: ImplementationOwner, expected_implementation_prs: Optional[tuple[int, ...]] = None) -> bool:
        """Release an owner only if relevant durable state is still unchanged.

        Lifecycle evidence is necessarily fetched without holding the registry
        lock. Admission or PR discovery may therefore race that lookup; the final
        state checks and removal must be one registry transaction so newly
        admitted work can never be discarded with its owner.
        """
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            executions = record.get("executions", [])
            if not isinstance(executions, list) or any(not isinstance(value, dict) or not isinstance(value.get("id"), str) for value in executions):
                raise ImplementationSlotUnavailable("Cannot safely parse active implementation executions")
            if executions:
                return False
            if expected_implementation_prs is not None:
                implementation_prs = record.get("implementation_prs", [])
                if not isinstance(implementation_prs, list) or any(isinstance(number, bool) or not isinstance(number, int) for number in implementation_prs):
                    raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
                if tuple(implementation_prs) != expected_implementation_prs:
                    return False
            owners.pop(owner.key)
            self._write(owners)
            return True

    def record_implementation_pr(self, owner: ImplementationOwner, pr_number: int) -> bool:
        """Record PR membership only when *owner* is already reserved."""
        if isinstance(pr_number, bool) or not isinstance(pr_number, int):
            raise ValueError("pr_number must identify a PR belonging to an implementation owner")
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            changed = False
            known_prs = record.setdefault("implementation_prs", [])
            if not isinstance(known_prs, list):
                raise ImplementationSlotUnavailable("Cannot safely parse implementation slot PR membership")
            if pr_number not in known_prs:
                known_prs.append(pr_number)
                changed = True
            if record.get("admission_pending", False) and not record.get("admission_established", False):
                record["admission_established"] = True
                changed = True
            if changed:
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
            changed = False
            provider_sessions = record.setdefault("provider_sessions", [])
            if not isinstance(provider_sessions, list):
                raise ImplementationSlotUnavailable("Cannot safely parse provider session membership")
            if session_id not in provider_sessions:
                provider_sessions.append(session_id)
                changed = True
            if record.get("admission_pending", False) and not record.get("admission_established", False):
                record["admission_established"] = True
                changed = True
            if changed:
                self._write(owners)
        return True

    def has_provider_sessions(self, owner: ImplementationOwner) -> bool:
        """Return whether logical ownership includes asynchronous provider work."""
        with self._state_lock():
            record = self._read().get(owner.key)
        if record is None:
            return False
        sessions = record.get("provider_sessions", [])
        if not isinstance(sessions, list) or any(not isinstance(value, str) for value in sessions):
            raise ImplementationSlotUnavailable("Cannot safely parse provider implementation membership")
        return bool(sessions)

    def finish_provider_session(self, owner: ImplementationOwner, session_id: str) -> bool:
        """Remove stopped remote work and release its idle logical ownership."""
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            sessions = record.get("provider_sessions", [])
            executions = record.get("executions", [])
            implementation_prs = record.get("implementation_prs", [])
            if not isinstance(sessions, list) or any(not isinstance(value, str) for value in sessions):
                raise ImplementationSlotUnavailable("Cannot safely parse provider implementation membership")
            if session_id not in sessions:
                return False
            remaining = [value for value in sessions if value != session_id]
            record["provider_sessions"] = remaining
            if not remaining and not executions and not implementation_prs:
                owners.pop(owner.key)
            self._write(owners)
            return True

    def release_unbound_idle_owner(self, owner: ImplementationOwner) -> bool:
        """Release ownership with no execution, provider task, or implementation PR."""
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            executions = record.get("executions", [])
            sessions = record.get("provider_sessions", [])
            implementation_prs = record.get("implementation_prs", [])
            if not isinstance(executions, list) or not isinstance(sessions, list) or not isinstance(implementation_prs, list):
                raise ImplementationSlotUnavailable("Cannot safely parse retained implementation ownership")
            if executions or sessions or implementation_prs:
                return False
            owners.pop(owner.key)
            self._write(owners)
            return True

    def record_validation_identity(self, owner: ImplementationOwner, identity: str) -> bool:
        """Bind logical Issue ownership to its authorized specification identity."""
        with self._state_lock():
            owners = self._read()
            record = owners.get(owner.key)
            if record is None:
                return False
            record["validation_identity"] = identity
            if record.get("admission_pending", False):
                record["admission_established"] = True
            self._write(owners)
            return True

    def validation_identity(self, owner: ImplementationOwner) -> Optional[str]:
        """Return the generation identity attached to retained ownership."""
        with self._state_lock():
            record = self._read().get(owner.key)
        if record is None:
            return None
        identity = record.get("validation_identity")
        if identity is not None and not isinstance(identity, str):
            raise ImplementationSlotUnavailable("Cannot safely parse implementation validation identity")
        return identity

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
        # Execution cleanup is deliberately independent from owner cleanup. A
        # dead process ceases to block lifecycle evaluation, but the GitHub
        # lifecycle below remains the sole authority for releasing its owner.
        self.reclaim_stale_executions()
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
                    if linked_prs_terminal and self._release_if_idle(owner, tuple(recorded_prs)):
                        logger.info(f"Released terminal logical implementation slot {owner.key}")
                elif owner.kind == "pr":
                    item = github_client.get_pull_request(self.repo_name, owner.number)
                    details = github_client.get_pr_details(item)
                    if (details.get("state", "").lower() == "closed" or details.get("merged") is True) and self._release_if_idle(owner):
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
            if depth:
                depths[owner.key] = depth + 1
                try:
                    yield
                finally:
                    depths[owner.key] -= 1
                return

            mutation_lock_path = self.storage_path.parent / f"implementation-{owner.kind}-{owner.number}.lock"
            try:
                mutation_lock_fd = self._open_lock_file(mutation_lock_path)
            except OSError as exc:
                self._raise_permission_error(mutation_lock_path, exc)
            with os.fdopen(mutation_lock_fd, "a+", encoding="utf-8") as lock_file:
                self._establish_shared_permissions(lock_file.fileno(), mutation_lock_path)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    self._raise_permission_error(mutation_lock_path, exc)
                depths[owner.key] = 1
                self._serialization_depth.owners = depths
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    depths.pop(owner.key, None)
