"""Real peer and late-mutation faults in the separately authorized recovery VM.

Only the original production issuer and bootstrap can produce authority. This
fixture observes real syscalls, retained process lifetimes and received kernel
objects. It may stop/kill its exact target or mutate an explicitly selected
fixture object; it never changes syscall arguments/results or production code.
"""

from __future__ import annotations

import array
import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from scripts.recovery_epoch_fault_tracer import _AUDIT_ARCH_X86_64, _PTRACE_GET_SYSCALL_INFO, KernelPublicationBarrier, TraceProcess, _Information, _token
from scripts.recovery_epoch_native_negatives import NativeNegativeContext, _epoch_snapshot, _projection, assert_unreserved_epoch, require_native_context
from scripts.test_management_containment_enrollment_host import _docker, _systemctl

from auto_coder.logger_config import get_logger
from auto_coder.management.bootstrap_supervisor import BootstrapConfig
from auto_coder.management.containment_route_manifest import _systemd_unit
from auto_coder.management.original_attempt_evidence import OriginalLineageEvidence, derive_lineage, observe, verify_lineage
from auto_coder.management.original_attempts import canonical, read_original
from auto_coder.management.recovery_epoch_authorization import AUTHORIZATION_FILE, PEER_SOCKET, EpochAuthorization
from auto_coder.management.recovery_epoch_consumer import GRANT_SCHEMA, ConsumerConfiguration, parse_consumer_configuration
from auto_coder.management.recovery_epoch_peer import CHALLENGE_SCHEMA, MAX_MESSAGE_BYTES, MAX_RESOURCES, OFFER_SCHEMA, RECEIPT_SCHEMA, inspect_kernel_lock

logger = get_logger(__name__)
REQUIRED_PEER_CASES = frozenset({"queued-rights-without-receipt-refused", "received-rights-without-receipt-refused", "changed-target-lifetime-refused", "foreign-acquirer-sh-refused"})
LATE_MUTATION_SCENARIOS = (
    "late-management-config",
    "late-ready-receipt",
    "late-installed-source",
    "late-original-journal",
    "late-original-attempt",
    "late-watcher-config",
    "late-controller-restarter",
    "late-original-fence",
    "late-target-fence",
    "late-controller-fence",
    "late-target-loss",
)
_SYS_SENDTO = 44
_SYS_SENDMSG = 46
_SYS_RECVMSG = 47
_SYS_PIDFD_GETFD = 438


def _wait(predicate: Callable[[], bool], description: str, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("native peer barrier timed out: " + description)
        time.sleep(0.02)


def _target_state(context: NativeNegativeContext) -> dict[str, object]:
    value = json.loads(_docker("inspect", "--format", "{{json .State}}", context.enrollment.target.management_id))
    if not isinstance(value, dict):
        raise AssertionError("actual target Docker state is unavailable")
    return value


class DockerTraceProcess:
    """Pin one actual Docker PID without inventing a parent/child relationship."""

    def __init__(self, context: NativeNegativeContext):
        self.context = context
        state = _target_state(context)
        if state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 1:
            raise AssertionError("the exact production target has no current lifetime")
        self.pid = int(state["Pid"])
        self.start_token = _token(self.pid)
        self.docker_started_at = state.get("StartedAt")
        self.returncode: int | None = None
        self.pidfd = os.pidfd_open(self.pid, 0)
        self.paused = False
        self.check()

    def dead(self) -> bool:
        poll = select.poll()
        poll.register(self.pidfd, select.POLLIN)
        return bool(poll.poll(0))

    def check(self) -> None:
        if self.pidfd < 0 or self.dead() or _token(self.pid) != self.start_token:
            raise AssertionError("the exact selected target lifetime ended or changed")
        state = _target_state(self.context)
        if state.get("Running") is not True or state.get("Pid") != self.pid or state.get("StartedAt") != self.docker_started_at:
            raise AssertionError("the selected Docker object now names another lifetime")

    def wait(self, timeout: float | None = None) -> int:
        """Observe Docker's actual termination after tracer wait ownership ends."""
        timeout = 30 if timeout is None else timeout
        poll = select.poll()
        poll.register(self.pidfd, select.POLLIN)
        if not poll.poll(max(1, int(timeout * 1000))):
            raise subprocess.TimeoutExpired("actual target pidfd", timeout)
        if self.returncode is None:
            _wait(lambda: _target_state(self.context).get("Running") is False, "Docker records the original target's exit", timeout=timeout)
            state = _target_state(self.context)
            if state.get("StartedAt") != self.docker_started_at or type(state.get("ExitCode")) is not int:
                raise AssertionError("Docker no longer retains the original target exit code")
            self.returncode = int(state["ExitCode"])
        return self.returncode

    def pause(self) -> None:
        self.check()
        signal.pidfd_send_signal(self.pidfd, signal.SIGSTOP)
        _wait(lambda: Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] in {"T", "t"}, "exact target is kernel-stopped")
        self.check()
        self.paused = True

    def resume(self) -> None:
        if self.paused and not self.dead():
            self.check()
            signal.pidfd_send_signal(self.pidfd, signal.SIGCONT)
        self.paused = False

    def kill(self) -> None:
        self.check()
        signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)

    def close(self) -> None:
        if self.pidfd >= 0:
            self.resume()
            os.close(self.pidfd)
            self.pidfd = -1


def _container_ids(encoded: str) -> tuple[str, ...]:
    """Require complete real Docker identities before interpreting absence."""
    values = tuple(encoded.splitlines())
    if len(values) > 4096 or len(values) != len(set(values)) or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in values):
        raise AssertionError("the actual Docker inventory has incomplete or conflicting identities")
    return tuple(sorted(values))


def _verify_target_loss(pidfd: int, identifier: str, before: tuple[str, ...], after: tuple[str, ...]) -> None:
    """Require both kernel death and removal of exactly the selected object."""
    if before.count(identifier) != 1 or after != tuple(value for value in before if value != identifier):
        raise AssertionError("the exact target was not removed without changing other Docker objects")
    poll = select.poll()
    poll.register(pidfd, select.POLLIN)
    if not poll.poll(0):
        raise AssertionError("the original target pidfd has not independently reported death")


