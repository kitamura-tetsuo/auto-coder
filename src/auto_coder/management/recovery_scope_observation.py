"""Finite recovery selection and actual current flock custody, without admission.

This deliberately incomplete component does not establish source/route closure,
task or namespace absence, historical exclusion, or full recovery containment.
It never creates a lock, acquires a flock, or invokes a controller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import stat
import subprocess
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from loguru import logger

from .archived_exclusions import unique_pairs
from .containment_enrollment import _approved_directory, _file_identity, _protected, _trusted_ancestors
from .containment_kernel_subobservation import _holder_start_token, _require_capture_host
from .containment_observation import _docker_ids, _docker_read
from .docker_inspection import canonical_inspection
from .fresh_host_observation import _assert_pid_flock

RECOVERY_ENROLLMENT_ROOT = Path("/var/lib/auto-coder/approved-recovery-scopes")
_PROFILE = "ordinary-recovery-custody-component-v1"
_MISSING = (
    "image-package-source-and-effective-configuration-closure",
    "complete-installed-controller-and-restart-route-inhibition",
    "original-task-cgroup-namespace-custody-and-listener-absence",
    "exact-target-source-root-and-namespace-disjointness",
    "independent-original-provenance-join",
    "native-initial-namespace-systemd-docker-acceptance",
)
_MAX_AGE_NS = 30_000_000_000


class RecoveryScopeUnavailable(ValueError):
    """No usable current component observation; never an authorization result."""


def _record(value: object, keys: tuple[str, ...]) -> dict:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise RecoveryScopeUnavailable("recovery record schema differs")
    return value


def _text(value: object, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise RecoveryScopeUnavailable("recovery identity is malformed")
    return value


def _positive(value: object, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**64 - 1:
        raise RecoveryScopeUnavailable("recovery numeric identity is malformed")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096 or "\x00" in value or str(Path(value)) != value or ".." in Path(value).parts:
        raise RecoveryScopeUnavailable("recovery resource path is malformed")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class CustodySelection:
    """An enrolled existing resource and external holder; not proof of custody."""

    path: str
    device: int
    inode: int
    holder_pid: int
    holder_start_token: str
    holder_fd: int
    holder_boot_id: str

    @classmethod
    def parse(cls, value: object) -> CustodySelection:
        raw = _record(value, tuple(cls.__dataclass_fields__))
        path = _path(raw["path"])
        device, inode, pid = (_positive(raw[key]) for key in ("device", "inode", "holder_pid"))
        fd = _positive(raw["holder_fd"], zero=True)
        if pid >= 2**31 or fd >= 2**31:
            raise RecoveryScopeUnavailable("holder PID or descriptor exceeds the kernel bound")
        token = _text(raw["holder_start_token"], r"[1-9][0-9]{0,19}")
        boot = _text(raw["holder_boot_id"], r"[0-9a-f-]{36}")
        if str(uuid.UUID(boot)) != boot:
            raise RecoveryScopeUnavailable("holder boot identity is malformed")
        return cls(path, device, inode, pid, token, fd, boot)


@dataclass(frozen=True)
class RecoveryPair:
    manager_id: str
    proxy_id: str
    manager_inspection_sha256: str
    proxy_inspection_sha256: str

    @classmethod
    def parse(cls, value: object) -> RecoveryPair:
        raw = _record(value, tuple(cls.__dataclass_fields__))
        return cls(*(_text(raw[key], r"[0-9a-f]{64}") for key in cls.__dataclass_fields__))


@dataclass(frozen=True)
class OriginalSelection:
    pair: RecoveryPair
    owner: CustodySelection


@dataclass(frozen=True)
class WatcherSelection:
    manager_id: str
    config_path: str
    config_sha256: str
    lock: CustodySelection


@dataclass(frozen=True)
class RecoveryEnrollment:
    schema: str
    deployment_id: str
    profile: str
    engine_id: str
    originals: tuple[OriginalSelection, ...]
    target: RecoveryPair
    watchers: tuple[WatcherSelection, ...]

    @classmethod
    def parse(cls, value: object) -> RecoveryEnrollment:
        raw = _record(value, tuple(cls.__dataclass_fields__))
        if raw["schema"] != "approved-recovery-custody-scope-v1" or raw["profile"] != _PROFILE:
            raise RecoveryScopeUnavailable("unsupported recovery component profile")
        deployment = _text(raw["deployment_id"], r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        engine = _text(raw["engine_id"], r"[A-Za-z0-9:_.-]{1,128}")
        if not isinstance(raw["originals"], list) or not 1 <= len(raw["originals"]) <= 16 or not isinstance(raw["watchers"], list) or not 1 <= len(raw["watchers"]) <= 17:
            raise RecoveryScopeUnavailable("recovery membership exceeds finite bounds or is empty")
        originals = []
        for item in raw["originals"]:
            item = _record(item, ("pair", "owner"))
            originals.append(OriginalSelection(RecoveryPair.parse(item["pair"]), CustodySelection.parse(item["owner"])))
        target = RecoveryPair.parse(raw["target"])
        pairs = tuple(item.pair for item in originals) + (target,)
        ids = tuple(identity for pair in pairs for identity in (pair.manager_id, pair.proxy_id))
        if len(set(ids)) != len(ids):
            raise RecoveryScopeUnavailable("original and target membership aliases")
        watchers = []
        for item in raw["watchers"]:
            item = _record(item, tuple(WatcherSelection.__dataclass_fields__))
            watchers.append(WatcherSelection(_text(item["manager_id"], r"[0-9a-f]{64}"), _path(item["config_path"]), _text(item["config_sha256"], r"[0-9a-f]{64}"), CustodySelection.parse(item["lock"])))
        if sorted(item.manager_id for item in watchers) != sorted(pair.manager_id for pair in pairs):
            raise RecoveryScopeUnavailable("watcher membership must exactly cover originals and target")
        locks = tuple(item.owner for item in originals) + tuple(item.lock for item in watchers)
        if len({item.path for item in locks}) != len(locks) or len({(item.device, item.inode) for item in locks}) != len(locks):
            raise RecoveryScopeUnavailable("recovery resources alias")
        return cls(raw["schema"], deployment, raw["profile"], engine, tuple(originals), target, tuple(watchers))


@dataclass(frozen=True)
class CustodyFact:
    selection: CustodySelection
    mode: str
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class BindingFacts:
    bind_roots: tuple[tuple[str, str, int, int, int, int, int], ...]
    watcher_configs: tuple[tuple[int, int, int, int, int], ...]


@dataclass(frozen=True)
class RecoveryCustodyObservation:
    """Component evidence only; deliberately incompatible with legacy reports."""

    schema: str
    status: str
    enrollment: RecoveryEnrollment
    enrollment_sha256: str
    enrollment_identity: tuple[int, int, int, int, int]
    engine_inventory: tuple[str, ...]
    custody: tuple[CustodyFact, ...]
    bindings: BindingFacts
    boot_id: str
    observer_pid: int
    observer_start_token: str
    observation_id: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    incomplete: tuple[str, ...]


def _read_enrollment(deployment_id: str) -> tuple[RecoveryEnrollment, str, tuple[int, int, int, int, int]]:
    _text(deployment_id, r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    _approved_directory(RECOVERY_ENROLLMENT_ROOT, uid=0)
    data, info = _protected(RECOVERY_ENROLLMENT_ROOT / (deployment_id + ".json"), uid=0, size_limit=131072)
    enrollment = RecoveryEnrollment.parse(json.loads(data, object_pairs_hook=unique_pairs))
    if enrollment.deployment_id != deployment_id:
        raise RecoveryScopeUnavailable("deployment-fixed enrollment differs")
    return enrollment, hashlib.sha256(data).hexdigest(), _file_identity(info)


def _snapshot(enrollment: RecoveryEnrollment) -> dict[str, dict[str, object]]:
    if any(os.environ.get(key) for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")):
        raise RecoveryScopeUnavailable("Docker environment override is unsupported")
    if _docker_read(("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")) != "unix:///var/run/docker.sock" or _docker_read(("info", "--format", "{{json .ID}}")) != enrollment.engine_id:
        raise RecoveryScopeUnavailable("local system engine differs")
    pairs = tuple(item.pair for item in enrollment.originals) + (enrollment.target,)
    expected = {identity: digest for pair in pairs for identity, digest in ((pair.manager_id, pair.manager_inspection_sha256), (pair.proxy_id, pair.proxy_inspection_sha256))}
    ids = _docker_ids()
    # Conservatively refuse every unselected object, including workers. No
    # caller classification can hide an omitted stopped management object.
    if len(ids) != len(expected) or set(ids) != set(expected):
        raise RecoveryScopeUnavailable("complete engine membership differs")
    result = {}
    for identity in ids:
        raw = _docker_read(("inspect", "--type", "container", identity))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict) or raw[0].get("Id") != identity:
            raise RecoveryScopeUnavailable("Docker object identity differs")
        current = canonical_inspection(raw[0])
        if _digest(current) != expected[identity]:
            raise RecoveryScopeUnavailable("enrolled full Docker inspection differs")
        result[identity] = current
    if _docker_ids() != ids or _docker_read(("info", "--format", "{{json .ID}}")) != enrollment.engine_id:
        raise RecoveryScopeUnavailable("engine changed during membership observation")
    return result


def _bindings(enrollment: RecoveryEnrollment, snapshot: dict[str, dict[str, object]]) -> BindingFacts:
    pairs = {item.pair.manager_id: item.pair for item in enrollment.originals}
    pairs[enrollment.target.manager_id] = enrollment.target
    roots: list[tuple[str, Path]] = []
    identities: list[tuple[str, str, int, int, int, int, int]] = []
    for pair in pairs.values():
        for identity in (pair.manager_id, pair.proxy_id):
            mounts = snapshot[identity]["Mounts"]
            if not isinstance(mounts, list):
                raise RecoveryScopeUnavailable("selected mount inventory unavailable")
            for mount in mounts:
                if not isinstance(mount, dict) or mount.get("Type") != "bind":
                    raise RecoveryScopeUnavailable("component supports only explicit bind roots")
                source = Path(_path(mount.get("Source")))
                if source != source.resolve(strict=True):
                    raise RecoveryScopeUnavailable("selected bind root is not canonical")
                if any(owner != pair.manager_id and (source == prior or source in prior.parents or prior in source.parents) for owner, prior in roots):
                    raise RecoveryScopeUnavailable("generation or target bind roots overlap")
                info = source.stat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or any(owner != pair.manager_id and (device, inode) == (info.st_dev, info.st_ino) for owner, _, device, inode, _, _, _ in identities):
                    raise RecoveryScopeUnavailable("generation or target bind root identities alias or are unsupported")
                identities.append((pair.manager_id, str(source), *_file_identity(info)))
                roots.append((pair.manager_id, source))
    for original in enrollment.originals:
        mounts = snapshot[original.pair.manager_id]["Mounts"]
        if not isinstance(mounts, list):
            raise RecoveryScopeUnavailable("original state mounts unavailable")
        selected = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/management-child-state" and mount.get("Type") == "bind" and mount.get("RW") is True]
        if len(selected) != 1 or not isinstance(selected[0].get("Source"), str) or str(Path(selected[0]["Source"]) / ".supervisor-owner.lock") != original.owner.path:
            raise RecoveryScopeUnavailable("owner lock is not the actual original state-mount resource")
        if any(isinstance(mount, dict) and isinstance(mount.get("Destination"), str) and mount["Destination"].startswith("/management-child-state/") for mount in mounts):
            raise RecoveryScopeUnavailable("original state resource is shadowed")
    configs = []
    for watcher in enrollment.watchers:
        data, info = _protected(Path(watcher.config_path), uid=0, size_limit=8192)
        if hashlib.sha256(data).hexdigest() != watcher.config_sha256:
            raise RecoveryScopeUnavailable("watcher configuration bytes differ")
        raw = _record(json.loads(data, object_pairs_hook=unique_pairs), ("state_root", "manager_id", "manager_image", "proxy_id", "proxy_image", "config_sha256"))
        pair = pairs[watcher.manager_id]
        if raw["manager_id"] != pair.manager_id or raw["proxy_id"] != pair.proxy_id or raw["manager_image"] != snapshot[pair.manager_id].get("Image") or raw["proxy_image"] != snapshot[pair.proxy_id].get("Image") or str(Path(_path(raw["state_root"])) / "proxy-watch.lock") != watcher.lock.path:
            raise RecoveryScopeUnavailable("watcher does not bind the selected pair and actual controller lock")
        _text(raw["config_sha256"], r"[0-9a-f]{64}")
        configs.append(_file_identity(info))
    return BindingFacts(tuple(identities), tuple(configs))


@contextmanager
def _custody(selection: CustodySelection, mode: str, boot: str, *, expected_uid: int = 0) -> Iterator[CustodyFact]:
    """Observe a real external acquisition, without flocking or duplicating it.

    Opening /proc/PID/fd/N here creates an independent read descriptor; it does
    not inherit the holder's open-file description or its flock. fdinfo binds
    the kernel acquisition to the enrolled FD, not merely another FD on the inode.
    """
    with ExitStack() as stack:
        if selection.holder_boot_id != boot or selection.holder_pid == os.getpid():
            raise RecoveryScopeUnavailable("custody holder is foreign or is the observer")
        path = Path(selection.path)
        if path != path.resolve(strict=True):
            raise RecoveryScopeUnavailable("custody path is not canonical")
        _trusted_ancestors(path.parent, uid=expected_uid)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        stack.callback(os.close, descriptor)
        pidfd = os.pidfd_open(selection.holder_pid, 0)
        stack.callback(os.close, pidfd)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        target = f"/proc/{selection.holder_pid}/fd/{selection.holder_fd}"
        info = os.fstat(descriptor)

        def check() -> None:
            _trusted_ancestors(path.parent, uid=expected_uid)
            if poller.poll(0) or _holder_start_token(selection.holder_pid) != selection.holder_start_token:
                raise RecoveryScopeUnavailable("custody holder exited or lifetime differs")
            current = path.lstat()
            held = os.stat(target)
            if (
                path != path.resolve(strict=True)
                or not stat.S_ISREG(current.st_mode)
                or current.st_uid != expected_uid
                or stat.S_IMODE(current.st_mode) != 0o600
                or (current.st_dev, current.st_ino) != (selection.device, selection.inode)
                or _file_identity(current) != _file_identity(info)
                or _file_identity(os.fstat(descriptor)) != _file_identity(info)
                or (held.st_dev, held.st_ino) != (selection.device, selection.inode)
            ):
                raise RecoveryScopeUnavailable("custody resource changed or is unprotected")
            _assert_pid_flock(selection.holder_pid, descriptor, mode)
            fdinfo = Path(f"/proc/{selection.holder_pid}/fdinfo/{selection.holder_fd}").read_text(encoding="ascii")
            identity = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
            locks = [line.split()[1:] for line in fdinfo.splitlines() if line.startswith("lock:")]
            if len(locks) != 1 or locks[0][1:] != ["FLOCK", "ADVISORY", mode, str(selection.holder_pid), identity, "0", "EOF"]:
                raise RecoveryScopeUnavailable("holder FD is not its independent whole-file flock acquisition")

        check()
        yield CustodyFact(selection, mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        check()


def observe_recovery_custody(deployment_id: str) -> RecoveryCustodyObservation:
    """Read a fixed protected selection and repeat every component observation.

    Raises RecoveryScopeUnavailable rather than returning partial success. This
    component's positive result ALWAYS carries its missing containment predicates.
    Existing logger sinks (colored console and rotating file) belong to the host.
    """
    try:
        _require_capture_host()
        started = time.monotonic_ns()
        boot = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))
        enrollment, digest, identity = _read_enrollment(deployment_id)
        first = _snapshot(enrollment)
        configs = _bindings(enrollment, first)
        with ExitStack() as stack:
            facts = tuple(stack.enter_context(_custody(item.owner, "READ", boot)) for item in enrollment.originals)
            facts += tuple(stack.enter_context(_custody(item.lock, "WRITE", boot)) for item in enrollment.watchers)
            if _snapshot(enrollment) != first or _bindings(enrollment, first) != configs or _read_enrollment(deployment_id) != (enrollment, digest, identity):
                raise RecoveryScopeUnavailable("recovery scope or bindings changed during observation")
        report = RecoveryCustodyObservation(
            "recovery-scope-custody-observation-v1",
            "observed-component-only",
            enrollment,
            digest,
            identity,
            tuple(sorted(first)),
            facts,
            configs,
            boot,
            os.getpid(),
            _holder_start_token(os.getpid()),
            str(uuid.uuid4()),
            started,
            time.monotonic_ns(),
            _MISSING,
        )
        logger.debug("Observed recovery scope/custody component; full containment remains unavailable")
        return report
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, UnicodeError, subprocess.SubprocessError) as exc:
        logger.debug("Recovery scope/custody unavailable: {}", exc)
        raise RecoveryScopeUnavailable(str(exc)) from exc


def verify_recovery_custody(deployment_id: str, previous: object) -> RecoveryCustodyObservation:
    """A separate consumer repeats actual reads; a saved report grants nothing.

    The producer and external resource holders must still exist. Reports expire
    after 30 seconds within the same boot. This is current evidence, not history.
    """
    current = observe_recovery_custody(deployment_id)
    raw = _record(previous, tuple(RecoveryCustodyObservation.__dataclass_fields__))
    start, finish = raw["started_monotonic_ns"], raw["finished_monotonic_ns"]
    if type(start) is not int or type(finish) is not int or not 0 < start <= finish <= current.started_monotonic_ns or current.finished_monotonic_ns - start > _MAX_AGE_NS:
        raise RecoveryScopeUnavailable("saved component interval is stale or malformed")
    pid = _positive(raw["observer_pid"])
    if pid >= 2**31:
        raise RecoveryScopeUnavailable("saved producer PID exceeds the kernel bound")
    _text(raw["observer_start_token"], r"[1-9][0-9]{0,19}")
    _text(raw["observation_id"], r"[0-9a-f-]{36}")
    try:
        observation_id = str(uuid.UUID(raw["observation_id"]))
    except ValueError as exc:
        raise RecoveryScopeUnavailable("saved observation identity is malformed") from exc
    if observation_id != raw["observation_id"] or pid == current.observer_pid or raw["observation_id"] == current.observation_id:
        raise RecoveryScopeUnavailable("separate consumer observation is required")
    try:
        descriptor = os.pidfd_open(pid, 0)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if poller.poll(0) or _holder_start_token(pid) != raw["observer_start_token"]:
                raise RecoveryScopeUnavailable("saved component producer lifetime differs or exited")
        finally:
            os.close(descriptor)
    except (OSError, ValueError) as exc:
        raise RecoveryScopeUnavailable("saved component producer lifetime unavailable") from exc
    changing = {"observer_pid", "observer_start_token", "observation_id", "started_monotonic_ns", "finished_monotonic_ns"}
    actual = json.loads(json.dumps(asdict(current)))
    # Serialized evidence is strictly compared, including status and the fixed
    # missing predicates. No caller can promote the component into full closure.
    try:
        matches = _digest({key: value for key, value in raw.items() if key not in changing}) == _digest({key: value for key, value in actual.items() if key not in changing})
    except (ValueError, TypeError, RecursionError) as exc:
        raise RecoveryScopeUnavailable("saved component serialization is malformed") from exc
    if not matches:
        raise RecoveryScopeUnavailable("independently repeated recovery component differs")
    return current
