"""Actual refusal injections for the separately authorized recovery epoch VM.

The calling driver supplies only process orchestration and a preservation
projection. Every authority decision still comes from the original production
issuer or the separate read-only containment CLI. No injection runs after an
original epoch publication, and an unexpected publication is never repaired.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, ContextManager, Iterator

from scripts.test_management_containment_closed_host import MARKER, read_authorization
from scripts.test_management_containment_enrollment_host import _docker, _systemctl, _write_private

from auto_coder.logger_config import get_logger
from auto_coder.management.containment_kernel_subobservation import _holder_start_token, _require_capture_host
from auto_coder.management.containment_route_manifest import _systemd_catalog, _systemd_unit
from auto_coder.management.fresh_host import _evidence
from auto_coder.management.fresh_host_observation import _engine_id, assert_host_listener_absent
from auto_coder.management.installed_bootstrap_mount_profile import _inspect
from auto_coder.management.original_attempts import canonical
from auto_coder.management.recovery_containment_model import RECOVERY_ENROLLMENT_ROOT, NamedDescriptor, RecoveryEnrollment, RecoveryLock, RecoveryOriginal, RecoveryResources
from auto_coder.management.recovery_containment_profile import digest
from auto_coder.management.recovery_epoch_peer import inspect_kernel_lock
from auto_coder.management.recovery_epoch_state import EPOCH_ROOT

logger = get_logger(__name__)
_FIXTURE_ROOT = Path("/var/lib/auto-coder/recovery-epoch-fixtures")
_UNRESERVED_ENTRIES = frozenset({"epoch.lock", "inventory.json"})
REQUIRED_NEGATIVE_CASES = frozenset(
    {
        "busy-original-owner-refused",
        "busy-controller-fence-refused",
        "replaced-original-owner-refused",
        "replaced-epoch-lock-refused",
        "unknown-docker-management-route-refused",
        "omitted-installed-controller-refused",
        "unmasked-installed-restarter-refused",
        "retained-old-network-task-and-listener-refused",
        "old-published-listener-refused",
    }
)
REQUIRED_REPORT_CASES = frozenset(
    {
        "native-current-containment-reverified",
        "forged-native-containment-refused",
        "cross-deployment-containment-refused",
        "stale-native-containment-refused",
    }
)


@dataclass(frozen=True)
class NativeNegativeContext:
    enrollment: RecoveryEnrollment
    fixture_root: Path
    epoch_root: Path


class EpochAlreadyPublished(AssertionError):
    """The fresh fixture has become uncertain and must never be reused."""


def assert_unreserved_epoch(root: Path) -> None:
    """Reject every record, head, partial name or unexpected epoch object."""
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise EpochAlreadyPublished("the fixed epoch directory is no longer canonical; never repair or retry")
    directory = root.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700 or {path.name for path in root.iterdir()} != _UNRESERVED_ENTRIES:
        raise EpochAlreadyPublished("the epoch has an original publication, partial state or changed root; never repair or retry")
    for name in _UNRESERVED_ENTRIES:
        info = (root / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise EpochAlreadyPublished("an original epoch input is missing or replaced by an unsupported object; never repair or retry")


def _projection(path: Path) -> tuple[object, ...]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise AssertionError("the fixture selected a nonregular original")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def _epoch_snapshot(root: Path) -> str:
    assert_unreserved_epoch(root)
    info = root.lstat()
    return digest(((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid), tuple((name, _projection(root / name)) for name in sorted(_UNRESERVED_ENTRIES))))


def require_native_context(context: NativeNegativeContext, *, unreserved: bool = True) -> None:
    """Verify the original VM approval before a separately bounded scenario.

    Late interruption fixtures select ``unreserved=False`` only when they also
    verify their exact original publication prefix; it never permits a reset.
    """
    if context.epoch_root != EPOCH_ROOT or context.fixture_root == _FIXTURE_ROOT or not context.fixture_root.is_relative_to(_FIXTURE_ROOT) or context.fixture_root != context.fixture_root.resolve(strict=True):
        raise PermissionError("native refusal injections require the fixed disposable fixture roots")
    _require_capture_host()
    authority = read_authorization(MARKER)
    selected = context.enrollment
    if (
        authority["authorization_id"] != selected.authorization_id
        or authority["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        or authority["machine_id"] != Path("/etc/machine-id").read_text().strip()
        or authority["engine_id"] != selected.engine_id
        or authority["engine_id"] != _engine_id()
    ):
        raise PermissionError("the original separate VM authorization no longer selects this fixture")
    RecoveryEnrollment.parse(json.loads(json.dumps(asdict(selected))))
    if not any(original.state == "started" for original in selected.originals) or not selected.watchers:
        raise AssertionError("native negatives require actual original and controller resources")
    for lock in (*(original.owner for original in selected.originals if original.owner is not None), *(watcher.lock for watcher in selected.watchers), selected.target.owner):
        if not Path(lock.path).is_relative_to(context.fixture_root):
            raise PermissionError("a selected negative resource is outside the authorized fixture")
    if unreserved:
        assert_unreserved_epoch(context.epoch_root)


@contextmanager
def _replacement(epoch_root: Path, path: Path, backup: Path, contents: bytes) -> Iterator[None]:
    """Retain the original inode; restore only our still-unchanged injection."""
    assert_unreserved_epoch(epoch_root)
    original = _projection(path)
    if path != path.resolve(strict=True) or backup.exists() or backup.is_symlink() or backup.parent.stat().st_dev != path.parent.stat().st_dev:
        raise AssertionError("fixture replacement requires a unique same-filesystem backup")
    path.rename(backup)
    replacement: tuple[object, ...] | None = None
    try:
        _write_private(path, contents)
        replacement = _projection(path)
        if replacement[:2] == original[:2]:
            raise AssertionError("fixture replacement did not select a different inode")
        yield
    finally:
        # A publication makes even an epoch-lock restoration an impermissible
        # retry/reset. Keep all original and injected objects for diagnosis.
        assert_unreserved_epoch(epoch_root)
        if _projection(backup) != original:
            raise AssertionError("the retained original changed during the negative fixture")
        if replacement is not None:
            if _projection(path) != replacement:
                raise AssertionError("the replacement changed; refusing fixture cleanup")
            path.unlink()
        elif path.exists() or path.is_symlink():
            raise AssertionError("an incomplete replacement is retained for diagnosis")
        backup.rename(path)
        if _projection(path) != original:
            raise AssertionError("fixture cleanup did not restore the exact original object")


@contextmanager
def _busy_fence(selected: RecoveryLock) -> Iterator[None]:
    descriptor = os.open(selected.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (selected.device, selected.inode) or not stat.S_ISREG(info.st_mode):
            raise AssertionError("fixture contention did not open the selected original")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock = inspect_kernel_lock(os.getpid(), descriptor, selected.device, selected.inode)
        if lock is None or lock.mode != "WRITE" or lock.pid != os.getpid():
            raise AssertionError("fixture contender lacks its real independently acquired EX")
        yield
        if inspect_kernel_lock(os.getpid(), descriptor, selected.device, selected.inode) != lock:
            raise AssertionError("the actual fixture contention was released during refusal")
    finally:
        os.close(descriptor)


@contextmanager
def _live_process(command: tuple[str, ...], *, pass_fds: tuple[int, ...] = ()) -> Iterator[subprocess.Popen[bytes]]:
    """Bound a real injection process to its original pidfd and ready message."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds)
    lifetime = -1
    try:
        lifetime = os.pidfd_open(process.pid, 0)
        if process.stdout is None:
            raise AssertionError("fixture process has no readiness pipe")
        message = b""
        deadline = time.monotonic() + 15
        while b"\n" not in message and len(message) <= 64:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select((process.stdout,), (), (), left)[0]:
                raise AssertionError("actual negative process never reached its bounded readiness barrier")
            chunk = os.read(process.stdout.fileno(), 65 - len(message))
            if not chunk:
                raise AssertionError("actual negative process exited before readiness")
            message += chunk
        if message != b"READY\n" or process.poll() is not None:
            raise AssertionError("actual negative process did not report exact readiness")
        yield process
        if process.poll() is not None:
            raise AssertionError("the actual injected process exited during refusal")
    finally:
        if process.poll() is None:
            if lifetime >= 0:
                signal.pidfd_send_signal(lifetime, signal.SIGTERM)
            else:
                process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            if lifetime >= 0:
                signal.pidfd_send_signal(lifetime, signal.SIGKILL)
            else:
                process.kill()
            process.communicate(timeout=10)
        if lifetime >= 0:
            os.close(lifetime)