@contextmanager
def _remove_target(context: NativeNegativeContext) -> Iterator[dict[str, object]]:
    """Remove only the pinned disposable target and retain that loss on exit."""
    target = DockerTraceProcess(context)
    try:
        identifier = context.enrollment.target.management_id
        before = _container_ids(_docker("ps", "--all", "--quiet", "--no-trunc"))
        if identifier not in before:
            raise AssertionError("the exact target is missing before its deliberate removal")
        target.check()
        # _docker checks the actual command return code. Neither a failed
        # removal nor Docker's stopped flag supplies kernel lifetime evidence.
        removed = _docker("rm", "--force", identifier, timeout=60)
        if removed != identifier:
            raise AssertionError("the actual Docker removal did not identify the selected target")
        _wait(target.dead, "the original target pidfd reports actual death")
        after = _container_ids(_docker("ps", "--all", "--quiet", "--no-trunc"))
        _verify_target_loss(target.pidfd, identifier, before, after)
        observation = {
            "target_id": identifier,
            "pid": target.pid,
            "start_token": target.start_token,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "docker_started_at": target.docker_started_at,
            "docker_rm_returncode": 0,
            "pidfd_death_observed": True,
            "exact_docker_id_absent": True,
            "other_docker_ids_preserved": True,
            "container_ids_before_sha256": hashlib.sha256(canonical(before)).hexdigest(),
            "container_ids_after_sha256": hashlib.sha256(canonical(after)).hexdigest(),
        }
        yield observation
        # The driver must retain the injected loss through the original
        # refusal and fixed-root replay; it must never recreate this target.
        _verify_target_loss(target.pidfd, identifier, before, _container_ids(_docker("ps", "--all", "--quiet", "--no-trunc")))
    finally:
        target.close()


def _consumer_configuration(context: NativeNegativeContext) -> ConsumerConfiguration:
    selected = [item for item in context.enrollment.target.mounts if item.destination == "/bootstrap/recovery-epoch.json"]
    if len(selected) != 1 or selected[0].writable:
        raise AssertionError("the actual consumer lacks its fixed read-only configuration")
    result = parse_consumer_configuration(Path(selected[0].source).read_bytes())
    if (result.authorization_id, result.target_id) != (context.enrollment.authorization_id, context.enrollment.target.management_id):
        raise AssertionError("the selected native consumer belongs to another fixture")
    return result


def _fd_for_object(pid: int, device: int, inode: int, mode: str) -> int:
    matches = []
    for path in Path(f"/proc/{pid}/fd").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            info = path.stat()
            if (info.st_dev, info.st_ino) == (device, inode):
                lock = inspect_kernel_lock(pid, int(path.name), device, inode)
                if lock is not None and lock.mode == mode:
                    matches.append(int(path.name))
        except FileNotFoundError:
            continue
    if len(matches) != 1:
        raise AssertionError("the actual selected process does not hold one exact fence descriptor")
    return matches[0]


def _duplicate(pid: int, descriptor: int, device: int, inode: int) -> int:
    """Duplicate the real OFD; unsupported pidfd_getfd is a fixture failure."""
    lifetime = _token(pid)
    pidfd = os.pidfd_open(pid, 0)
    duplicate = -1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        duplicate = int(libc.syscall(ctypes.c_long(_SYS_PIDFD_GETFD), ctypes.c_int(pidfd), ctypes.c_int(descriptor), ctypes.c_uint(0)))
        if duplicate < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        info = os.fstat(duplicate)
        if (info.st_dev, info.st_ino) != (device, inode) or _token(pid) != lifetime:
            raise AssertionError("the duplicated native descriptor or process changed")
        os.set_inheritable(duplicate, False)
        return duplicate
    except BaseException:
        if duplicate >= 0:
            os.close(duplicate)
        raise
    finally:
        os.close(pidfd)


