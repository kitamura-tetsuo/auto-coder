"""Deployment-fixed, single-use recovery-epoch persistence.

These receipts record only durable state. They are not a grant, containment
proof, historical completeness verdict, or permission to launch. The issuer
must independently verify those facts before consuming this state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from ..logger_config import get_logger
from .containment_enrollment import _approved_directory
from .fresh_host_observation import _assert_pid_flock
from .fresh_preflight import ObservedIdentity, _file, _identity, _open
from .original_attempts import canonical, identity

EPOCH_ROOT = Path("/var/lib/auto-coder/recovery-epoch")
logger = get_logger(__name__)
_PHASES = ("reserved", "intent", "consumed", "receipt")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_BYTES = 1024 * 1024


class EpochUnavailable(ValueError):
    """An absent, conflicting or uncertain state is never repaired or replayed."""


@dataclass(frozen=True)
class EpochInventory:
    schema_version: int
    root: str
    root_device: int
    root_inode: int
    lock_device: int
    lock_inode: int
    deployment_id: str
    authorization_id: str
    enrollment_ids: tuple[str, ...]
    request_id: str
    preparation_id: str
    target_id: str
    source_sha256: str
    config_sha256: str


@dataclass(frozen=True)
class EpochBinding:
    deployment_id: str
    authorization_id: str
    enrollment_ids: tuple[str, ...]
    request_id: str
    preparation_id: str
    target_id: str
    source_sha256: str
    config_sha256: str
    evidence_sha256: str
    host_boot_id: str
    host_pid: int
    host_start_token: str


@dataclass(frozen=True)
class EpochReceipt:
    inventory_sha256: str
    binding_sha256: str
    consumed_sha256: str
    receipt_sha256: str
    # This state machine never observes dispatch or grants launch authority.
    grant_delivered: bool = False


def issuer_lifetime() -> tuple[str, int, str]:
    """Read the actual current issuer's boot, PID and Linux start token."""
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    fields = Path("/proc/self/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    if str(uuid.UUID(boot)) != boot or len(fields) < 20 or not fields[19].isascii() or not fields[19].isdecimal() or int(fields[19]) <= 0 or str(int(fields[19])) != fields[19]:
        raise EpochUnavailable("recovery issuer lifetime is unavailable")
    return boot, os.getpid(), fields[19]


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read(path: Path) -> tuple[ObservedIdentity, bytes]:
    observed, encoded = _file(path, private=True, limit=_MAX_BYTES)
    if identity(path) != (observed.device, observed.inode):
        raise EpochUnavailable("recovery epoch file was replaced")
    return observed, encoded


def _validate_static(value: EpochInventory | EpochBinding) -> None:
    for field in ("deployment_id", "authorization_id", "request_id", "preparation_id"):
        token = getattr(value, field)
        if not isinstance(token, str) or not _IDENTIFIER.fullmatch(token):
            raise EpochUnavailable("invalid recovery epoch " + field)
    if not isinstance(value.enrollment_ids, tuple) or not value.enrollment_ids or len(value.enrollment_ids) > 256 or any(not isinstance(token, str) or not _IDENTIFIER.fullmatch(token) for token in value.enrollment_ids):
        raise EpochUnavailable("invalid recovery epoch enrollments")
    if tuple(sorted(set(value.enrollment_ids))) != value.enrollment_ids:
        raise EpochUnavailable("recovery epoch enrollments are aliased or unordered")
    for field in ("target_id", "source_sha256", "config_sha256"):
        digest = getattr(value, field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise EpochUnavailable("invalid recovery epoch " + field)


def _parse_inventory(encoded: bytes, root: Path, root_id: tuple[int, int], lock_id: tuple[int, int]) -> EpochInventory:
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or set(raw) != set(EpochInventory.__dataclass_fields__) or canonical(raw) != encoded or not isinstance(raw.get("enrollment_ids"), list):
        raise EpochUnavailable("recovery epoch inventory is noncanonical or incomplete")
    raw["enrollment_ids"] = tuple(raw["enrollment_ids"])
    inventory = EpochInventory(**raw)
    _validate_static(inventory)
    if type(inventory.schema_version) is not int or inventory.schema_version != 1:
        raise EpochUnavailable("unsupported recovery epoch inventory schema")
    if any(type(getattr(inventory, field)) is not int or getattr(inventory, field) < 0 for field in ("root_device", "root_inode", "lock_device", "lock_inode")):
        raise EpochUnavailable("invalid recovery epoch inventory identity")
    if inventory.root != str(root) or (inventory.root_device, inventory.root_inode) != root_id or (inventory.lock_device, inventory.lock_inode) != lock_id:
        raise EpochUnavailable("recovery epoch inventory root or lock differs")
    return inventory


class RecoveryEpochState:
    """One process's held serialization lifetime, never reusable authorization."""

    def __init__(self, root: Path, directory: int, lock: int, inventory: EpochInventory, observed: ObservedIdentity, encoded: bytes) -> None:
        self.inventory = inventory
        self.inventory_sha256 = _digest(encoded)
        self._root = root
        self._directory = directory
        self._lock = lock
        self._inventory_observed = observed
        self._inventory_encoded = encoded
        self._lifetime = issuer_lifetime()
        self._records: dict[str, tuple[ObservedIdentity, bytes]] = {}
        self._binding: EpochBinding | None = None
        self._phase = 0
        self._active = True
        self._failed = False

    def _check(self) -> None:
        if not self._active or self._failed or issuer_lifetime() != self._lifetime:
            raise EpochUnavailable("recovery epoch invocation is closed, failed or inherited")
        try:
            expected_root = (self.inventory.root_device, self.inventory.root_inode)
            if identity(self._root, directory=True) != expected_root or (os.fstat(self._directory).st_dev, os.fstat(self._directory).st_ino) != expected_root:
                raise EpochUnavailable("recovery epoch root was replaced")
            expected_lock = (self.inventory.lock_device, self.inventory.lock_inode)
            if identity(self._root / "epoch.lock") != expected_lock or (os.fstat(self._lock).st_dev, os.fstat(self._lock).st_ino) != expected_lock:
                raise EpochUnavailable("recovery epoch lock was replaced")
            locks = Path(f"/proc/self/fdinfo/{self._lock}").read_text(encoding="ascii").splitlines()
            if not any(line.startswith("lock:") and " FLOCK  ADVISORY  WRITE " in line for line in locks):
                raise EpochUnavailable("recovery epoch exclusive lock is not held")
            _assert_pid_flock(os.getpid(), self._lock, "WRITE")
            if _read(self._root / "inventory.json") != (self._inventory_observed, self._inventory_encoded):
                raise EpochUnavailable("recovery epoch inventory changed")
            if set(os.listdir(self._directory)) != {"inventory.json", "epoch.lock"} | set(self._records):
                raise EpochUnavailable("recovery epoch contains existing, partial or conflicting state")
            for name, expected in self._records.items():
                if _read(self._root / name) != expected:
                    raise EpochUnavailable("recovery epoch record changed")
        except (OSError, ValueError, TypeError) as error:
            self._failed = True
            raise EpochUnavailable("recovery epoch custody unavailable: " + str(error)) from error

    def _require_binding(self, binding: EpochBinding) -> None:
        if not isinstance(binding, EpochBinding):
            raise EpochUnavailable("typed recovery epoch binding required")
        _validate_static(binding)
        for field in ("deployment_id", "authorization_id", "enrollment_ids", "request_id", "preparation_id", "target_id", "source_sha256", "config_sha256"):
            if getattr(binding, field) != getattr(self.inventory, field):
                raise EpochUnavailable("recovery epoch binding differs from fixed inventory: " + field)
        if not isinstance(binding.evidence_sha256, str) or not _SHA256.fullmatch(binding.evidence_sha256):
            raise EpochUnavailable("invalid independently verified recovery evidence digest")
        if type(binding.host_pid) is not int or (binding.host_boot_id, binding.host_pid, binding.host_start_token) != self._lifetime:
            raise EpochUnavailable("recovery epoch binding differs from current issuer lifetime")
        if self._binding is not None and binding != self._binding:
            raise EpochUnavailable("recovery epoch binding changed after reservation")

    def _create(self, name: str, encoded: bytes) -> None:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._directory)
        try:
            # Publish even an empty/partial attempt before filling it. A later
            # invocation refuses the entire namespace instead of repairing it.
            os.fsync(self._directory)
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            written = _identity(self._root / name, descriptor, _digest(encoded))
            os.fsync(self._directory)
            observed, actual = _read(self._root / name)
            # Retain the inode actually fsynced by this writer. Matching bytes
            # in a replacement file cannot inherit that durability observation.
            if observed != written or _identity(self._root / name, descriptor, _digest(encoded)) != written or os.fstat(descriptor).st_nlink != 1 or actual != encoded:
                raise EpochUnavailable("recovery epoch append identity or readback differs")
            self._records[name] = (observed, actual)
        finally:
            os.close(descriptor)

    def _append(self, phase: str, binding: EpochBinding) -> str:
        self._check()
        try:
            self._require_binding(binding)
        except BaseException:
            # An already reserved epoch cannot recover from conflicting authority
            # in the same issuer lifetime, even when the caller catches refusal.
            if self._phase:
                self._failed = True
            raise
        if self._phase >= len(_PHASES) or _PHASES[self._phase] != phase:
            raise EpochUnavailable("recovery epoch transition is out of order or already consumed")
        previous = _digest(self._records[_PHASES[self._phase - 1] + "-head.json"][1]) if self._phase else ""
        value = {"schema_version": 1, "phase": phase, "sequence": self._phase, "previous": previous, "inventory_sha256": self.inventory_sha256, "binding": asdict(binding)}
        encoded = canonical(value)
        try:
            self._create(phase + ".json", encoded)
            self._check()
            head = canonical({"schema_version": 1, "phase": phase, "record_sha256": _digest(encoded), "previous": previous, "inventory_sha256": self.inventory_sha256})
            self._create(phase + "-head.json", head)
            self._check()
            self._binding = binding
            self._phase += 1
            logger.bind(recovery_epoch_phase=phase, request_id=binding.request_id, target_id=binding.target_id, grant_delivered=False).info("Recovery epoch {} durably recorded; no grant delivery observed", phase)
            return _digest(encoded)
        except BaseException:
            # Failure, including cancellation after a successful write, ends
            # this state machine. Nothing is deleted or marked retryable.
            self._failed = True
            raise

    def reserve(self, binding: EpochBinding) -> None:
        """Irreversibly reserve this one deployment-wide approved invocation."""
        self._append("reserved", binding)

    def record_intent(self, binding: EpochBinding) -> None:
        """Retain the full currently verified binding before final consumption."""
        self._append("intent", binding)

    def consume(self, binding: EpochBinding) -> EpochReceipt:
        """Persist consumption and its final receipt before possible dispatch.

        The returned record grants nothing. A caller must retain and verify
        actual consumer fence custody and current evidence at its grant boundary.
        A lost return value cannot be obtained by another call or invocation.
        """
        consumed = self._append("consumed", binding)
        receipt = self._append("receipt", binding)
        self._check()
        return EpochReceipt(self.inventory_sha256, _digest(canonical(asdict(binding))), consumed, receipt)


@contextmanager
def _hold_recovery_epoch(root: Path, *, expected_uid: int) -> Iterator[RecoveryEpochState]:
    """Internal explicit fixture seam; production never accepts a root input."""
    state: RecoveryEpochState | None = None
    if os.geteuid() != expected_uid:
        raise EpochUnavailable("recovery epoch requires its approved Unix owner")
    try:
        root_id = identity(root, directory=True)
        with _open(root, directory=True) as directory, _open(root / "epoch.lock") as lock:
            if (os.fstat(directory).st_dev, os.fstat(directory).st_ino) != root_id:
                raise EpochUnavailable("recovery epoch directory changed while opening")
            lock_id = identity(root / "epoch.lock")
            if (os.fstat(lock).st_dev, os.fstat(lock).st_ino) != lock_id:
                raise EpochUnavailable("recovery epoch lock changed while opening")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise EpochUnavailable("recovery epoch admission contested") from error
            observed, encoded = _read(root / "inventory.json")
            inventory = _parse_inventory(encoded, root, root_id, lock_id)
            state = RecoveryEpochState(root, directory, lock, inventory, observed, encoded)
            state._check()
            yield state
            state._check()
    except (OSError, ValueError, TypeError, KeyError) as error:
        if isinstance(error, EpochUnavailable):
            raise
        raise EpochUnavailable("recovery epoch state unavailable: " + str(error)) from error
    finally:
        if state is not None:
            state._active = False


@contextmanager
def hold_recovery_epoch() -> Iterator[RecoveryEpochState]:
    """Open only the deployment-fixed root; no request/environment override.

    Provisioning the protected inventory and original empty lock is a separate
    reviewed operation. Missing state is unavailable, never auto-initialized.
    """
    if os.geteuid() != 0:
        raise EpochUnavailable("recovery epoch production issuer requires host root")
    try:
        _approved_directory(EPOCH_ROOT, uid=0)
        with _hold_recovery_epoch(EPOCH_ROOT, expected_uid=0) as state:
            yield state
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, EpochUnavailable):
            raise
        raise EpochUnavailable("fixed recovery epoch inventory unavailable: " + str(error)) from error