_NAMESPACE_LISTENER = """
import os
import socket
import sys
os.setns(int(sys.argv[1]), os.CLONE_NEWNET)
os.close(int(sys.argv[1]))
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(('127.0.0.1', 8080))
listener.listen(1)
print('READY', flush=True)
sys.stdin.buffer.read(1)
"""


@contextmanager
def _retained_namespace_listener(original: RecoveryOriginal) -> Iterator[None]:
    pin = original.namespace_pin
    if pin is None or original.prior_evidence is None:
        raise AssertionError("a genuine retained original namespace is required")
    if _holder_start_token(pin.holder_pid) != pin.holder_start_token:
        raise AssertionError("the original namespace holder changed before injection")
    descriptor = os.open(f"/proc/{pin.holder_pid}/fd/{pin.holder_fd}", os.O_RDONLY | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (pin.device, pin.inode) or os.readlink(f"/proc/self/fd/{descriptor}") != pin.namespace:
            raise AssertionError("the injected process did not receive the exact retained namespace")
        with _live_process((sys.executable, "-I", "-B", "-c", _NAMESPACE_LISTENER, str(descriptor)), pass_fds=(descriptor,)) as process:
            proc = Path("/proc") / str(process.pid)
            prior, _, _ = _evidence(Path(original.prior_evidence.path))
            if os.readlink(proc / "ns/net") != pin.namespace or "0::" + prior.cgroup_path in (proc / "cgroup").read_text().splitlines():
                raise AssertionError("the real task did not remain outside the old container in its retained network")
            rows = (proc / "net/tcp").read_text().splitlines()[1:]
            if not any(row.split()[1].endswith(":1F90") and row.split()[3] == "0A" for row in rows):
                raise AssertionError("the actual retained namespace has no injected listening socket")
            yield
    finally:
        os.close(descriptor)


@contextmanager
def _published_listener(original: RecoveryOriginal, target_port: int) -> Iterator[None]:
    if original.prior_evidence is None:
        raise AssertionError("the old listener requires actual original capture")
    prior, _, _ = _evidence(Path(original.prior_evidence.path))
    if prior.published_port == target_port:
        raise AssertionError("the fixture target reused an original published port")
    assert_host_listener_absent(prior.published_port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", prior.published_port))
        listener.listen(1)
        try:
            assert_host_listener_absent(prior.published_port)
        except ValueError:
            pass
        else:
            raise AssertionError("the actual host listener injection was not visible")
        yield
    assert_host_listener_absent(prior.published_port)


@contextmanager
def _extra_docker(enrollment: RecoveryEnrollment) -> Iterator[None]:
    image = enrollment.originals[0].management_image_id
    identifier = _docker("create", "--name", "ac-recovery-unknown-" + uuid.uuid4().hex, "--restart=no", "--network=none", "--read-only", "--entrypoint", "python", image, "-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json")
    if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
        raise AssertionError("the actual extra Docker object has no complete identity")
    try:
        observed = _inspect(identifier)
        state = observed.get("State")
        if observed.get("Image") != image or not isinstance(state, dict) or state.get("Status") != "created" or state.get("Pid") != 0 or state.get("Running") is not False or observed.get("RestartCount") != 0:
            raise AssertionError("the actual unknown manager route is not a pristine created object")
        yield
        if _inspect(identifier) != observed:
            raise AssertionError("the issuer changed the injected Docker object")
    finally:
        current = _inspect(identifier)
        state = current.get("State")
        if not isinstance(state, dict) or state.get("Status") != "created" or state.get("Pid") != 0 or state.get("Running") is not False or current.get("Image") != image:
            raise AssertionError("the injected object acquired a lifetime; refusing destructive cleanup")
        _docker("rm", identifier)


@contextmanager
def _omitted_controller(context: NativeNegativeContext, backups: Path) -> Iterator[None]:
    path = RECOVERY_ENROLLMENT_ROOT / (context.enrollment.enrollment_id + ".routes.json")
    previous = json.loads(path.read_bytes())
    routes = previous.get("systemd_routes")
    selected = context.enrollment.watchers[0].unit
    if not isinstance(routes, list) or sum(isinstance(route, dict) and route.get("unit") == selected for route in routes) != 1:
        raise AssertionError("the approved full manifest does not contain the actual selected watcher")
    previous["systemd_routes"] = [route for route in routes if route["unit"] != selected]
    with _replacement(context.epoch_root, path, backups / "approved-routes.original", canonical(previous)):
        yield


@contextmanager
def _unmasked_restarter(context: NativeNegativeContext) -> Iterator[None]:
    """Add an actual runnable restarter without touching original mask inodes."""
    catalog = _systemd_catalog()
    executable = shutil.which("docker")
    if executable is None:
        raise AssertionError("the original local Docker executable is unavailable")
    executable = str(Path(executable).resolve(strict=True))
    if any(character in executable for character in ' \t\n\r%"\\'):
        raise AssertionError("the native Docker executable is outside the finite systemd profile")
    name = "auto-coder-recovery-negative-" + uuid.uuid4().hex + ".service"
    path = Path("/run/systemd/system") / name
    contents = f"[Unit]\nDescription=Disposable management recovery restarter negative\n[Service]\nType=oneshot\nExecStart={executable} start {context.enrollment.originals[0].management_id}\n".encode()
    _write_private(path, contents)
    identity = _projection(path)
    try:
        _systemctl("daemon-reload")
        actual = _systemd_unit(name)
        if actual.inhibited or not actual.management_candidate or actual.main_pid != 0 or actual.active_state != "inactive" or name not in _systemd_catalog():
            raise AssertionError("the actual installed restarter is not the intended unmasked inactive route")
        yield
    finally:
        if _projection(path) != identity:
            raise AssertionError("the fixture restarter source changed; refusing cleanup")
        path.unlink()
        _systemctl("daemon-reload")
        deadline = time.monotonic() + 15
        while _systemd_catalog() != catalog:
            if time.monotonic() >= deadline:
                raise AssertionError("the original systemd catalog did not return after exact fixture removal")
            time.sleep(0.1)


def _run_case(context: NativeNegativeContext, label: str, injection: Callable[[], ContextManager[None]], run_refusal: Callable[[str], None], preserved_snapshot: Callable[[], object]) -> None:
    assert_unreserved_epoch(context.epoch_root)
    baseline = preserved_snapshot()
    epoch_before = _epoch_snapshot(context.epoch_root)
    logger.info("Starting native prepublication refusal case: {}", label)
    with injection():
        injected = preserved_snapshot()
        epoch_injected = _epoch_snapshot(context.epoch_root)
        try:
            run_refusal(label)
        finally:
            assert_unreserved_epoch(context.epoch_root)
            if _epoch_snapshot(context.epoch_root) != epoch_injected:
                raise AssertionError("the refused issuer changed an original epoch input")
            if preserved_snapshot() != injected:
                raise AssertionError("the refused issuer changed original/OAuth/worker state during " + label)
    if _epoch_snapshot(context.epoch_root) != epoch_before or preserved_snapshot() != baseline:
        raise AssertionError("the exact injected fixture state did not restore after " + label)
    logger.info("Native prepublication refusal and preservation verified: {}", label)


def run_native_negatives(context: NativeNegativeContext, run_refusal: Callable[[str], None], preserved_snapshot: Callable[[], object]) -> list[str]:
    """Exercise actual issuer refusals before its first irreversible publication."""
    require_native_context(context)
    backups = context.fixture_root / "native-negative-originals"
    backups.mkdir(mode=0o700)
    original = next(item for item in context.enrollment.originals if item.state == "started")
    if original.owner is None:
        raise AssertionError("the original started owner identity is absent")
    selected_owner = original.owner
    owner = Path(selected_owner.path)
    epoch = context.epoch_root / "epoch.lock"
    injections: tuple[tuple[str, Callable[[], ContextManager[None]]], ...] = (
        ("busy-original-owner-refused", lambda: _busy_fence(selected_owner)),
        ("busy-controller-fence-refused", lambda: _busy_fence(context.enrollment.watchers[0].lock)),
        ("replaced-original-owner-refused", lambda: _replacement(context.epoch_root, owner, backups / "owner.original", owner.read_bytes())),
        ("replaced-epoch-lock-refused", lambda: _replacement(context.epoch_root, epoch, backups / "epoch.original", epoch.read_bytes())),
        ("unknown-docker-management-route-refused", lambda: _extra_docker(context.enrollment)),
        ("omitted-installed-controller-refused", lambda: _omitted_controller(context, backups)),
        ("unmasked-installed-restarter-refused", lambda: _unmasked_restarter(context)),
        ("retained-old-network-task-and-listener-refused", lambda: _retained_namespace_listener(original)),
        ("old-published-listener-refused", lambda: _published_listener(original, context.enrollment.target.published_port)),
    )
    cases = []
    for label, injection in injections:
        _run_case(context, label, injection, run_refusal, preserved_snapshot)
        cases.append(label)
    if set(cases) != REQUIRED_NEGATIVE_CASES:
        raise AssertionError("the native refusal matrix is incomplete")
    return cases


def _issuer_resources(enrollment: RecoveryEnrollment, issuer_pid: int) -> RecoveryResources:
    """Read actual stopped issuer descriptors; the production verifier rechecks."""
    if type(issuer_pid) is not int or issuer_pid <= 1:
        raise AssertionError("an exact actual issuer process is required")
    proc = Path("/proc") / str(issuer_pid)
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] not in {"T", "t"}:
        raise AssertionError("native report probes require the actual prepublication SIGSTOP/ptrace barrier")
    start = _holder_start_token(issuer_pid)
    descriptors = tuple((proc / "fd").iterdir())
    if len(descriptors) > 4096 or any(not item.name.isdecimal() for item in descriptors):
        raise AssertionError("the actual issuer descriptor inventory exceeds the fixture bound")

    def find(name: str, path: str, device: int, inode: int) -> NamedDescriptor:
        matches = []
        for candidate in descriptors:
            info = candidate.stat()
            if (info.st_dev, info.st_ino) == (device, inode) and os.readlink(candidate) == path:
                matches.append(int(candidate.name))
        if len(matches) != 1:
            raise AssertionError("the actual issuer resource is absent or ambiguous: " + name)
        return NamedDescriptor(name, matches[0])

    owners = tuple(find(item.generation_id, item.owner.path, item.owner.device, item.owner.inode) for item in enrollment.originals if item.owner is not None)
    controllers = tuple(find(item.unit, item.lock.path, item.lock.device, item.lock.inode) for item in enrollment.watchers)
    namespaces = tuple(find(item.generation_id, item.namespace_pin.namespace, item.namespace_pin.device, item.namespace_pin.inode) for item in enrollment.originals if item.namespace_pin is not None)
    return RecoveryResources(issuer_pid, start, Path("/proc/sys/kernel/random/boot_id").read_text().strip(), owners, controllers, namespaces)


def _containment_cli(directory: Path, label: str, enrollment: str, resources: Path, *, previous: Path | None = None, refusal: str | None = None) -> dict[str, object]:
    command = [sys.executable, "-B", "-m", "auto_coder.management.recovery_containment", "verify" if previous is not None else "observe", "--enrollment", enrollment, "--resources", str(resources)]
    if previous is not None:
        command.extend(("--previous", str(previous)))
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    _write_private(directory / (label + ".log"), (completed.stdout + completed.stderr).encode())
    value = json.loads(completed.stdout.splitlines()[-1])
    if not isinstance(value, dict) or any(value.get(name) is not False for name in ("grant_issued", "history_asserted", "admission_created")):
        raise AssertionError("the actual containment CLI returned an incomplete or authorizing result")
    if refusal is None:
        if completed.returncode != 0 or value.get("status") != "observed-current-recovery-containment":
            raise AssertionError("the genuine native containment positive control was unavailable: " + str(value.get("reason")))
    elif completed.returncode != 2 or value.get("status") != "unavailable" or refusal not in str(value.get("reason")):
        raise AssertionError("the actual containment verifier did not reject the injected condition: " + label + " " + str(value.get("reason")))
    return value


def verify_native_containment_reports(context: NativeNegativeContext, issuer_pid: int) -> list[str]:
    """Reobserve saved/forged reports under genuine paused issuer custody."""
    require_native_context(context)
    directory = context.fixture_root / "native-containment-report-probes"
    directory.mkdir(mode=0o700)
    lifetime = os.pidfd_open(issuer_pid, 0)
    try:
        epoch = _epoch_snapshot(context.epoch_root)
        resources = _issuer_resources(context.enrollment, issuer_pid)
        resource_path = directory / "actual-issuer-resources.json"
        _write_private(resource_path, canonical(asdict(resources)))
        enrollment = context.enrollment.enrollment_id
        poller = select.poll()
        poller.register(lifetime, select.POLLIN)

        def check_barrier() -> None:
            if poller.poll(0) or _epoch_snapshot(context.epoch_root) != epoch or _issuer_resources(context.enrollment, issuer_pid) != resources:
                raise AssertionError("the actual issuer advanced or lost custody during native report probes")

        def probe(label: str, *, previous: Path | None = None, refusal: str | None = None) -> dict[str, object]:
            check_barrier()
            try:
                return _containment_cli(directory, label, enrollment, resource_path, previous=previous, refusal=refusal)
            finally:
                check_barrier()

        try:
            report = probe("original-observer")
            previous = directory / "original-observation.json"
            _write_private(previous, canonical(report))
            probe("separate-current-verifier", previous=previous)
            cases = ["native-current-containment-reverified"]
            for label, field in (("forged-native-containment-refused", "target"), ("cross-deployment-containment-refused", "deployment")):
                forged = json.loads(json.dumps(report))
                if field == "target":
                    forged["facts"]["target"]["pid"] += 1
                else:
                    forged["facts"]["enrollment"]["enrollment"]["deployment_id"] = "foreign-" + uuid.uuid4().hex
                forged["evidence_sha256"] = digest(forged["facts"])
                path = directory / (label + ".json")
                _write_private(path, canonical(forged))
                probe(label, previous=path, refusal="previous recovery scope/source/resources/current facts differ")
                cases.append(label)
            original = next(item for item in context.enrollment.originals if item.state == "started")
            check_barrier()
            with _retained_namespace_listener(original):
                probe("stale-native-containment-refused", previous=previous, refusal="prior network namespace still has a task")
            cases.append("stale-native-containment-refused")
            probe("restored-current-verifier", previous=previous)
            if set(cases) != REQUIRED_REPORT_CASES:
                raise AssertionError("the actual report-verifier case matrix is incomplete")
            return cases
        finally:
            check_barrier()
    finally:
        os.close(lifetime)