@contextmanager
def _foreign_acquirer(pid: int, device: int, inode: int, mode: str) -> Iterator[dict[str, object]]:
    descriptor = _fd_for_object(pid, device, inode, mode)
    before = inspect_kernel_lock(pid, descriptor, device, inode)
    if before is None or before.pid != pid:
        raise AssertionError("the selected fence was not independently held before the real fault")
    duplicate = _duplicate(pid, descriptor, device, inode)
    try:
        fcntl.flock(duplicate, fcntl.LOCK_UN)
        fcntl.flock(duplicate, (fcntl.LOCK_SH if mode == "READ" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        after = inspect_kernel_lock(pid, descriptor, device, inode)
        if after is None or after.pid != os.getpid() or after.mode != mode or after == before:
            raise AssertionError("the real shared OFD did not record the foreign acquiring process")
        yield {"pid": pid, "start_token": _token(pid), "descriptor": descriptor, "device": device, "inode": inode, "before": asdict(before), "after": asdict(after)}
        if inspect_kernel_lock(os.getpid(), duplicate, device, inode) != after:
            raise AssertionError("the injected native OFD custody changed during refusal")
    finally:
        os.close(duplicate)


def _require_publication_prefix(context: NativeNegativeContext, issuer_pid: int, through: str) -> None:
    require_native_context(context, unreserved=False)
    phases = ("reserved", "intent", "consumed", "receipt")
    if through not in phases:
        raise ValueError("an exact existing publication prefix is required")
    chosen = phases[: phases.index(through) + 1]
    expected = {"inventory.json", "epoch.lock", *(name + suffix for name in chosen for suffix in (".json", "-head.json"))}
    if {path.name for path in context.epoch_root.iterdir()} != expected:
        raise AssertionError("the actual issuer is not stopped at the selected original publication prefix")
    inventory_sha = hashlib.sha256((context.epoch_root / "inventory.json").read_bytes()).hexdigest()
    previous, binding = "", None
    for sequence, phase in enumerate(chosen):
        encoded = (context.epoch_root / (phase + ".json")).read_bytes()
        record = json.loads(encoded)
        head_bytes = (context.epoch_root / (phase + "-head.json")).read_bytes()
        head = json.loads(head_bytes)
        if canonical(record) != encoded or canonical(head) != head_bytes or record.get("phase") != phase or record.get("sequence") != sequence or record.get("previous") != previous or record.get("inventory_sha256") != inventory_sha:
            raise AssertionError("the actual durable publication chain is malformed")
        if head != {"schema_version": 1, "phase": phase, "record_sha256": hashlib.sha256(encoded).hexdigest(), "previous": previous, "inventory_sha256": inventory_sha}:
            raise AssertionError("the actual publication head differs from its original record")
        binding = record["binding"] if binding is None else binding
        if record.get("binding") != binding:
            raise AssertionError("the stopped publication prefix changed authority")
        previous = hashlib.sha256(head_bytes).hexdigest()
    if binding is None or (binding["host_pid"], binding["host_start_token"], binding["host_boot_id"]) != (issuer_pid, _token(issuer_pid), Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
        raise AssertionError("the durable original prefix belongs to another issuer lifetime")


def _failed_terminal(lineage: OriginalLineageEvidence) -> tuple[Path, dict[str, object]]:
    """Select a failed original only from the complete, independently read set."""
    branches = [generation for generation in lineage.generations if generation.outcome == "never-created"]
    if lineage.unavailable or len(branches) != 1:
        raise AssertionError("the actual fixture has no unique complete never-created failed branch")
    branch = branches[0]
    matches = []
    for original in lineage.originals:
        path = Path(original.path)
        if path.parent != Path(lineage.binding.root) or path.suffix != ".json":
            continue
        encoded, file_id = read_original(path)
        if file_id != (original.device, original.inode) or hashlib.sha256(encoded).hexdigest() != original.sha256:
            raise AssertionError("the selected failed-branch original changed after independent derivation")
        value = json.loads(encoded)
        if isinstance(value, dict) and value.get("kind") == "never-created" and value.get("attempt") == branch.request_id:
            matches.append((path, original))
    if len(matches) != 1:
        raise AssertionError("the actual failed branch has no unique matched original terminal record")
    path, original = matches[0]
    return path, {"instance_id": branch.instance_id, "request_id": branch.request_id, "outcome": branch.outcome, "frontier": lineage.frontier, "terminal_kind": "never-created", "original_file": asdict(original)}


def _failed_branch_original(context: NativeNegativeContext) -> tuple[Path, dict[str, object]]:
    """Reverify real L evidence and bind the fault to the issuer's saved input."""
    authorization = EpochAuthorization.parse(json.loads(read_original(AUTHORIZATION_FILE)[0]))
    if authorization.target_id != context.enrollment.target.management_id:
        raise AssertionError("the current authorization selects another target")
    with ExitStack() as held:
        descriptors = []
        for original in context.enrollment.originals:
            owner = original.owner
            if owner is None:
                continue
            _, identity = read_original(Path(owner.path))
            descriptor = os.open(owner.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            held.callback(os.close, descriptor)
            if identity != (owner.device, owner.inode) or (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != identity:
                raise AssertionError("the actual original owner changed before independent lineage verification")
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            descriptors.append(descriptor)
        history = held.enter_context(observe(authorization.history))
        current = derive_lineage(authorization.history, history, tuple(descriptors))
        verify_lineage(current, authorization.history, history, tuple(descriptors))
        intent = json.loads(read_original(context.epoch_root / "intent.json")[0])
        saved = json.loads(intent["binding"]["evidence_json"])["lineage"]
        if canonical(asdict(current)) != canonical(saved):
            raise AssertionError("the independently verified failed-branch scope differs from the actual retained intent")
        path, observation = _failed_terminal(current)
        observation.update(independent_lineage_verified=True, retained_intent_lineage_matched=True)
        return path, observation


def _mutation_file(context: NativeNegativeContext, scenario: str) -> Path:
    target = context.enrollment.target
    if scenario == "late-management-config":
        return Path(target.management_config.path)
    if scenario == "late-watcher-config":
        return Path(context.enrollment.watchers[0].configuration.path)
    if scenario == "late-original-journal":
        original = context.enrollment.originals[0]
        config = BootstrapConfig.read(Path(original.bootstrap_config.path))
        mounts = [item for item in original.mounts if item.container_id == original.management_id and item.destination == str(config.journal_root) and item.writable]
        if len(mounts) != 1:
            raise AssertionError("the original native journal mapping is ambiguous")
        return Path(mounts[0].source) / "management-operations.sqlite3"
    authorization = EpochAuthorization.parse(json.loads(AUTHORIZATION_FILE.read_bytes()))
    if authorization.target_id != target.management_id:
        raise AssertionError("the actual fixed authorization selects a different target")
    if scenario == "late-ready-receipt":
        return Path(authorization.ready_receipt.path)
    if scenario == "late-installed-source":
        receipt = json.loads(Path(authorization.ready_receipt.path).read_bytes())
        return Path(receipt["environment_path"]) / "lib/python3.12/site-packages/auto_coder/__init__.py"
    raise ValueError("the selected late fault is not a file mutation")


@contextmanager
def mutate_after_intent(context: NativeNegativeContext, scenario: str, issuer_pid: int) -> Iterator[dict[str, object]]:
    """Retain the deliberate fault through refusal and replay; never repair it."""
    if scenario not in LATE_MUTATION_SCENARIOS:
        raise ValueError("a reviewed late mutation scenario is required")
    _require_publication_prefix(context, issuer_pid, "intent")
    observation: dict[str, object] = {"scenario": scenario, "issuer_pid": issuer_pid, "issuer_start_token": _token(issuer_pid), "boundary": "after-intent-head", "preservation_baseline": "after-deliberate-fixture-mutation"}
    with ExitStack() as held:
        if scenario == "late-target-loss":
            observation["target_fault"] = held.enter_context(_remove_target(context))
        elif scenario.endswith("-fence"):
            pid = issuer_pid
            if scenario == "late-controller-fence":
                lock, mode = context.enrollment.watchers[0].lock, "WRITE"
            else:
                target = DockerTraceProcess(context)
                held.callback(target.close)
                pid = target.pid
                if scenario == "late-original-fence":
                    lock, mode = next(item.owner for item in context.enrollment.originals if item.owner is not None), "READ"
                else:
                    lock, mode = context.enrollment.target.owner, "WRITE"
            observation["kernel_fault"] = held.enter_context(_foreign_acquirer(pid, lock.device, lock.inode, mode))
        elif scenario == "late-controller-restarter":
            unit = context.enrollment.watchers[0].unit
            before = _systemd_unit(unit)
            if not before.inhibited:
                raise AssertionError("the actual controller was not inhibited at the original boundary")
            _systemctl("unmask", unit)
            _systemctl("daemon-reload")
            after = _systemd_unit(unit)
            if after.inhibited or after.main_pid != 0:
                raise AssertionError("the real controller did not become startable without starting a process")
            observation["controller_fault"] = {"unit": unit, "before": asdict(before), "after": asdict(after)}
        else:
            if scenario == "late-original-attempt":
                path, observation["original_attempt"] = _failed_branch_original(context)
            else:
                path = _mutation_file(context, scenario)
            if not path.is_relative_to(context.fixture_root) or path != path.resolve(strict=True):
                raise PermissionError("late mutation escaped the separately authorized fixture root")
            before = _projection(path)
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != before[:2]:
                    raise AssertionError("the selected original changed before deliberate mutation")
                os.write(descriptor, b"\n# deliberate native fixture source mutation\n" if scenario == "late-installed-source" else b"\x00")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            after = _projection(path)
            if before == after or before[:2] != after[:2]:
                raise AssertionError("the selected native original bytes did not change in place")
            observation["file_fault"] = {"path": str(path), "before": before, "after": after}
        logger.bind(native_peer_fault=scenario, issuer_pid=issuer_pid).info("Injected the explicit native fault after the original intent head")
        yield observation
        if "file_fault" in observation and _projection(path) != after:
            raise AssertionError("production refusal or replay changed the deliberately injected file")
        if "controller_fault" in observation and _systemd_unit(unit) != after:
            raise AssertionError("production refusal or replay changed the deliberately unmasked controller")


def _socket_option(descriptor: int, option: int, size: int) -> bytes:
    """Read kernel socket metadata without constructing or reconfiguring a socket."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.getsockopt.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
    libc.getsockopt.restype = ctypes.c_int
    value = ctypes.create_string_buffer(size)
    length = ctypes.c_uint(size)
    result = libc.getsockopt(descriptor, socket.SOL_SOCKET, option, value, ctypes.byref(length))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if length.value != size:
        raise AssertionError("the actual socket metadata has an incomplete kernel representation")
    return value.raw


def _local_socket_peer(descriptor: int) -> dict[str, int] | None:
    """Return the real AF_UNIX stream peer's host-visible credentials."""
    domain = struct.unpack("i", _socket_option(descriptor, socket.SO_DOMAIN, struct.calcsize("i")))[0]
    kind = struct.unpack("i", _socket_option(descriptor, socket.SO_TYPE, struct.calcsize("i")))[0]
    if domain != socket.AF_UNIX or kind != socket.SOCK_STREAM:
        return None
    pid, uid, gid = struct.unpack("iII", _socket_option(descriptor, socket.SO_PEERCRED, struct.calcsize("iII")))
    return {"pid": pid, "uid": uid, "gid": gid}


def _peek_queued_packet(descriptor: int, expected: bytes) -> tuple[bytes, int, int]:
    """Observe the actual queued frame twice, without consuming or changing flags."""
    if not expected.endswith(b"\n") or not 0 < len(expected) <= MAX_MESSAGE_BYTES:
        raise AssertionError("a bounded complete expected grant frame is required")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.recv.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    libc.recv.restype = ctypes.c_ssize_t
    before = fcntl.fcntl(descriptor, fcntl.F_GETFL)

    def peek() -> bytes:
        buffer = ctypes.create_string_buffer(MAX_MESSAGE_BYTES + 1)
        result = libc.recv(descriptor, buffer, len(buffer), socket.MSG_PEEK | socket.MSG_DONTWAIT)
        if result < 0:
            error = ctypes.get_errno()
            raise AssertionError("the actual receiver has no observable complete queued grant: " + os.strerror(error))
        if result <= 0 or result > MAX_MESSAGE_BYTES:
            raise AssertionError("the actual receiver has an empty or oversized queued grant")
        return buffer.raw[:result]

    encoded = peek()
    if encoded != expected:
        raise AssertionError("the actual queued bytes differ from the exact consumed grant")
    if peek() != encoded:
        raise AssertionError("the actual grant bytes did not remain queued after MSG_PEEK")
    after = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    if after != before:
        raise AssertionError("observing the actual grant changed shared socket status flags")
    return encoded, before, after


def _expected_queued_grant(context: NativeNegativeContext, issuer_pid: int, issuer_start_token: str, target_pid: int, target_start_token: str) -> dict[str, object]:
    """Bind the expected packet to fixed consumer inputs and original durable custody."""
    configuration = _consumer_configuration(context)
    inventory_bytes = read_original(context.epoch_root / "inventory.json")[0]
    consumed_bytes = read_original(context.epoch_root / "consumed.json")[0]
    receipt_bytes = read_original(context.epoch_root / "receipt.json")[0]
    receipt = json.loads(receipt_bytes)
    binding = receipt["binding"]
    evidence_bytes = binding["evidence_json"].encode()
    evidence = json.loads(evidence_bytes)
    if canonical(evidence) != evidence_bytes or hashlib.sha256(evidence_bytes).hexdigest() != binding["evidence_sha256"]:
        raise AssertionError("the actual retained custody evidence is not canonical and digest-bound")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if (binding["host_pid"], binding["host_start_token"], binding["host_boot_id"]) != (issuer_pid, issuer_start_token, boot):
        raise AssertionError("the original consumed evidence names another issuer lifetime")
    custody = evidence["custody"]
    peer = custody["peer"]
    if (peer["pid"], peer["start_token"], peer["boot_id"], peer["uid"]) != (target_pid, target_start_token, boot, 0):
        raise AssertionError("the original consumed custody names another target lifetime")
    selected = {name: getattr(configuration, name) for name in ("request_id", "authorization_id", "target_id")}
    if any(binding[name] != value or peer[name] != value for name, value in selected.items()):
        raise AssertionError("the original consumed custody differs from the actual fixed consumer binding")
    for name in ("challenge", "receipt_nonce"):
        if not isinstance(custody[name], str) or re.fullmatch(r"[0-9a-f]{64}", custody[name]) is None:
            raise AssertionError("the actual original custody challenge or receipt nonce is incomplete")
    return {
        "schema": GRANT_SCHEMA,
        "granted": "recovery-epoch-consumed",
        **selected,
        "challenge": custody["challenge"],
        "receipt_nonce": custody["receipt_nonce"],
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "binding_sha256": hashlib.sha256(canonical(binding)).hexdigest(),
        "consumed_sha256": hashlib.sha256(consumed_bytes).hexdigest(),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "host_boot_id": boot,
        "host_pid": issuer_pid,
        "host_start_token": issuer_start_token,
    }


def observe_queued_grant(context: NativeNegativeContext, issuer_pid: int, *, target_pid: int, target_start_token: str, issuer_start_token: str) -> dict[str, object]:
    """Prove a full grant reached the stopped receiver before the issuer dies."""
    _require_publication_prefix(context, issuer_pid, "receipt")
    with ExitStack() as held:
        target = DockerTraceProcess(context)
        held.callback(target.close)
        issuer_lifetime = os.pidfd_open(issuer_pid, 0)
        held.callback(os.close, issuer_lifetime)
        issuer_dead = select.poll()
        issuer_dead.register(issuer_lifetime, select.POLLIN)

        def check_lifetimes() -> None:
            target.check()
            if (target.pid, target.start_token) != (target_pid, target_start_token) or issuer_dead.poll(0) or _token(issuer_pid) != issuer_start_token:
                raise AssertionError("a queued-grant observer input differs from the original pinned lifetimes")

        check_lifetimes()
        before = _descriptor_snapshot(target.pid)
        _stopped_receiver(target.pid, target.start_token, target.pidfd, before)
        if len(before) > 4096:
            raise AssertionError("the actual receiver descriptor inventory exceeds the fixture bound")
        expected = _expected_queued_grant(context, issuer_pid, issuer_start_token, target.pid, target.start_token)
        configuration = _consumer_configuration(context)
        matches = []
        for descriptor, device, inode, _ in before:
            if not stat.S_ISSOCK(Path(f"/proc/{target.pid}/fd/{descriptor}").stat().st_mode):
                continue
            duplicate = _duplicate(target.pid, descriptor, device, inode)
            held.callback(os.close, duplicate)
            credentials = _local_socket_peer(duplicate)
            if credentials is not None and credentials["pid"] == issuer_pid:
                if credentials["uid"] != configuration.issuer_uid:
                    raise AssertionError("the actual queued-grant sender has the wrong Unix owner")
                matches.append((descriptor, device, inode, duplicate, credentials))
        if len(matches) != 1:
            raise AssertionError("the stopped receiver does not retain one exact authenticated issuer socket")
        descriptor, device, inode, duplicate, credentials = matches[0]
        encoded, flags_before, flags_after = _peek_queued_packet(duplicate, canonical(expected) + b"\n")
        check_lifetimes()
        _stopped_receiver(target.pid, target.start_token, target.pidfd, before)
        _assert_no_target_admission(context, running_pid=target.pid)
        _require_publication_prefix(context, issuer_pid, "receipt")
        if _expected_queued_grant(context, issuer_pid, issuer_start_token, target.pid, target.start_token) != expected or _local_socket_peer(duplicate) != credentials:
            raise AssertionError("the original grant binding or socket sender changed during kernel observation")
        logger.bind(issuer_pid=issuer_pid, target_pid=target.pid, grant_schema=GRANT_SCHEMA, bytes_observed=len(encoded)).info("Observed the complete consumed grant still queued for the stopped actual receiver")
        return {
            "schema": "actual-recovery-queued-grant-v1",
            "target_id": configuration.target_id,
            "target_pid": target.pid,
            "target_start_token": target.start_token,
            "issuer_pid": issuer_pid,
            "issuer_start_token": issuer_start_token,
            "socket_descriptor": descriptor,
            "socket_device": device,
            "socket_inode": inode,
            "peer_credentials": credentials,
            "grant_schema": GRANT_SCHEMA,
            "message_sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes_observed": len(encoded),
            "grant": expected,
            "socket_flags_before": flags_before,
            "socket_flags_after": flags_after,
            "peek_only": True,
            "socket_flags_unchanged": True,
            "bytes_still_queued": True,
            "receiver_stopped": True,
            "issuer_alive": True,
        }


@contextmanager
def pause_target_before_grant(context: NativeNegativeContext, issuer_pid: int) -> Iterator[dict[str, object]]:
    """Hold the actual receiver while the original issuer sends, then dies."""
    _require_publication_prefix(context, issuer_pid, "receipt")
    target = DockerTraceProcess(context)
    issuer_lifetime = os.pidfd_open(issuer_pid, 0)
    try:
        target.pause()
        observation = {"target_pid": target.pid, "target_start_token": target.start_token, "issuer_pid": issuer_pid, "issuer_start_token": _token(issuer_pid), "boundary": "after-receipt-head-before-target-grant-observation", "issuer_death_verified_before_receiver_resumed": False}
        _assert_no_target_admission(context, running_pid=target.pid)
        yield observation
        target.check()
        issuer_dead = select.poll()
        issuer_dead.register(issuer_lifetime, select.POLLIN)
        if not issuer_dead.poll(0):
            raise AssertionError("the exact original issuer still lives before the receiver resumes")
        observation["issuer_death_verified_before_receiver_resumed"] = True
        _assert_no_target_admission(context, running_pid=target.pid)
    finally:
        target.close()
        os.close(issuer_lifetime)


class _Iovec(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]


class _MessageHeader(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("name_length", ctypes.c_uint32), ("vectors", ctypes.c_void_p), ("vector_count", ctypes.c_size_t), ("control", ctypes.c_void_p), ("control_length", ctypes.c_size_t), ("flags", ctypes.c_int)]


class _ControlHeader(ctypes.Structure):
    _fields_ = [("length", ctypes.c_size_t), ("level", ctypes.c_int), ("kind", ctypes.c_int)]


@dataclass(frozen=True)
class PeerMessageObservation:
    process_id: int
    start_token: str
    boot_id: str
    schema: str
    direction: str
    when: str
    socket_descriptor: int
    socket_inode: int
    message_sha256: str
    bytes_observed: int
    syscall_result: int | None
    transferred_descriptors: tuple[int, ...]
    descriptor_objects: tuple[tuple[int, int, int, str], ...]


def _decode_rights(control: bytes) -> tuple[int, ...]:
    """Decode the bounded native Linux cmsghdr layout, never claimed JSON FDs."""
    result: list[int] = []
    offset = 0
    header_size = ctypes.sizeof(_ControlHeader)
    alignment = ctypes.sizeof(ctypes.c_size_t)
    while offset < len(control):
        if len(control) - offset < header_size:
            raise AssertionError("native SCM_RIGHTS control header is truncated")
        header = _ControlHeader.from_buffer_copy(control[offset : offset + header_size])
        if header.length < header_size or offset + header.length > len(control) or (header.level, header.kind) != (socket.SOL_SOCKET, socket.SCM_RIGHTS):
            raise AssertionError("native message control is not exact SCM_RIGHTS")
        payload = control[offset + header_size : offset + header.length]
        if len(payload) % array.array("i").itemsize:
            raise AssertionError("native SCM_RIGHTS descriptor bytes are truncated")
        descriptors = array.array("i")
        descriptors.frombytes(payload)
        result.extend(descriptors)
        if len(result) > MAX_RESOURCES or any(item < 0 for item in result) or len(set(result)) != len(result):
            raise AssertionError("native SCM_RIGHTS descriptor set is invalid")
        end = offset + int(header.length)
        aligned = (end + alignment - 1) // alignment * alignment
        # Linux may include or omit the final cmsghdr alignment padding.
        offset = min(aligned, len(control)) if end <= len(control) < aligned else aligned
    return tuple(result)


class KernelPeerMessageBarrier(KernelPublicationBarrier):
    """Stop one real protocol syscall with no ptrace register or memory writes."""

    def __init__(
        self,
        process: TraceProcess,
        *,
        context: NativeNegativeContext,
        schema: str,
        direction: str,
        when: str = "after",
        before_send: Callable[[], None] | None = None,
    ):
        if schema not in {CHALLENGE_SCHEMA, OFFER_SCHEMA, RECEIPT_SCHEMA, GRANT_SCHEMA} or direction not in {"send", "receive"} or when not in {"before", "after"} or when == "before" and direction != "send":
            raise ValueError("an exact reviewed native message boundary is required")
        super().__init__(process, root=context.epoch_root, name="reserved.json", when="before")
        self.context = context
        self.schema = schema
        self.direction = direction
        self.message_when = when
        self.before_send = before_send
        configuration = _consumer_configuration(context)
        self.binding = {name: getattr(configuration, name) for name in ("request_id", "authorization_id", "target_id")}
        self._message_call: tuple[int, int, int, int] | None = None
        self._selected_message: tuple[bytes, tuple[int, ...], int, int | None] | None = None
        self._before_called = False

    def _memory(self, address: int, length: int) -> bytes:
        if not address or type(length) is not int or not 0 <= length <= MAX_MESSAGE_BYTES + 1024:
            raise AssertionError("native protocol memory read is absent or unbounded")
        with Path(f"/proc/{self.issuer.pid}/mem").open("rb", buffering=0) as memory:
            value = os.pread(memory.fileno(), length, address)
        if len(value) != length:
            raise AssertionError("native protocol memory is incomplete")
        return value

    def _message(self, address: int, *, received_length: int | None) -> tuple[bytes, tuple[int, ...]]:
        header = _MessageHeader.from_buffer_copy(self._memory(address, ctypes.sizeof(_MessageHeader)))
        if not 1 <= header.vector_count <= 4 or header.control_length > socket.CMSG_SPACE(MAX_RESOURCES * array.array("i").itemsize) or header.flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise AssertionError("native protocol message vectors or ancillary data exceed their bound")
        vectors = self._memory(int(header.vectors or 0), ctypes.sizeof(_Iovec) * header.vector_count)
        data = bytearray()
        remaining = received_length
        for index in range(header.vector_count):
            item = _Iovec.from_buffer_copy(vectors[index * ctypes.sizeof(_Iovec) : (index + 1) * ctypes.sizeof(_Iovec)])
            size = int(item.length) if remaining is None else min(int(item.length), remaining)
            if size < 0 or size > MAX_MESSAGE_BYTES + 1 or len(data) + size > MAX_MESSAGE_BYTES + 1:
                raise AssertionError("native protocol message bytes exceed their bound")
            if size:
                data.extend(self._memory(int(item.base or 0), size))
            if remaining is not None:
                remaining -= size
                if remaining == 0:
                    break
        if remaining not in (None, 0):
            raise AssertionError("actual recvmsg result exceeds its real iovec capacity")
        control = self._memory(int(header.control or 0), int(header.control_length)) if header.control_length else b""
        return bytes(data), _decode_rights(control)

    def _selected(self, encoded: bytes) -> bool:
        if not encoded.endswith(b"\n") or encoded.count(b"\n") != 1 or len(encoded) > MAX_MESSAGE_BYTES:
            return False
        try:
            value = json.loads(encoded)
        except (UnicodeError, ValueError):
            return False
        return isinstance(value, dict) and value.get("schema") == self.schema and all(value.get(name) == selected for name, selected in self.binding.items())

    def _observe_call(self) -> bool:
        info = _Information()
        length = self._ptrace(_PTRACE_GET_SYSCALL_INFO, ctypes.sizeof(info), ctypes.cast(ctypes.byref(info), ctypes.c_void_p))
        if length < 24 or info.architecture != _AUDIT_ARCH_X86_64:
            raise AssertionError("actual native protocol syscall information is unavailable")
        if info.operation == 1:
            self._message_call = None
            call = info.call.entry
            number, descriptor, address = int(call.number), int(call.arguments[0]), int(call.arguments[1])
            if self.direction == "receive" and number == _SYS_RECVMSG:
                self._message_call = (number, descriptor, address, 0)
            elif self.direction == "send" and number in {_SYS_SENDMSG, _SYS_SENDTO}:
                if number == _SYS_SENDMSG:
                    encoded, descriptors = self._message(address, received_length=None)
                else:
                    size = int(call.arguments[2])
                    if not 0 < size <= MAX_MESSAGE_BYTES:
                        return False
                    encoded, descriptors = self._memory(address, size), ()
                if self._selected(encoded):
                    if self.before_send is not None:
                        if self._before_called:
                            raise AssertionError("the actual selected protocol send was attempted twice")
                        self.before_send()
                        self._before_called = True
                    self._message_call = (number, descriptor, address, len(encoded))
                    self._selected_message = (encoded, descriptors, descriptor, None)
                    return self.message_when == "before"
        elif info.operation == 2 and self._message_call is not None:
            number, descriptor, address, expected_size = self._message_call
            self._message_call = None
            result = int(info.call.exit.result)
            if info.call.exit.is_error or result <= 0:
                return False
            if self.direction == "receive":
                encoded, descriptors = self._message(address, received_length=result)
                if not self._selected(encoded):
                    return False
                self._selected_message = (encoded, descriptors, descriptor, result)
                return True
            if self._selected_message is None or result != expected_size:
                raise AssertionError("the actual selected protocol send was incomplete")
            encoded, descriptors, _, _ = self._selected_message
            self._selected_message = (encoded, descriptors, descriptor, result)
            return True
        return False

    def _record(self) -> None:
        self._require_lifetime()
        if self._selected_message is None:
            raise AssertionError("the actual selected protocol message is absent")
        encoded, descriptors, descriptor, result = self._selected_message
        path = Path(f"/proc/{self.issuer.pid}/fd/{descriptor}")
        info = path.stat()
        if not stat.S_ISSOCK(info.st_mode):
            raise AssertionError("the actual protocol descriptor is not a kernel socket")
        if self.schema == OFFER_SCHEMA and not descriptors or self.schema != OFFER_SCHEMA and descriptors:
            raise AssertionError("the actual protocol ancillary descriptors differ from the expected message class")
        objects = []
        for received in descriptors:
            item = Path(f"/proc/{self.issuer.pid}/fd/{received}")
            actual = item.stat()
            objects.append((received, actual.st_dev, actual.st_ino, os.readlink(item)))
        self._observation = PeerMessageObservation(self.issuer.pid, self.start_token, self.boot_id, self.schema, self.direction, self.message_when, descriptor, info.st_ino, hashlib.sha256(encoded).hexdigest(), len(encoded), result, descriptors, tuple(objects))
        logger.bind(native_peer_schema=self.schema, native_peer_direction=self.direction, native_peer_boundary=self.message_when, process_id=self.issuer.pid).info("Actual production protocol process stopped at its selected kernel message boundary")
        self._hit.set()

    def wait(self, timeout: float = 900) -> PeerMessageObservation:
        result = self.wait_observation(timeout)
        if not isinstance(result, PeerMessageObservation):
            raise AssertionError("the native peer barrier did not observe its actual message")
        return result


def _descriptor_snapshot(pid: int) -> tuple[tuple[int, int, int, str], ...]:
    result = []
    for path in sorted(Path(f"/proc/{pid}/fd").iterdir(), key=lambda item: int(item.name)):
        info = path.stat()
        result.append((int(path.name), info.st_dev, info.st_ino, os.readlink(path)))
    return tuple(result)


def _stopped_receiver(pid: int, start_token: str, pidfd: int, descriptors: tuple[tuple[int, int, int, str], ...]) -> dict[str, object]:
    """Prove the exact live receiver cannot yet observe userspace messages."""
    poll = select.poll()
    poll.register(pidfd, select.POLLIN)
    if poll.poll(0) or _token(pid) != start_token:
        raise AssertionError("the receiver lifetime ended before the original boundary observation")
    state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    if state not in {"T", "t"} or _descriptor_snapshot(pid) != descriptors:
        raise AssertionError("the receipt-free receiver is not still stopped with its exact descriptors")
    return {"pid": pid, "start_token": start_token, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "state": state, "descriptor_sha256": hashlib.sha256(canonical(descriptors)).hexdigest(), "pidfd_alive": True}


def _assert_no_target_admission(context: NativeNegativeContext, *, running_pid: int | None = None) -> None:
    state = Path(context.enrollment.target.owner.path).parent
    config = BootstrapConfig.read(Path(context.enrollment.target.bootstrap_config.path))
    journals = [item for item in context.enrollment.target.mounts if item.destination == str(config.journal_root) and item.writable]
    if len(journals) != 1 or set(os.listdir(state)) - {".supervisor-owner.lock", "logs"} or os.listdir(journals[0].source):
        raise AssertionError("the prelaunch target created a native consumer claim or operation")
    if running_pid is not None and Path(f"/proc/{running_pid}/task/{running_pid}/children").read_text().strip():
        raise AssertionError("the actual prelaunch target launched a child")
    for path in (state / "logs").glob("recovery-epoch-bootstrap*.log"):
        if "Recovery custody acknowledged" in path.read_text():
            raise AssertionError("the actual consumer accepted an unauthorized current grant")


def _assert_received_objects(context: NativeNegativeContext, observation: PeerMessageObservation, issuer_pid: int) -> None:
    expected = {(item.device, item.inode): item.kind for item in _consumer_configuration(context).transferred_resources}
    if len(observation.descriptor_objects) != len(expected) or {(device, inode) for _, device, inode, _ in observation.descriptor_objects} != set(expected):
        raise AssertionError("actual recvmsg did not install the exact complete original object set")
    for descriptor, device, inode, path in observation.descriptor_objects:
        if expected[device, inode] == "network-namespace":
            if path != f"net:[{inode}]":
                raise AssertionError("actual recvmsg namespace object differs")
        else:
            lock = inspect_kernel_lock(observation.process_id, descriptor, device, inode)
            if lock is None or lock.mode != "WRITE" or lock.pid != issuer_pid:
                raise AssertionError("actual recvmsg did not retain the issuer's exclusive OFD")


def _stop_target_after_failure(context: NativeNegativeContext) -> None:
    state = _target_state(context)
    if state.get("Running") is True:
        target = DockerTraceProcess(context)
        try:
            target.kill()
            target.wait(30)
        finally:
            target.close()


def _run_peer_case(context: NativeNegativeContext, case: str) -> dict[str, object]:
    require_native_context(context)
    if case not in REQUIRED_PEER_CASES:
        raise ValueError("a reviewed production peer refusal is required")
    if _target_state(context).get("Running") is not False or PEER_SOCKET.exists() or PEER_SOCKET.is_symlink():
        raise AssertionError("a previous target lifetime or issuer endpoint remains; never reset it to retry")
    epoch_before = _epoch_snapshot(context.epoch_root)
    observations: dict[str, object] = {"case": case}
    log = context.fixture_root / ("native-peer-" + case + ".log")
    issuer = None
    host_barrier = None
    target_barrier = None
    target: DockerTraceProcess | None = None
    replacement: DockerTraceProcess | None = None
    paused_snapshot: tuple[tuple[int, int, int, str], ...] | None = None
    unacknowledged_snapshot: tuple[tuple[int, int, int, str], ...] | None = None
    with ExitStack() as held:
        output = held.enter_context(log.open("xb"))
        try:
            issuer = subprocess.Popen([sys.executable, "-B", "-m", "auto_coder.management.recovery_epoch"], stdout=output, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

            def stop_receiver_before_offer() -> None:
                nonlocal target, paused_snapshot
                target = DockerTraceProcess(context)
                target.pause()
                paused_snapshot = _descriptor_snapshot(target.pid)
                _assert_no_target_admission(context, running_pid=target.pid)

            receiving_offer = case == "received-rights-without-receipt-refused"
            challenge = case in {"changed-target-lifetime-refused", "foreign-acquirer-sh-refused"}
            host_barrier = KernelPeerMessageBarrier(
                issuer,
                context=context,
                schema=CHALLENGE_SCHEMA if challenge else OFFER_SCHEMA,
                direction="receive" if challenge else "send",
                when="before" if receiving_offer else "after",
                before_send=stop_receiver_before_offer if case == "queued-rights-without-receipt-refused" else None,
            )
            host_barrier.start()
            host_barrier.resume_initial()
            _wait(lambda: PEER_SOCKET.exists() and "Fixed recovery issuer endpoint is listening" in log.read_text(), "the actual fixed issuer endpoint is listening", timeout=120)
            _docker("start", context.enrollment.target.management_id)
            observation = host_barrier.wait(timeout=600)
            observations["issuer_syscall"] = asdict(observation)
            assert_unreserved_epoch(context.epoch_root)
            if case == "queued-rights-without-receipt-refused":
                if target is None or paused_snapshot is None or _descriptor_snapshot(target.pid) != paused_snapshot or observation.syscall_result != observation.bytes_observed:
                    raise AssertionError("the successful native send did not precede actual receiver custody")
                observations["receiver_before_and_after_enqueue"] = paused_snapshot
                _assert_no_target_admission(context, running_pid=target.pid)
                unacknowledged_snapshot = paused_snapshot
                host_barrier.release()
            elif receiving_offer:
                target = DockerTraceProcess(context)
                target_barrier = KernelPeerMessageBarrier(target, context=context, schema=OFFER_SCHEMA, direction="receive")
                target_barrier.start()
                target_barrier.resume_initial()
                host_barrier.release()
                received = target_barrier.wait(timeout=90)
                _assert_received_objects(context, received, issuer.pid)
                observations["receiver_syscall"] = asdict(received)
                assert_unreserved_epoch(context.epoch_root)
                _assert_no_target_admission(context, running_pid=target.pid)
                unacknowledged_snapshot = _descriptor_snapshot(target.pid)
            elif case == "changed-target-lifetime-refused":
                target = DockerTraceProcess(context)
                observations["original_target_lifetime"] = {"pid": target.pid, "start_token": target.start_token}
                target.kill()
                target.wait(30)
                _docker("start", context.enrollment.target.management_id)
                replacement = DockerTraceProcess(context)
                if (replacement.pid, replacement.start_token) == (target.pid, target.start_token):
                    raise AssertionError("the actual target restart did not create a new lifetime")
                observations["replacement_target_lifetime"] = {"pid": replacement.pid, "start_token": replacement.start_token}
                _assert_no_target_admission(context, running_pid=replacement.pid)
                host_barrier.release()
            else:
                target = DockerTraceProcess(context)
                original = next(item for item in _consumer_configuration(context).original_fences if item.name.startswith("owner-"))
                observations["foreign_original_custody"] = held.enter_context(_foreign_acquirer(target.pid, original.device, original.inode, "READ"))
                host_barrier.release()
            # Trace ownership has ended before ordinary Popen wait/poll.
            issuer.wait(timeout=120)
            if issuer.returncode != 2:
                raise AssertionError("the actual issuer did not refuse the incomplete native custody")
            if unacknowledged_snapshot is not None:
                assert target is not None
                # Keep the exact live receiver blocked until the issuer's
                # genuine receipt timeout refuses. Killing it beforehand would
                # let a dead-peer check hide missing acknowledgment enforcement.
                target.check()
                observations["live_receiver_after_issuer_refusal"] = _stopped_receiver(target.pid, target.start_token, target.pidfd, unacknowledged_snapshot)
                assert_unreserved_epoch(context.epoch_root)
                _assert_no_target_admission(context, running_pid=target.pid)
                observations.update(receiver_alive_until_issuer_refusal=True, issuer_refused_before_receiver_termination=True)
                if target_barrier is not None:
                    target_barrier.kill()
                else:
                    target.kill()
                    target.wait(30)
            _wait(lambda: _target_state(context).get("Running") is False, "the actual refused consumer exits", timeout=90)
            _assert_no_target_admission(context)
            if PEER_SOCKET.exists() or PEER_SOCKET.is_symlink():
                raise AssertionError("the original refusing issuer left its endpoint; never unlink it to retry")
            if _epoch_snapshot(context.epoch_root) != epoch_before or "One consumed recovery epoch grant send completed" in log.read_text():
                raise AssertionError("incomplete native peer custody published or dispatched an epoch")
            observations.update(issuer_returncode=issuer.returncode, epoch_unreserved=True, issuer_endpoint_removed_by_original=True, consumer_grant_accepted=False, no_child_or_admission=True)
            return observations
        finally:
            if target_barrier is not None:
                target_barrier.close()
            if host_barrier is not None:
                host_barrier.close()
            if issuer is not None and issuer.poll() is None:
                issuer.kill()
                issuer.wait(timeout=30)
            _stop_target_after_failure(context)
            for process in (replacement, target):
                if process is not None:
                    process.close()


def run_native_peer_cases(context: NativeNegativeContext, preserved_snapshot: Callable[[], object]) -> list[str]:
    """Require actual peer refusal before every original epoch publication."""
    require_native_context(context)
    observations = []
    for case in sorted(REQUIRED_PEER_CASES):
        preserved = preserved_snapshot()
        observation = _run_peer_case(context, case)
        if preserved_snapshot() != preserved:
            raise AssertionError("actual peer refusal changed original records, OAuth or unrelated workers")
        observations.append(observation)
        logger.bind(native_peer_case=case, epoch_unreserved=True, consumer_grant_accepted=False).info("Actual production peer refusal preserved original state")
    destination = context.fixture_root / "native-peer-observations.json"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical({"schema": "actual-recovery-native-peer-faults-v1", "cases": observations}))
        stream.flush()
        os.fsync(stream.fileno())
    return [str(item["case"]) for item in observations]
