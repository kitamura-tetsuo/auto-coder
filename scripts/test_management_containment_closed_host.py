"""Exercise full current-containment CLI acceptance in an authorized disposable VM.

The host marker is provisioned separately after operator authorization. This
script never creates that marker and never accepts a containerized or populated
host. The fixture runs the installed bootstrap, Caddy and watcher, captures their
real kernel identities, then installs test-only root approval outside the
read-only producer. Saved reports never become launch grants.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import http.client
import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from loguru import logger
from scripts.test_management_containment_enrollment_host import APPROVAL_ROOT, FIXTURE_ROOT, INSTALLED_BOOTSTRAP, DisposableSystemdRoutes, _capture_manager, _docker, _original, _port, _private_directory, _systemctl, _write_private

from auto_coder.management import containment_route_sources, proxy_watch_cli
from auto_coder.management.archived_exclusions import unique_pairs
from auto_coder.management.containment_enrollment import ApprovedEnrollment, OriginalNetworkPin, OriginalOwnerFencePin, observe_approved_enrollment
from auto_coder.management.containment_kernel_subobservation import capture_original_caddy_container
from auto_coder.management.containment_observation import OBSERVED
from auto_coder.management.containment_observation import _docker as inspect_complete_docker
from auto_coder.management.containment_observation import read_scope
from auto_coder.management.containment_route_manifest import SystemdRouteFact, _systemd_catalog, _systemd_unit, propose_unapproved_route_draft
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import PriorContainer, _assert_pid_flock
from auto_coder.management.installed_bootstrap_source import _REVIEWED_ORDINARY_SOURCE_SHA256, _REVIEWED_QUARANTINED_SOURCE_SHA256
from auto_coder.management.systemd_watcher_observation import WATCHER_UNIT, observe_watcher_unit

if TYPE_CHECKING:
    from scripts.containment_quarantine_fixture import PreparedQuarantine

MARKER = Path("/etc/auto-coder-disposable-containment-host.json")
RESULT_ROOT = Path("/var/lib/auto-coder/containment-closed-host-results")
MARKER_SCHEMA = "auto-coder-disposable-containment-host-v1"
STOCK_UNITS = (
    "motd-news.timer",
    "motd-news.service",
    "cloud-init-hotplugd.socket",
    "cloud-init-hotplugd.service",
    "debug-shell.service",
    "grub-common.service",
    "cron.service",
)
STOCK_MASK_ROOT = Path("/etc/systemd/system")
CRON_ENTRY_ROOT = Path("/etc/cron.d")
CRON_TASK_GROUP = Path("/sys/fs/cgroup/system.slice/cron.service")
CUSTOM_UNIT_ROOT = Path("/run/systemd/system")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def read_authorization(path: Path, *, uid: int = 0) -> dict[str, object]:
    """Read an already installed authorization; the production path is fixed."""
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise PermissionError("disposable-host authorization path is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        content = stream.read(8193)
        after = os.fstat(stream.fileno())

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or stat.S_IMODE(before.st_mode) != 0o600 or len(content) > 8192 or identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise PermissionError("disposable-host authorization is unprotected or changed")
    raw = json.loads(content, object_pairs_hook=unique_pairs)
    fields = {"schema", "authorization_id", "machine_id", "boot_id", "engine_id", "synthetic_only"}
    if not isinstance(raw, dict) or set(raw) != fields or raw.get("schema") != MARKER_SCHEMA or raw.get("synthetic_only") is not True:
        raise PermissionError("disposable-host authorization schema differs")
    if any(not isinstance(raw[name], str) or not raw[name] or len(raw[name]) > 128 for name in fields - {"synthetic_only"}):
        raise PermissionError("disposable-host authorization identity is malformed")
    if not _IDENTIFIER.fullmatch(raw["authorization_id"]):
        raise PermissionError("disposable-host authorization ID is malformed")
    return raw


def require_disposable_host(authorization_id: str) -> dict[str, object]:
    """Complete all host selection checks before creating any fixture resource."""
    if os.geteuid() != 0 or not _IDENTIFIER.fullmatch(authorization_id):
        raise PermissionError("an explicit root-authorized disposable VM is required")
    authorization = read_authorization(MARKER)
    if authorization["authorization_id"] != authorization_id:
        raise PermissionError("disposable-host authorization does not match this invocation")
    identities = {"machine_id": Path("/etc/machine-id").read_text().strip(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    if any(authorization[name] != value for name, value in identities.items()):
        raise PermissionError("disposable-host machine or boot identity differs")
    if Path("/proc/1/comm").read_text().strip() != "systemd" or any(os.readlink("/proc/self/ns/" + name) != os.readlink("/proc/1/ns/" + name) for name in ("pid", "mnt", "net", "user")):
        raise PermissionError("initial systemd host namespaces are required")
    virtual = subprocess.run(("systemd-detect-virt", "--vm"), capture_output=True, text=True, check=False, timeout=10)
    container = subprocess.run(("systemd-detect-virt", "--container"), capture_output=True, text=True, check=False, timeout=10)
    if virtual.returncode != 0 or virtual.stdout.strip() not in ("kvm", "qemu") or container.returncode == 0:
        raise PermissionError("only a separately provisioned disposable KVM/QEMU VM is supported")
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT") or _docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}") != "unix:///var/run/docker.sock":
        raise PermissionError("the VM's own local Docker engine is required")
    if _docker("info", "--format", "{{.ID}}") != authorization["engine_id"]:
        raise PermissionError("disposable-host Docker engine identity differs")
    if _docker("ps", "-aq", "--no-trunc"):
        raise PermissionError("the disposable VM already contains Docker objects")
    if WATCHER_UNIT in _systemd_catalog():
        raise PermissionError("the fixed production watcher unit already exists")
    watcher_source = Path(proxy_watch_cli.__file__).resolve(strict=True)
    _original(watcher_source, "watcher-source")
    return authorization


def _start_token(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]


def _assert_image(image: str) -> None:
    if not _IMAGE.fullmatch(image) or _docker("image", "inspect", image, "--format", "{{.Id}}") != image:
        raise ValueError("an exact preinstalled image ID is required; this fixture never pulls images")


def assert_report(report: object, returncode: int, *, accepted: bool, authority: str | None = None) -> dict[str, object]:
    """Judge the real CLI result, including complete positive component predicates."""
    if not isinstance(report, dict) or returncode != (0 if accepted else 2) or report.get("status") != (OBSERVED if accepted else "unavailable"):
        raise AssertionError("containment CLI exit code or status differs: " + repr(report))
    reasons = report.get("unavailable")
    if not isinstance(reasons, list) or any(not isinstance(item, dict) or not isinstance(item.get("authority"), str) for item in reasons):
        raise AssertionError("containment CLI did not return structured predicate evidence")
    if accepted:
        if reasons or any(not isinstance(report.get(key), dict) for key in ("approved_enrollment", "watcher_systemd", "installed_bootstrap_source", "installed_mount_profile", "route_manifest", "kernel_subobservation")):
            raise AssertionError("positive containment result is missing a required component")
        kernel = report["kernel_subobservation"]
        if not all(kernel.get(name) is True for name in ("original_cgroups_empty", "unpinned_network_task_scan_absent", "host_listener_absent", "external_namespace_pin_verified", "external_owner_fence_verified")) or kernel.get("retained_original_network_pin") is not False:
            raise AssertionError("positive containment result is missing independently retained kernel facts")
        if report["route_manifest"].get("closed_profile") is not True or report["installed_bootstrap_source"].get("reviewed_lock_before_launch") is not True:
            raise AssertionError("positive containment result does not close the reviewed source/routes")
    elif not reasons or authority is not None and authority not in {item["authority"] for item in reasons}:
        raise AssertionError("the independently required refusal was absent: " + repr({"expected": authority, "unavailable": reasons}))
    return report


def _assert_independent(first: dict[str, object], second: dict[str, object]) -> None:
    if (first["observer_pid"], first["observer_start_token"]) == (second["observer_pid"], second["observer_start_token"]):
        raise AssertionError("producer and consumer did not independently inspect the fixture")
    finish, start = first["finished_monotonic_ns"], second["started_monotonic_ns"]
    if type(finish) is not int or type(start) is not int or first["observation_id"] == second["observation_id"] or finish > start:
        raise AssertionError("independent observation intervals or identities overlap")


def _footprint(paths: tuple[Path, ...], worker: subprocess.Popen[str]) -> tuple[object, ...]:
    """Check protected fixture bytes and the unrelated real worker lifetime."""
    if worker.poll() is not None:
        raise AssertionError("the preserved worker exited during containment observation")
    values: list[object] = [(worker.pid, _start_token(worker.pid))]
    for root in paths:
        try:
            root.lstat()
        except FileNotFoundError:
            # Negative fixtures may deliberately remove one approved mask.
            # Its absence is still bound across the read-only CLI invocation.
            values.append((str(root), "missing"))
            continue
        for path in (root, *(sorted(root.rglob("*")) if root.is_dir() else ())):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            elif stat.S_ISDIR(info.st_mode) or stat.S_ISSOCK(info.st_mode):
                content = ""
            else:
                raise AssertionError("unsupported protected fixture object")
            values.append((str(path), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns, content))
    return tuple(values)


def _receipt_fingerprint(values: tuple[object, ...]) -> dict[str, object]:
    """Serialize measured identities and hashes without protected source bytes."""
    worker = values[0]
    if not isinstance(worker, tuple) or len(worker) != 2:
        raise AssertionError("preservation worker identity is malformed")
    entries = []
    for row in values[1:]:
        if not isinstance(row, tuple):
            raise AssertionError("preservation file identity is malformed")
        if len(row) == 2 and row[1] == "missing":
            entries.append({"path": row[0], "kind": "missing"})
            continue
        if len(row) != 9 or not isinstance(row[3], int) or not isinstance(row[8], str):
            raise AssertionError("preservation file fingerprint is malformed")
        mode, content = row[3], row[8]
        kind = "symlink" if stat.S_ISLNK(mode) else "file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode) else "socket"
        entries.append({"path": row[0], "kind": kind, "device": row[1], "inode": row[2], "mode": mode, "uid": row[4], "size": row[5], "mtime_ns": row[6], "ctime_ns": row[7], "sha256": hashlib.sha256(content.encode()).hexdigest() if kind == "symlink" else content or None})
    return {"worker": {"pid": worker[0], "start_token": worker[1]}, "entries": entries}


@dataclass
class PreservationReceipt:
    """Persist actual invocation samples even when execution or assertions fail."""

    case: Case
    label: str
    mode: str
    intentional_config: Path | None = None
    samples: dict[str, dict[str, object]] = field(default_factory=dict)
    raw: dict[str, tuple[object, ...]] = field(default_factory=dict)
    comparisons: list[dict[str, object]] = field(default_factory=list)
    mutation_applied: bool = False
    spawn_returned_monotonic_ns: int | None = None
    process_start_token: str | None = None

    def _publish(self, phase: str, value: dict[str, object]) -> None:
        directory = self.case.root / "evidence"
        if not _IDENTIFIER.fullmatch(self.label) or self.mode not in ("observe", "verify") or directory != directory.resolve(strict=True):
            raise AssertionError("preservation receipt destination or invocation is unsupported")
        if any(root.resolve(strict=False) == directory or root.resolve(strict=False) in directory.parents for root in self.case.protected):
            raise AssertionError("preservation receipts must remain outside protected paths")
        path = directory / (self.label + ".preservation-" + phase + ".json")
        _write_private(path, _json(value))
        self.case.artifacts.append(path)

    def capture(self, phase: str, *, publish: bool = True) -> None:
        started = time.monotonic_ns()
        error: BaseException | None = None
        fingerprint = None
        try:
            measured = _footprint(self.case.protected, self.case.worker)
            fingerprint = _receipt_fingerprint(measured)
            self.raw[phase] = measured
        except BaseException as exc:
            error = exc
        sample: dict[str, object] = {
            "schema": "disposable-containment-preservation-receipt-v1",
            "profile": self.case.profile,
            "label": self.label,
            "cli_mode": self.mode,
            "phase": phase,
            "started_monotonic_ns": started,
            "finished_monotonic_ns": time.monotonic_ns(),
            "recorded_unix_ns": time.time_ns(),
            "protected_roots": [str(path) for path in self.case.protected],
            "fingerprint": fingerprint,
            "capture_error": type(error).__name__ if error is not None else None,
        }
        self.samples[phase] = sample
        if publish:
            self._publish(phase, sample)
        if error is not None:
            raise error

    def spawned(self, process: subprocess.Popen[str]) -> None:
        self.spawn_returned_monotonic_ns = time.monotonic_ns()
        try:
            self.process_start_token = _start_token(process.pid)
        except (OSError, ValueError, IndexError):
            # A very short-lived command may exit before this optional read.
            # Its actual return code remains independently available from Popen.
            self.process_start_token = None

    def compare(self, first: str, second: str, *, excluded: tuple[Path, ...] = ()) -> bool:
        omitted = {str(path) for path in excluded}

        def selected(phase: str) -> tuple[object, ...] | None:
            rows = self.raw.get(phase)
            if rows is None:
                return None
            return tuple(row for index, row in enumerate(rows) if index == 0 or not isinstance(row, tuple) or row[0] not in omitted)

        before, after = selected(first), selected(second)
        same = None if before is None or after is None else before == after
        self.comparisons.append({"from": first, "to": second, "excluded_paths": sorted(omitted), "equal": same})
        return same is True

    def _mutation_entries(self, phase: str) -> list[object] | None:
        sample = self.samples.get(phase, {})
        fingerprint = sample.get("fingerprint")
        if not isinstance(fingerprint, dict) or not isinstance(fingerprint.get("entries"), list):
            return None
        return [entry for entry in fingerprint["entries"] if entry.get("path") == str(self.intentional_config)]

    def finish(self, process: subprocess.Popen[str] | None, error: BaseException | None) -> None:
        cleanup_error: BaseException | None = None
        cleanup_signal = None
        if process is not None and process.poll() is None:
            try:
                process.kill()
                cleanup_signal = int(signal.SIGKILL)
                process.communicate(timeout=15)
            except BaseException as exc:
                cleanup_error = exc
        capture_error: BaseException | None = None
        try:
            self.capture("after", publish=False)
        except BaseException as exc:
            capture_error = exc
        excluded = (self.intentional_config,) if self.mutation_applied and self.intentional_config is not None else ()
        self.compare("before", "after", excluded=excluded)
        if self.mutation_applied:
            self.compare("post-fixture-mutation", "after")
        outcome = dict(self.samples["after"])
        outcome.update(
            process={
                "pid": process.pid if process is not None else None,
                "start_token": self.process_start_token,
                "spawn_returned_monotonic_ns": self.spawn_returned_monotonic_ns,
                "returncode": process.returncode if process is not None else None,
                "termination_signal": -process.returncode if process is not None and process.returncode is not None and process.returncode < 0 else None,
                "cleanup_signal_requested": cleanup_signal,
            },
            invocation_error=type(error).__name__ if error is not None else None,
            cleanup_error=type(cleanup_error).__name__ if cleanup_error is not None else None,
            comparisons=self.comparisons,
            unqualified_before_after_equal=self.raw["before"] == self.raw["after"] if "before" in self.raw and "after" in self.raw else None,
            intentional_mutation=(
                {
                    "path": str(self.intentional_config),
                    "write_completed": self.mutation_applied,
                    "preservation_claimed": False,
                    "reason": "This configuration is intentionally changed by the race fixture and is not claimed preserved. A completed write excludes only this path from the full-invocation equality check.",
                    "before_phase": "paused",
                    "after_phase": "post-fixture-mutation",
                    "before": self._mutation_entries("paused"),
                    "after": self._mutation_entries("post-fixture-mutation"),
                }
                if self.intentional_config is not None
                else None
            ),
            snapshot_artifacts=[str(self.case.root / "evidence" / (self.label + ".preservation-" + phase + ".json")) for phase in self.samples if phase != "after"],
        )
        preserved = all(item["equal"] is True for item in self.comparisons)
        outcome["status"] = "passed" if error is None and cleanup_error is None and capture_error is None and preserved else "failed"
        self._publish("after", outcome)
        if error is None:
            if cleanup_error is not None:
                raise cleanup_error
            if capture_error is not None:
                raise capture_error
            if not preserved:
                raise AssertionError("read-only CLI changed preserved state, OAuth, configuration or worker lifetime")


def _stock_mask_identity(path: Path) -> tuple[int, ...]:
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or os.readlink(path) != "/dev/null":
        raise AssertionError("stock fixture mask is not a protected /dev/null link: " + str(path))
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass
class InhibitedStockRoutes:
    """Inhibit only the fixed installed stock routes in the authorized VM."""

    identities: dict[Path, tuple[int, ...]] = field(default_factory=dict)
    created: set[Path] = field(default_factory=set)
    initial_facts: tuple[SystemdRouteFact, ...] = ()

    @classmethod
    def install(cls) -> InhibitedStockRoutes:
        if not set(STOCK_UNITS) <= set(_systemd_catalog()):
            raise AssertionError("required stock controller is absent from the installed catalog")
        initial = tuple(_systemd_unit(name) for name in STOCK_UNITS)
        if any(fact.unit_file_state not in {"enabled", "disabled", "static", "masked"} for fact in initial):
            raise AssertionError("required stock controller has no supported installed unit state")
        state = cls(initial_facts=initial)
        for name in STOCK_UNITS:
            path = STOCK_MASK_ROOT / name
            if path.exists() or path.is_symlink():
                state.identities[path] = _stock_mask_identity(path)
        try:
            # Reloading an active timer's mask can leave it failed instead of
            # inactive. Stop the original units before changing their files.
            _systemctl("stop", *STOCK_UNITS)
            for name in STOCK_UNITS:
                path = STOCK_MASK_ROOT / name
                if path not in state.identities:
                    # Exclusive link creation leaves existing fragments and
                    # preexisting masks untouched, including on setup failure.
                    path.symlink_to("/dev/null")
                    state.identities[path] = _stock_mask_identity(path)
                    state.created.add(path)
            _systemctl("daemon-reload")
            state.assert_inhibited()
            logger.info("Inhibited the installed stock fixture routes: {}", ", ".join(STOCK_UNITS))
            return state
        except BaseException:
            state.cleanup()
            raise

    @property
    def mask_paths(self) -> tuple[Path, ...]:
        return tuple(STOCK_MASK_ROOT / name for name in STOCK_UNITS)

    def assert_inhibited(self) -> tuple[SystemdRouteFact, ...]:
        facts = []
        for path in self.mask_paths:
            if _stock_mask_identity(path) != self.identities.get(path):
                raise AssertionError("stock fixture mask identity changed: " + str(path))
            fact = _systemd_unit(path.name)
            if not fact.inhibited:
                details = {name: getattr(fact, name) for name in ("unit", "unit_file_state", "active_state", "main_pid", "inhibited", "needs_daemon_reload")}
                raise AssertionError("stock fixture route is not masked and inactive: " + _json(details).decode())
            facts.append(fact)
        return tuple(facts)

    def evidence(self) -> dict[str, object]:
        facts = self.assert_inhibited()
        return {"initial_units": [asdict(fact) for fact in self.initial_facts], "units": [asdict(fact) for fact in facts], "masks": [{"path": str(path), "identity": list(self.identities[path]), "created_by_fixture": path in self.created} for path in self.mask_paths]}

    def cleanup(self) -> None:
        failures = []
        removed = False
        for path, identity in tuple(self.identities.items()):
            try:
                if path not in self.created:
                    if _stock_mask_identity(path) != identity:
                        raise AssertionError("preexisting stock mask identity changed: " + str(path))
                    continue
                if _stock_mask_identity(path) != identity:
                    raise AssertionError("fixture-created stock mask was externally replaced: " + str(path))
                path.unlink()
                self.created.remove(path)
                del self.identities[path]
                removed = True
            except (OSError, AssertionError) as exc:
                failures.append(str(exc))
        if removed:
            try:
                _systemctl("daemon-reload")
            except AssertionError as exc:
                failures.append(str(exc))
        if failures:
            raise AssertionError("stock fixture mask cleanup failed: " + repr(failures))


@dataclass
class WatcherFixture:
    """Run the real installed watcher before inhibiting its real unit."""

    source: Path
    inode: int
    digest: str
    config: Path
    state: Path
    stdout: Path
    unit: str = WATCHER_UNIT

    @classmethod
    def start(cls, root: Path, manager: str, manager_image: str, proxy: str, proxy_image: str, caddyfile: Path, *, unit: str = WATCHER_UNIT) -> WatcherFixture:
        if re.fullmatch(r"auto-coder-management-proxy-watch(?:-[A-Za-z0-9_-]+)?\.service", unit) is None:
            raise ValueError("fixture watcher unit must use a bounded management name")
        state = root / "watcher-state"
        state.mkdir(mode=0o700)
        config, output = root / "watcher-config.json", root / "watcher-output.log"
        _write_private(config, _json({"state_root": str(state), "manager_id": manager, "manager_image": manager_image, "proxy_id": proxy, "proxy_image": proxy_image, "config_sha256": hashlib.sha256(caddyfile.read_bytes()).hexdigest()}))
        _write_private(output, b"")
        source = Path("/run/systemd/system") / unit
        command = f"{sys.executable} -I -B -m auto_coder.management.proxy_watch_cli --config {config}"
        if any(character in command for character in ("\n", "\r", "%", '"')) or " " in sys.executable or " " in str(root):
            raise ValueError("fixture watcher command contains unsupported systemd syntax")
        content = f"[Unit]\nDescription=Disposable original management proxy watcher\n[Service]\nType=simple\nExecStart={command}\nRestart=no\nStandardOutput=append:{output}\nStandardError=append:{output}\n".encode()
        _write_private(source, content)
        result = cls(source, source.lstat().st_ino, hashlib.sha256(content).hexdigest(), config, state, output, unit)
        try:
            _systemctl("daemon-reload")
            _systemctl("start", unit)
            deadline = time.monotonic() + 30
            while "ATTACHED\n" not in output.read_text():
                if time.monotonic() >= deadline:
                    raise AssertionError("the real installed watcher never observed Caddy attached")
                time.sleep(0.1)
            pid = subprocess.run(("systemctl", "show", "--property=MainPID", "--value", unit), check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            if not pid.isdigit() or int(pid) <= 1:
                raise AssertionError("the real watcher has no live unit-owned process")
            _systemctl("stop", unit)
            _systemctl("mask", unit)
            if unit == WATCHER_UNIT:
                observe_watcher_unit()
            elif not _systemd_unit(unit).inhibited:
                raise AssertionError("the real additional watcher was not inhibited")
            return result
        except BaseException:
            result.cleanup()
            raise

    def cleanup(self) -> None:
        _systemctl("stop", self.unit)
        mask = Path("/etc/systemd/system") / self.unit
        if mask.is_symlink() and os.readlink(mask) == "/dev/null":
            _systemctl("unmask", self.unit)
        elif mask.exists() or mask.is_symlink():
            raise AssertionError("fixture watcher mask was externally replaced")
        if self.source.lstat().st_ino != self.inode or hashlib.sha256(self.source.read_bytes()).hexdigest() != self.digest:
            raise AssertionError("fixture watcher source was externally replaced")
        self.source.unlink()
        _systemctl("daemon-reload")


@dataclass
class Case:
    root: Path
    fixture_id: str
    profile: str
    manager: str
    manager_image: str
    config: Path
    management_config: Path
    state: Path
    oauth: Path
    prior_manager: PriorContainer
    namespace_fd: int
    pin: OriginalNetworkPin
    worker: subprocess.Popen[str]
    worker_config: Path
    proxy: str = ""
    owner_fd: int = -1
    watcher: WatcherFixture | None = None
    routes: DisposableSystemdRoutes | None = None
    stock_routes: InhibitedStockRoutes | None = None
    cron_dispatch: CronDispatchFixture | None = None
    custom_cron: MaskedCronController | None = None
    artifacts: list[Path] = field(default_factory=list)
    protected: tuple[Path, ...] = ()
    scope: Path | None = None
    previous: Path | None = None
    prior_evidence: Path | None = None
    support_source: Path | None = None
    owns_manager: bool = True
    extra_protected: tuple[Path, ...] = ()

    def command(self, label: str, *, accepted: bool, authority: str | None = None, verify: bool = True) -> dict[str, object]:
        if self.scope is None or verify and self.previous is None:
            raise AssertionError("fixture approval or prior observation is missing")
        arguments = [sys.executable, "-I", "-B", "-m", "auto_coder.management.containment_observation", "verify" if verify else "observe", "--scope", str(self.scope)]
        if verify:
            arguments.extend(("--previous", str(self.previous)))
        receipt = PreservationReceipt(self, label, "verify" if verify else "observe")
        process: subprocess.Popen[str] | None = None
        error: BaseException | None = None
        try:
            receipt.capture("before")
            logger.info("Running {} {} through the actual production CLI", self.profile, label)
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            receipt.spawned(process)
            stdout, _ = process.communicate(timeout=240)
            output = self.root / "evidence" / (label + ".json")
            _write_private(output, stdout.encode())
            self.artifacts.append(output)
            report = assert_report(json.loads(stdout), process.returncode, accepted=accepted, authority=authority)
            expected = _REVIEWED_ORDINARY_SOURCE_SHA256 if self.profile == "ordinary-v1" else _REVIEWED_QUARANTINED_SOURCE_SHA256
            if accepted:
                source, profile = report["installed_bootstrap_source"], report["installed_mount_profile"]
                if not isinstance(source, dict) or not isinstance(profile, dict) or source.get("source_sha256") != expected or profile.get("profile") != self.profile:
                    raise AssertionError("positive report substituted the original reviewed source profile")
            return report
        except BaseException as exc:
            error = exc
            raise
        finally:
            receipt.finish(process, error)

    def positive_pair(self) -> tuple[dict[str, object], dict[str, object]]:
        first = self.command("observe", accepted=True, verify=False)
        self.previous = self.root / "evidence" / "observe.json"
        second = self.command("verify", accepted=True)
        _assert_independent(first, second)
        return first, second

    def refusal_pair(self, label: str, *, authority: str) -> tuple[dict[str, object], dict[str, object]]:
        """Require both real entrypoints to reobserve the same extant fault."""
        first = self.command(label + "-observe", accepted=False, authority=authority, verify=False)
        if self.previous is None:
            self.previous = self.root / "evidence" / (label + "-observe.json")
        second = self.command(label + "-verify", accepted=False, authority=authority)
        _assert_independent(first, second)
        return first, second

    def baseline(self, label: str) -> None:
        """Require full current success immediately before one fault is added."""
        self.command(label + "-baseline", accepted=True, verify=False)
        self.previous = self.root / "evidence" / (label + "-baseline.json")

    def cleanup(self) -> None:
        if self.routes is not None:
            self.routes.cleanup()
            self.routes = None
        if self.watcher is not None:
            self.watcher.cleanup()
            self.watcher = None
        for name in (self.fixture_id + ".routes.json", self.fixture_id + ".json"):
            path = APPROVAL_ROOT / name
            if path.exists():
                path.unlink()
        if self.proxy:
            _docker("rm", "-f", self.proxy)
            self.proxy = ""
        if self.manager and self.owns_manager:
            _docker("rm", "-f", self.manager)
            self.manager = ""
        if self.namespace_fd >= 0:
            os.close(self.namespace_fd)
            self.namespace_fd = -1


@dataclass
class BoundFixtureTask:
    """Retain a real PID lifetime before signalling fixture-owned work."""

    pid: int
    start_token: str
    parent: int
    command: tuple[str, ...]
    cgroup: str
    descriptor: int

    @classmethod
    def capture(cls, pid: int) -> BoundFixtureTask:
        start = _start_token(pid)
        descriptor = os.pidfd_open(pid, 0)
        try:
            base = Path(f"/proc/{pid}")
            fields = (base / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            command = tuple(part.decode("utf-8", errors="strict") for part in (base / "cmdline").read_bytes().split(b"\0") if part)
            group = (base / "cgroup").read_text(encoding="ascii")
            if fields[19] != start or _start_token(pid) != start:
                raise AssertionError("fixture task lifetime changed during capture")
            return cls(pid, start, int(fields[1]), command, group, descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def assert_current(self) -> None:
        if _start_token(self.pid) != self.start_token or Path(f"/proc/{self.pid}/cgroup").read_text(encoding="ascii") != self.cgroup:
            raise AssertionError("fixture task lifetime or cgroup changed")

    def send(self, number: int) -> None:
        self.assert_current()
        signal.pidfd_send_signal(self.descriptor, number)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass
class CronDispatchFixture:
    """Install one immutable benign job before any complete approval."""

    source: Path
    identity: tuple[int, ...]
    digest: str

    @classmethod
    def install(cls, name: str) -> CronDispatchFixture:
        if not re.fullmatch(r"ac-test-pr2829-[0-9a-f]{32}", name):
            raise ValueError("cron fixture name is not independently scoped")
        source = CRON_ENTRY_ROOT / name
        content = b"* * * * * root /usr/bin/sleep 120\n"
        _write_private(source, content)
        return cls(source, cls._identity(source), hashlib.sha256(content).hexdigest())

    @staticmethod
    def _identity(path: Path) -> tuple[int, ...]:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise AssertionError("cron fixture source is not a private owned regular file")
        return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_size

    def assert_unchanged(self) -> None:
        if self._identity(self.source) != self.identity or hashlib.sha256(self.source.read_bytes()).hexdigest() != self.digest:
            raise AssertionError("cron fixture source was externally replaced")

    def cleanup(self) -> None:
        self.assert_unchanged()
        self.source.unlink()


@dataclass
class MaskedCronController:
    """Keep a real differently named controller masked before every baseline."""

    source: Path
    source_identity: tuple[int, ...]
    mask: Path
    mask_identity: tuple[int, ...] | None = None

    @classmethod
    def install(cls, name: str) -> MaskedCronController:
        if not re.fullmatch(r"ac-test-pr2829-[0-9a-f]{32}", name):
            raise ValueError("custom cron controller name is not independently scoped")
        source = CUSTOM_UNIT_ROOT / (name + "-custom-cron.service")
        content = b"[Unit]\nDescription=Disposable differently named cron controller\n[Service]\nType=simple\nExecStart=/usr/sbin/cron -f -P\nKillMode=process\n"
        _write_private(source, content)
        controller = cls(source, CronDispatchFixture._identity(source), STOCK_MASK_ROOT / source.name)
        try:
            controller.mask.symlink_to("/dev/null")
            controller.mask_identity = _stock_mask_identity(controller.mask)
            _systemctl("daemon-reload")
            controller.assert_inhibited()
            return controller
        except BaseException:
            controller.cleanup()
            raise

    def assert_inhibited(self) -> None:
        if CronDispatchFixture._identity(self.source) != self.source_identity or _stock_mask_identity(self.mask) != self.mask_identity or not _systemd_unit(self.source.name).inhibited:
            raise AssertionError("the owned custom cron controller is not unchanged and inhibited")

    def cleanup(self) -> None:
        if self.mask_identity is not None:
            if _stock_mask_identity(self.mask) != self.mask_identity:
                raise AssertionError("custom cron mask was externally replaced")
            self.mask.unlink()
            self.mask_identity = None
        if CronDispatchFixture._identity(self.source) != self.source_identity:
            raise AssertionError("custom cron source was externally replaced")
        self.source.unlink()
        _systemctl("daemon-reload")


def _cron_group_pids(group: Path) -> tuple[int, ...]:
    if any(path.is_dir() for path in group.iterdir()):
        raise AssertionError("fixture cron unexpectedly created a nested cgroup")
    values = (group / "cgroup.procs").read_text(encoding="ascii").split()
    if len(values) > 128 or any(not value.isdecimal() or int(value) <= 1 for value in values):
        raise AssertionError("fixture cron cgroup has unsupported process readback")
    return tuple(sorted({int(value) for value in values}))


def _owned_cron_descendant(pid: int, daemon: int, retained: dict[int, BoundFixtureTask]) -> bool:
    """Bind ancestry while the real daemon or a retained parent still exists."""
    seen: set[int] = set()
    for _ in range(64):
        if pid in seen or pid <= 1:
            return False
        seen.add(pid)
        if pid == daemon:
            return True
        if pid in retained:
            retained[pid].assert_current()
            return True
        pid = int(Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[1])
    return False


def _require_refusal_reason(reports: tuple[dict[str, object], dict[str, object]], reason: str) -> None:
    for report in reports:
        unavailable = report.get("unavailable")
        if not isinstance(unavailable, list) or not any(isinstance(item, dict) and item.get("authority") == "approved-route-manifest" and reason in str(item.get("reason", "")) for item in unavailable):
            raise AssertionError("actual CLI did not refuse the intended cron predicate: " + repr(report))


def _cron_task_negative(case: Case, *, controller: str = "cron.service", label: str = "genuine-cron-retained-task") -> None:
    """Dispatch actual cron work while preserving the already approved mask."""
    if controller != "cron.service" and (case.custom_cron is None or controller != case.custom_cron.source.name):
        raise ValueError("fixture task fault cannot target an unowned cron controller")
    assert case.cron_dispatch is not None and case.stock_routes is not None
    case.cron_dispatch.assert_unchanged()
    mask = STOCK_MASK_ROOT / controller
    group = CRON_TASK_GROUP.parent / controller
    mask_identity = _stock_mask_identity(mask)
    created = not group.exists()
    if created:
        group.mkdir()
    if group.is_symlink() or group.resolve(strict=True) != group:
        raise AssertionError("fixture cron cgroup is not canonical")
    group_info = group.stat()
    group_identity = group_info.st_dev, group_info.st_ino
    if _cron_group_pids(group) or (group / "cgroup.threads").read_text().strip():
        raise AssertionError("fixture never adopts a populated cron cgroup")
    retained: dict[int, BoundFixtureTask] = {}
    daemon: subprocess.Popen[str] | None = None
    output = case.root / "evidence" / (label + "-daemon.log")
    _write_private(output, b"")
    case.artifacts.append(output)
    try:
        # The child enters the empty owned scope before exec of the real daemon.
        program = "import os,sys; f=open(sys.argv[1], 'w'); f.write(str(os.getpid())); f.close(); os.execv('/usr/sbin/cron', ('/usr/sbin/cron', '-f', '-P'))"
        with output.open("a", encoding="utf-8") as stream:
            daemon = subprocess.Popen((sys.executable, "-I", "-B", "-c", program, str(group / "cgroup.procs")), stdout=stream, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 90
            job: BoundFixtureTask | None = None
            while job is None:
                if daemon.poll() is not None:
                    raise AssertionError("genuine cron exited before dispatch: " + output.read_text())
                for pid in _cron_group_pids(group):
                    try:
                        if pid not in retained:
                            if not _owned_cron_descendant(pid, daemon.pid, retained):
                                raise AssertionError("cron cgroup contains a task outside the owned daemon ancestry")
                            retained[pid] = BoundFixtureTask.capture(pid)
                        task = retained[pid]
                        task.assert_current()
                        task.command = tuple(part.decode("utf-8", errors="strict") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part)
                        if task.command == ("/usr/bin/sleep", "120") and Path(f"/proc/{pid}/exe").resolve(strict=True) == Path("/usr/bin/sleep"):
                            if task.cgroup != "0::/system.slice/" + controller + "\n":
                                raise AssertionError("genuine cron job escaped the expected measured scope")
                            task.send(signal.SIGSTOP)
                            job = task
                    except FileNotFoundError:
                        continue
                if job is None and time.monotonic() >= deadline:
                    raise AssertionError("genuine cron never dispatched the immutable sleep entry: " + output.read_text())
                if job is None:
                    time.sleep(0.2)
            daemon.terminate()
            daemon.wait(timeout=15)
            job.assert_current()
            if _stock_mask_identity(mask) != mask_identity or not _systemd_unit(controller).inhibited:
                raise AssertionError("cron task fault altered the approved controller mask")
            facts = {
                "source": str(case.cron_dispatch.source),
                "source_sha256": case.cron_dispatch.digest,
                "mask_identity": list(mask_identity),
                "cgroup": str(group),
                "daemon_pid": daemon.pid,
                "daemon_returncode": daemon.returncode,
                "retained_tasks": [asdict(task) for task in retained.values()],
                "actual_job_stat": Path(f"/proc/{job.pid}/stat").read_text(),
                "job_stopped_after_real_dispatch": True,
            }
            evidence = case.root / "evidence" / (label + ".json")
            _write_private(evidence, _json(facts))
            case.artifacts.append(evidence)
            reports = case.refusal_pair(label, authority="approved-route-manifest")
            _require_refusal_reason(reports, "inhibited stock cron scope retains tasks: /system.slice/" + controller)
            job.assert_current()
            case.cron_dispatch.assert_unchanged()
            if _stock_mask_identity(mask) != mask_identity:
                raise AssertionError("cron mask identity changed during actual task refusal")
    finally:
        if daemon is not None and daemon.poll() is None:
            daemon.terminate()
            daemon.wait(timeout=15)
        try:
            for task in reversed(tuple(retained.values())):
                try:
                    task.send(signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
            deadline = time.monotonic() + 10
            while _cron_group_pids(group):
                if time.monotonic() >= deadline:
                    raise AssertionError("owned cron task cleanup did not empty its scope")
                time.sleep(0.1)
            current = group.stat()
            if (current.st_dev, current.st_ino) != group_identity:
                raise AssertionError("owned cron task cgroup was externally replaced")
            if created:
                group.rmdir()
        finally:
            for task in retained.values():
                task.close()


def _namespace_pin(prior: PriorContainer) -> tuple[int, OriginalNetworkPin]:
    descriptor = os.open(f"/proc/{prior.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if os.readlink(f"/proc/self/fd/{descriptor}") != prior.net_namespace:
            raise AssertionError("the real original namespace changed before pinning")
        return descriptor, OriginalNetworkPin(os.getpid(), _start_token(os.getpid()), Path("/proc/sys/kernel/random/boot_id").read_text().strip(), descriptor, prior.net_namespace, info.st_dev, info.st_ino)
    except BaseException:
        os.close(descriptor)
        raise


def _quarantine_case(prepared: PreparedQuarantine, worker: subprocess.Popen[str], worker_config: Path, *, artifact_groups: list[list[Path]] | None = None) -> Case:
    if artifact_groups is not None:
        # Keep the live list before capture, which can fail before Case exists.
        artifact_groups.append(prepared.artifacts)
    prior = prepared.start_and_capture()
    descriptor, pin = _namespace_pin(prior)
    try:
        (prepared.root / "evidence").mkdir(mode=0o700)
        case = Case(
            prepared.root,
            "ac-test-pr2829-" + uuid.uuid4().hex,
            "quarantined-original-v1",
            prepared.manager_id,
            prepared.image_id,
            prepared.bootstrap_config,
            prepared.management_config,
            prepared.state_root,
            prepared.oauth_root,
            prior,
            descriptor,
            pin,
            worker,
            worker_config,
            support_source=prepared.support_source_descriptor,
            owns_manager=False,
            extra_protected=(prepared.journal_root,),
        )
        if prepared.variant != "supported":
            case.artifacts.append(prepared.root / "variant-live-capture.json")
        return case
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_ordinary(root: Path, image: str, worker: subprocess.Popen[str], worker_config: Path) -> Case:
    fixture_id = "ac-test-pr2829-" + uuid.uuid4().hex
    root.mkdir(mode=0o700)
    for name in ("state", "bootstrap", "oauth", "evidence"):
        (root / name).mkdir(mode=0o700)
    config = root / "bootstrap" / "config.json"
    _write_private(
        config,
        _json(
            {
                "instance_id": fixture_id,
                "state_root": "/management-child-state",
                "oauth_root": "/management-oauth",
                "journal_root": "/management-child-state/journal",
                "listener_url": "http://127.0.0.1:8081",
                "initial_runtime": "bootstrap",
                "crash_budget": 0,
                "runtimes": [{"reference": "bootstrap", "receipt": "sha256:b8cb1f05b3261967b1432b6d312f0cc6d787924f9dd84eba2227ac40d597b6d9", "executable": "/opt/auto-coder/.venv/bin/auto-coder", "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"]}],
            }
        ),
    )
    management = root / "bootstrap" / "management.yaml"
    _write_private(management, b"fixture: true\n")
    # The genuine supervisor publishes its owner/ledger and remains at the
    # supported exhausted crash budget. No child launch or fresh success is forged.
    database = root / "oauth" / "fixture.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preserved_oauth_fixture (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved_oauth_fixture VALUES ('synthetic-fixture-only')")
    database.chmod(0o600)
    manager = _docker(
        "run",
        "-d",
        "--name",
        fixture_id + "-manager",
        "--publish",
        f"127.0.0.1:{_port()}:8080",
        "--mount",
        f"type=bind,src={root / 'state'},dst=/management-child-state",
        "--mount",
        f"type=bind,src={root / 'bootstrap'},dst=/bootstrap,readonly",
        "--mount",
        f"type=bind,src={root / 'oauth'},dst=/management-oauth",
        "--entrypoint",
        "python",
        image,
        "-m",
        "auto_coder.management.bootstrap_supervisor",
        "--config",
        "/bootstrap/config.json",
    )
    try:
        prior = _capture_manager(manager, image, root / "state", root / "bootstrap", root / "oauth")
        descriptor, pin = _namespace_pin(prior)
        return Case(root, fixture_id, "ordinary-v1", manager, image, config, management, root / "state", root / "oauth", prior, descriptor, pin, worker, worker_config)
    except BaseException:
        _docker("rm", "-f", manager)
        raise


def _classify_fixture_restarter_routes(case: Case, draft: dict[str, object]) -> None:
    if case.routes is None or case.stock_routes is None:
        raise AssertionError("the fixture restarter prerequisites are missing")
    case.stock_routes.assert_inhibited()
    expected = {*case.routes.names, WATCHER_UNIT, *STOCK_UNITS}
    custom_cron = getattr(case, "custom_cron", None)
    if custom_cron is not None:
        custom_cron.assert_inhibited()
        expected.add(custom_cron.source.name)
    routes = draft["systemd_routes"]
    if not isinstance(routes, list):
        raise AssertionError("route proposal has no actual systemd catalog")
    seen = set()
    for route in routes:
        if route["unit"] in expected:
            route["role"] = "restarter"
            seen.add(route["unit"])
    if seen != expected:
        raise AssertionError("a real fixture watcher/service/timer/socket or inhibited stock route is absent from the complete catalog")


def _close_and_approve(case: Case, caddy_image: str, authorization_id: str, *, stop_manager: Callable[[], None] | None = None) -> None:
    caddyfile = case.root / "Caddyfile"
    _write_private(caddyfile, b'{\n admin off\n auto_https off\n}\n:8080 {\n respond "synthetic-containment-fixture" 200\n}\n')
    for name in ("caddy-data", "caddy-config"):
        (case.root / name).mkdir(mode=0o700)
    case.proxy = _docker(
        "create",
        "--name",
        case.fixture_id + "-proxy",
        "--network",
        "container:" + case.manager,
        "--mount",
        f"type=bind,src={caddyfile},dst=/etc/caddy/Caddyfile,readonly",
        "--mount",
        f"type=bind,src={case.root / 'caddy-data'},dst=/data",
        "--mount",
        f"type=bind,src={case.root / 'caddy-config'},dst=/config",
        "--entrypoint",
        "caddy",
        caddy_image,
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    )
    _docker("start", case.proxy)
    case.watcher = WatcherFixture.start(case.root, case.manager, case.manager_image, case.proxy, caddy_image, caddyfile)
    watcher_output = case.root / "evidence" / "actual-watcher-output.log"
    _write_private(watcher_output, case.watcher.stdout.read_bytes())
    case.artifacts.append(watcher_output)
    deadline = time.monotonic() + 10
    while True:
        connection = http.client.HTTPConnection("127.0.0.1", case.prior_manager.published_port, timeout=5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read(1025)
            if response.status != 200 or body != b"synthetic-containment-fixture":
                raise AssertionError("the real original Caddy did not serve its synthetic configuration")
            proof = case.root / "evidence" / "actual-proxy-response.json"
            _write_private(proof, _json({"status": response.status, "body_sha256": hashlib.sha256(body).hexdigest(), "host": "127.0.0.1", "port": case.prior_manager.published_port}))
            case.artifacts.append(proof)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
        finally:
            connection.close()
    proxy = capture_original_caddy_container(case.proxy, caddy_image, expected_mounts=((str(caddyfile), "/etc/caddy/Caddyfile", False), (str(case.root / "caddy-data"), "/data", True), (str(case.root / "caddy-config"), "/config", True)))
    if proxy.net_namespace != case.prior_manager.net_namespace:
        raise AssertionError("the actual Caddy process is not in the original manager namespace")
    installed_source = case.root / "bootstrap-source.py"
    _docker("cp", case.manager + ":" + INSTALLED_BOOTSTRAP, str(installed_source))
    installed_source.chmod(0o600)
    expected_source = _REVIEWED_ORDINARY_SOURCE_SHA256 if case.profile == "ordinary-v1" else _REVIEWED_QUARANTINED_SOURCE_SHA256
    if hashlib.sha256(installed_source.read_bytes()).hexdigest() != expected_source:
        raise AssertionError("fixture image is not the reviewed original bootstrap")
    _docker("stop", "--time", "1", case.proxy)
    if callable(stop_manager):
        stop_manager()
    else:
        _docker("stop", "--time", "1", case.manager)
    case.owner_fd = os.open(case.state / ".supervisor-owner.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    fcntl.flock(case.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    info = os.fstat(case.owner_fd)
    if (info.st_dev, info.st_ino) != (case.prior_manager.owner_lock_device, case.prior_manager.owner_lock_inode):
        raise AssertionError("actual owner fence differs from live capture")
    _assert_pid_flock(os.getpid(), case.owner_fd, "WRITE")
    owner = OriginalOwnerFencePin(os.getpid(), _start_token(os.getpid()), case.pin.holder_boot_id, case.owner_fd, info.st_dev, info.st_ino)
    captured = case.root / "captured"
    captured.mkdir(mode=0o700)
    prior_digest = _write_evidence(captured, {"management": asdict(case.prior_manager), "proxy": asdict(proxy)})
    case.prior_evidence = captured / "prior-evidence.json"
    originals = [
        _original(case.config, "bootstrap-config"),
        _original(case.management_config, "management-config"),
        _original(installed_source, "bootstrap-source"),
        _original(caddyfile, "proxy-config"),
        _original(case.watcher.config, "watcher-config"),
        _original(Path(proxy_watch_cli.__file__).resolve(strict=True), "watcher-source"),
    ]
    if case.support_source is not None:
        originals.append(_original(case.support_source, "support-source"))
    enrollment = ApprovedEnrollment(
        "approved-current-containment-enrollment-v3",
        case.fixture_id,
        case.fixture_id,
        authorization_id,
        case.profile,
        case.prior_manager.engine_id,
        case.manager,
        case.manager_image,
        case.proxy,
        caddy_image,
        tuple(sorted(originals, key=lambda item: item.purpose)),
        str(case.prior_evidence),
        prior_digest,
        True,
        case.pin,
        owner,
    )
    _write_private(APPROVAL_ROOT / (case.fixture_id + ".json"), _json(asdict(enrollment)))
    case.scope = case.root / "scope.json"
    _write_private(case.scope, _json({"deployment_id": case.fixture_id, "enrollment_id": case.fixture_id, "authorization_id": authorization_id, "profile": case.profile, "management_id": case.manager, "proxy_id": case.proxy, "files": []}))
    case.routes = DisposableSystemdRoutes.install(fixture_id=case.fixture_id, manager_id=case.manager)
    scope = read_scope(case.scope)
    approval = observe_approved_enrollment(scope)
    draft = propose_unapproved_route_draft(approval, inspect_complete_docker(scope), closed_profile=True)
    if draft["complete_ordinary_routes_attested"] is not False or draft["local_script_routes"] or draft["remote_script_origins"]:
        raise AssertionError("route proposal unexpectedly acquired independent approval")
    _classify_fixture_restarter_routes(case, draft)
    # The separately authorized VM is synthetic and initially empty. Fixture
    # setup owns all management origins and explicitly approves that closed
    # construction; the producer only reads this preexisting root authority.
    draft["complete_ordinary_routes_attested"] = True
    _write_private(APPROVAL_ROOT / (case.fixture_id + ".routes.json"), _json(draft))
    assert case.stock_routes is not None
    case.protected = (
        case.state,
        case.oauth,
        case.config.parent,
        case.watcher.state,
        case.worker_config,
        case.prior_evidence,
        case.scope,
        APPROVAL_ROOT / (case.fixture_id + ".json"),
        APPROVAL_ROOT / (case.fixture_id + ".routes.json"),
        *case.stock_routes.mask_paths,
        *(Path(item.path) for item in originals),
        *case.extra_protected,
    )
    for name, value in (("approved-enrollment.json", asdict(enrollment)), ("approved-routes.json", draft)):
        path = case.root / "evidence" / name
        _write_private(path, _json(value))
        case.artifacts.append(path)


_NONLEADER = """import ctypes,json,os,sys,threading,time
fd=int(sys.argv[1])
def occupy():
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.setns(fd,0):
        raise OSError(ctypes.get_errno(), 'setns')
    print(json.dumps({'pid':os.getpid(),'tid':threading.get_native_id()}),flush=True)
    while True:
        time.sleep(1)
thread=threading.Thread(target=occupy)
thread.start()
thread.join()
"""


def _escaped_nonleader(case: Case) -> None:
    process = subprocess.Popen((sys.executable, "-I", "-B", "-c", _NONLEADER, str(case.namespace_fd)), pass_fds=(case.namespace_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        if not select.select([process.stdout], [], [], 15)[0]:
            raise AssertionError("escaped nonleader fixture did not enter the original namespace")
        value = json.loads(process.stdout.readline())
        if value.get("pid") != process.pid or value.get("tid") == process.pid or os.readlink(f"/proc/{process.pid}/ns/net") == case.prior_manager.net_namespace or os.readlink(f"/proc/{process.pid}/task/{value['tid']}/ns/net") != case.prior_manager.net_namespace:
            raise AssertionError("escaped nonleader control lacks the required real namespace split")
        case.refusal_pair("escaped-nonleader", authority="kernel-subcomponent")
    finally:
        process.terminate()
        process.communicate(timeout=15)


def _race_and_producer_death(case: Case, *, kill: bool) -> None:
    """Pause the real producer after a kernel-observed read; never inject its result."""
    assert case.scope is not None
    library = ctypes.CDLL(None, use_errno=True)
    descriptor = library.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    original = case.config.read_bytes()
    label = "producer-death" if kill else "configuration-race"
    receipt = PreservationReceipt(case, label, "observe", intentional_config=None if kill else case.config)
    process: subprocess.Popen[str] | None = None
    error: BaseException | None = None
    report: dict[str, object] | None = None
    try:
        try:
            receipt.capture("before")
            if library.inotify_add_watch(descriptor, os.fsencode(case.config), 0x20) < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch")
            process = subprocess.Popen((sys.executable, "-I", "-B", "-m", "auto_coder.management.containment_observation", "observe", "--scope", str(case.scope)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            receipt.spawned(process)
            if not select.select([descriptor], [], [], 60)[0]:
                raise AssertionError("actual producer never opened its protected original configuration")
            os.read(descriptor, 4096)
            process.send_signal(signal.SIGSTOP)
            deadline = time.monotonic() + 5
            while Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("actual producer did not pause within its observation")
                time.sleep(0.01)
            receipt.capture("paused")
            if not receipt.compare("before", "paused"):
                raise AssertionError("in-flight production observation changed preserved fixture state")
            if kill:
                process.kill()
                output, _ = process.communicate(timeout=15)
                if process.returncode != -signal.SIGKILL or output.strip():
                    raise AssertionError("killed in-flight producer published a completed containment report")
            else:
                case.config.write_bytes(original + b"\n")
                receipt.mutation_applied = True
                receipt.capture("post-fixture-mutation")
                process.send_signal(signal.SIGCONT)
                output, _ = process.communicate(timeout=240)
                path = case.root / "evidence" / "configuration-race.json"
                _write_private(path, output.encode())
                case.artifacts.append(path)
                report = assert_report(json.loads(output), process.returncode, accepted=False)
                reasons = report["unavailable"]
                if not isinstance(reasons, list) or not {item["authority"] for item in reasons}.intersection({"approved-enrollment-component", "installed-bootstrap-mount-profile", "complete-observation-race"}):
                    raise AssertionError("contested configuration did not cause a source/read-window refusal")
        except BaseException as exc:
            error = exc
            raise
        finally:
            # Finalize this producer's actual interval before starting the
            # independent consumer or restoring the intentional fixture write.
            receipt.finish(process, error)
        if kill:
            case.command("independent-after-producer-death", accepted=True, verify=False)
        else:
            assert report is not None
            verified = case.command("configuration-race-verify", accepted=False, authority="approved-enrollment-component")
            _assert_independent(report, verified)
    finally:
        os.close(descriptor)
        if case.config.read_bytes() != original:
            case.config.write_bytes(original)


def _alternative_cron_controller_negative(case: Case) -> None:
    """Misclassify one real, fully catalogued controller to test cue detection."""
    assert case.scope is not None
    manifest = APPROVAL_ROOT / (case.fixture_id + ".routes.json")
    original = manifest.read_bytes()
    source = Path("/run/systemd/system") / (case.fixture_id + "-cron-alternative.service")
    content = b"[Unit]\nDescription=Disposable alternative cron controller\n[Service]\nType=oneshot\nExecStart=/usr/bin/run-parts /etc/cron.daily\n"
    _write_private(source, content)
    identity = CronDispatchFixture._identity(source)
    try:
        _systemctl("daemon-reload")
        scope = read_scope(case.scope)
        draft = propose_unapproved_route_draft(observe_approved_enrollment(scope), inspect_complete_docker(scope), closed_profile=True)
        draft = json.loads(_json(draft))
        draft["complete_ordinary_routes_attested"] = True
        _classify_fixture_restarter_routes(case, draft)
        matches = [route for route in draft["systemd_routes"] if route["unit"] == source.name]
        if len(matches) != 1 or matches[0]["role"] != "restarter":
            raise AssertionError("actual source readback failed to identify the alternative run-parts controller")
        # A deliberately wrong approval is a negative fixture, never a
        # claimed successful observation or a generated launch grant.
        matches[0]["role"] = "unrelated"
        manifest.write_bytes(_json(draft))
        evidence = case.root / "evidence" / "misclassified-run-parts-approval.json"
        _write_private(evidence, _json(draft))
        case.artifacts.append(evidence)
        reports = case.refusal_pair("excluded-run-parts-controller", authority="approved-route-manifest")
        _require_refusal_reason(reports, "detected management systemd route is excluded: " + source.name)
    finally:
        manifest.write_bytes(original)
        if CronDispatchFixture._identity(source) != identity or source.read_bytes() != content:
            raise AssertionError("fixture alternative controller was externally replaced")
        source.unlink()
        _systemctl("daemon-reload")


def _allow_restored_cron_proofs(prior: dict[str, object], current: dict[str, object], *, old_digest: str, new_digest: str, mask: Path, target: str = "cron.service") -> None:
    """Permit only one actual remasked controller's fields to change."""
    info = mask.lstat()
    expected_identity = [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid]
    old_entries, new_entries = prior["cron_entries"], current["cron_entries"]
    if not isinstance(old_entries, list) or not isinstance(new_entries, list) or len(old_entries) != len(new_entries):
        raise AssertionError("cron catalog changed while restoring its mask")
    count = 0
    for old, new in zip(old_entries, new_entries):
        proof = old.get("inhibited_source")
        if proof is None:
            continue
        observed = new.get("inhibited_source")
        if not isinstance(proof, dict) or not isinstance(observed, dict) or proof.get("scheduler_unit") != "cron.service" or observed.get("scheduler_unit") != "cron.service":
            raise AssertionError("restored cron proof changed its scheduler authority")
        old_controllers, new_controllers = proof.get("inhibited_services"), observed.get("inhibited_services")
        if not isinstance(old_controllers, list) or not isinstance(new_controllers, list) or [target, old_digest] not in old_controllers or [target, new_digest] not in new_controllers:
            raise AssertionError("restored cron proof is not bound to the actual remasked controller")
        proof["inhibited_services"] = [[name, new_digest if name == target else digest] for name, digest in old_controllers]
        if target == "cron.service":
            if proof.get("mask_path") != str(mask) or observed.get("mask_path") != str(mask) or proof.get("scheduler_sha256") != old_digest or observed.get("scheduler_sha256") != new_digest or observed.get("mask_identity") != expected_identity:
                raise AssertionError("restored cron proof is not bound to the actual new mask")
            proof["scheduler_sha256"] = observed["scheduler_sha256"]
            proof["mask_identity"] = observed["mask_identity"]
        if old != new:
            raise AssertionError("another cron source or task authority changed while restoring its mask")
        count += 1
    if not count:
        raise AssertionError("the approved stock cron inhibition proof is absent")


def _unmasked_cron_negative(case: Case, manifest: Path) -> None:
    assert case.stock_routes is not None
    mask = STOCK_MASK_ROOT / "cron.service"
    if mask not in case.stock_routes.created or _stock_mask_identity(mask) != case.stock_routes.identities[mask]:
        raise AssertionError("fixture will not unmask an unowned cron controller")
    _systemctl("unmask", "cron.service")
    try:
        case.refusal_pair("unmasked-cron-controller", authority="approved-route-manifest")
    finally:
        _systemctl("mask", "cron.service")
        case.stock_routes.identities[mask] = _stock_mask_identity(mask)
        case.stock_routes.assert_inhibited()
    _reapprove_restored_mask(case, manifest, target="cron.service")


def _reapprove_restored_mask(case: Case, manifest: Path, *, target: str | None = None) -> None:
    """Approve only the known recreated fixture mask after exact comparison."""
    assert case.scope is not None and case.routes is not None
    case.routes.assert_inhibited()
    prior = json.loads(manifest.read_bytes())
    scope = read_scope(case.scope)
    current = propose_unapproved_route_draft(observe_approved_enrollment(scope), inspect_complete_docker(scope), closed_profile=True)
    current = json.loads(_json(current))
    current["complete_ordinary_routes_attested"] = True
    routes = current["systemd_routes"]
    if not isinstance(routes, list):
        raise AssertionError("restored route approval has no systemd catalog")
    _classify_fixture_restarter_routes(case, current)
    target = target or case.routes.names[0]
    changed = [route for route in routes if route["unit"] == target]
    if len(changed) != 1:
        raise AssertionError("restored fixture mask is absent from actual catalog")
    for route in prior["systemd_routes"]:
        if route["unit"] == target:
            old_digest = route["sha256"]
            route["sha256"] = changed[0]["sha256"]
            _allow_restored_cron_proofs(prior, current, old_digest=old_digest, new_digest=changed[0]["sha256"], mask=STOCK_MASK_ROOT / target, target=target)
    if current != prior:
        raise AssertionError("another approved authority changed while restoring the one fixture mask")
    manifest.write_bytes(_json(current))
    evidence = case.root / "evidence" / ("restored-" + target + "-mask-approval.json")
    _write_private(evidence, _json(current))
    case.artifacts.append(evidence)


def _negative_cases(case: Case) -> None:
    assert case.previous is not None and case.routes is not None
    case.baseline("genuine-cron-retained-task")
    _cron_task_negative(case)
    assert case.custom_cron is not None
    case.custom_cron.assert_inhibited()
    case.baseline("custom-cron-retained-task")
    _cron_task_negative(case, controller=case.custom_cron.source.name, label="custom-cron-retained-task")
    case.baseline("excluded-run-parts-controller")
    _alternative_cron_controller_negative(case)
    case.baseline("escaped-nonleader")
    _escaped_nonleader(case)
    case.baseline("conflicting-host-listener")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", case.prior_manager.published_port))
        listener.listen(1)
        case.refusal_pair("conflicting-host-listener", authority="kernel-subcomponent")
    case.baseline("enabled-docker-restart")
    _docker("update", "--restart=unless-stopped", case.proxy)
    try:
        case.refusal_pair("enabled-docker-restart", authority="docker-restart:" + case.proxy)
    finally:
        _docker("update", "--restart=no", case.proxy)
    case.baseline("unselected-alternate-bootstrap-root")
    alternate_root = case.root / "alternate-state"
    alternate_root.mkdir(mode=0o700)
    alternative = _docker(
        "create", "--name", case.fixture_id + "-alternative-root", "--network", "none", "--mount", f"type=bind,src={alternate_root},dst=/management-child-state", "--entrypoint", "python", case.manager_image, "-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json"
    )
    try:
        case.refusal_pair("unselected-alternate-bootstrap-root", authority="docker-alternative-management:" + alternative)
    finally:
        _docker("rm", alternative)
    case.baseline("source-substitution")
    source = case.root / "bootstrap-source.py"
    substituted = case.root / "substituted-bootstrap.py"
    _write_private(substituted, source.read_bytes() + b"\n# Disposable source-substitution control.\n")
    if case.profile == "ordinary-v1":
        _docker("cp", str(substituted), case.manager + ":" + INSTALLED_BOOTSTRAP)
        try:
            case.refusal_pair("installed-source-substitution", authority="installed-bootstrap-source")
        finally:
            _docker("cp", str(source), case.manager + ":" + INSTALLED_BOOTSTRAP)
    else:
        original_source = source.read_bytes()
        source.write_bytes(substituted.read_bytes())
        try:
            case.refusal_pair("enrolled-source-substitution", authority="approved-enrollment-component")
        finally:
            source.write_bytes(original_source)
    case.baseline("original-bind-source-path-replacement")
    original_directory = case.config.parent
    detached = original_directory.with_name(original_directory.name + "-original-bind")
    original_directory.rename(detached)
    try:
        shutil.copytree(detached, original_directory)
        case.refusal_pair("original-bind-source-path-replacement", authority="approved-enrollment-component")
    finally:
        if original_directory.exists():
            shutil.rmtree(original_directory)
        detached.rename(original_directory)
    case.baseline("producer-death")
    _race_and_producer_death(case, kill=True)
    case.baseline("configuration-race")
    _race_and_producer_death(case, kill=False)
    case.baseline("cross-scope-original-identities")
    assert case.scope is not None
    scope_content = case.scope.read_bytes()
    cross_scope = json.loads(scope_content)
    cross_scope["management_id"], cross_scope["proxy_id"] = cross_scope["proxy_id"], cross_scope["management_id"]
    case.scope.write_bytes(_json(cross_scope))
    try:
        case.refusal_pair("cross-scope-original-identities", authority="approved-enrollment-component")
    finally:
        case.scope.write_bytes(scope_content)
    case.baseline("omitted-installed-timer")
    manifest = APPROVAL_ROOT / (case.fixture_id + ".routes.json")
    content = manifest.read_bytes()
    omitted = json.loads(content)
    omitted["systemd_routes"] = [route for route in omitted["systemd_routes"] if route["unit"] != case.routes.names[1]]
    manifest.write_bytes(_json(omitted))
    try:
        case.refusal_pair("omitted-installed-timer", authority="approved-route-manifest")
    finally:
        manifest.write_bytes(content)
    case.baseline("omitted-installed-watcher")
    omitted = json.loads(content)
    omitted["systemd_routes"] = [route for route in omitted["systemd_routes"] if route["unit"] != WATCHER_UNIT]
    manifest.write_bytes(_json(omitted))
    try:
        case.refusal_pair("omitted-installed-watcher", authority="approved-route-manifest")
    finally:
        manifest.write_bytes(content)
    if case.support_source is not None:
        case.baseline("support-directory-permissions")
        support = json.loads(case.support_source.read_bytes())
        directory = Path(support["source"])
        mode = stat.S_IMODE(directory.stat().st_mode)
        directory.chmod(mode | stat.S_IWGRP)
        try:
            case.refusal_pair("support-directory-permissions", authority="installed-bootstrap-mount-profile")
        finally:
            directory.chmod(mode)
    case.baseline("released-original-owner-fence")
    fcntl.flock(case.owner_fd, fcntl.LOCK_UN)
    try:
        case.refusal_pair("released-original-owner-fence", authority="kernel-subcomponent")
    finally:
        fcntl.flock(case.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    case.baseline("enabled-installed-service")
    _systemctl("unmask", case.routes.names[0])
    try:
        case.refusal_pair("enabled-installed-service", authority="approved-route-manifest")
    finally:
        _systemctl("mask", case.routes.names[0])
    _reapprove_restored_mask(case, manifest)
    case.baseline("unmasked-cron-controller")
    _unmasked_cron_negative(case, manifest)
    case.baseline("released-original-namespace-pin")
    os.close(case.namespace_fd)
    case.namespace_fd = -1
    case.refusal_pair("released-original-namespace-pin", authority="kernel-subcomponent")


def _cleanup_case_lifetime(case: Case) -> None:
    """Release this control's runtime resources while retaining its evidence."""
    try:
        case.cleanup()
    finally:
        if case.owner_fd >= 0:
            os.close(case.owner_fd)
            case.owner_fd = -1


def _quarantine_runtime_lineage(prepared: PreparedQuarantine) -> dict[str, object]:
    configuration_bytes = prepared.bootstrap_config.read_bytes()
    configuration = json.loads(configuration_bytes)
    mounts = {destination: (source, writable) for source, destination, writable in prepared.mounts}
    if any(sum(destination == name for _, destination, _ in prepared.mounts) != 1 for name in ("/management-source", "/management-versions")):
        raise AssertionError("paired quarantine source/runtime mount identities are ambiguous")
    return {
        "image_id": prepared.image_id,
        "reviewed_bootstrap_source_sha256": _REVIEWED_QUARANTINED_SOURCE_SHA256,
        "bootstrap_configuration_sha256": hashlib.sha256(configuration_bytes).hexdigest(),
        "management_configuration_sha256": hashlib.sha256(prepared.management_config.read_bytes()).hexdigest(),
        "instance_id": configuration["instance_id"],
        "initial_runtime": configuration["initial_runtime"],
        "fresh_runtime_binding": configuration["fresh_runtime_binding"],
        "source_repository": mounts["/management-source"][0],
        "sealed_versions": mounts["/management-versions"][0],
        "source_mount_read_only": not mounts["/management-source"][1],
        "versions_mount_read_only": not mounts["/management-versions"][1],
    }


def _quarantine_variant_pair(
    *,
    root: Path,
    variant: str,
    template: PreparedQuarantine,
    ordinary: Case,
    source_repository: Path,
    caddy_image: str,
    authorization_id: str,
    worker: subprocess.Popen[str],
    worker_config: Path,
    stock_routes: InhibitedStockRoutes,
    cron_dispatch: CronDispatchFixture,
    custom_cron: MaskedCronController,
    cases: list[Case],
    artifact_groups: list[list[Path]] | None = None,
) -> dict[str, object]:
    """Run a fresh genuine supported control before one immutable-input fault."""
    from scripts.containment_quarantine_fixture import NEGATIVE_VARIANTS, prepare_quarantine

    if variant not in NEGATIVE_VARIANTS or template.prior is None or ordinary.prior_evidence is None:
        raise ValueError("paired quarantine variant requires actual original evidence")

    def prepare(selected: str, source: PreparedQuarantine) -> PreparedQuarantine:
        return prepare_quarantine(
            root=root / ("quarantine-" + variant + ("-control" if selected == "supported" else "")),
            name="ac-test-pr2829-" + uuid.uuid4().hex,
            image_id=template.image_id,
            source_repository=source_repository,
            predecessor=ordinary.prior_manager,
            prior_evidence_path=ordinary.prior_evidence,
            prior_lock=ordinary.state / ".supervisor-owner.lock",
            host_listener_port=template.prior.published_port,
            docker=_docker,
            variant=selected,
            runtime_template=source,
        )

    def capture(prepared: PreparedQuarantine) -> Case:
        case = _quarantine_case(prepared, worker, worker_config, artifact_groups=artifact_groups)
        case.stock_routes, case.cron_dispatch, case.custom_cron = stock_routes, cron_dispatch, custom_cron
        case.extra_protected += (cron_dispatch.source, custom_cron.source, custom_cron.mask)
        cases.append(case)
        return case

    with prepare("supported", template) as control, ExitStack() as cleanup:
        control_case = capture(control)
        cleanup.callback(_cleanup_case_lifetime, control_case)
        _close_and_approve(control_case, caddy_image, authorization_id, stop_manager=control.stop)
        first, second = control_case.positive_pair()
        lineage = _quarantine_runtime_lineage(control)
        if lineage != _quarantine_runtime_lineage(template) or lineage["source_mount_read_only"] is not True or lineage["versions_mount_read_only"] is not True:
            raise AssertionError("paired supported control changed the genuine original runtime lineage")
    if not control.stopped or control_case.owner_fd >= 0 or control_case.namespace_fd >= 0 or control.guard.pidfd >= 0 or control.guard.listener.fileno() >= 0 or _docker("ps", "-aq", "--no-trunc"):
        raise AssertionError("paired supported control resources remain before the invalid variant")

    with prepare(variant, control) as altered, ExitStack() as cleanup:
        variant_case = capture(altered)
        cleanup.callback(_cleanup_case_lifetime, variant_case)
        _close_and_approve(variant_case, caddy_image, authorization_id, stop_manager=altered.stop)
        reports = variant_case.refusal_pair(variant, authority="installed-bootstrap-mount-profile")
        for report in reports:
            source = report.get("installed_bootstrap_source")
            if not isinstance(report.get("approved_enrollment"), dict) or not isinstance(source, dict) or source.get("source_sha256") != _REVIEWED_QUARANTINED_SOURCE_SHA256 or source.get("reviewed_lock_before_launch") is not True:
                raise AssertionError("unsupported quarantine variant did not reach the actual enrolled original-source profile")
        variant_lineage = _quarantine_runtime_lineage(altered)
        expected_lineage = {**lineage, "source_mount_read_only": False} if variant == "writable-support-mount" else lineage
        if variant_lineage != expected_lineage:
            raise AssertionError("paired invalid variant changed the genuine source/config/runtime lineage")
        if control_case.manager == variant_case.manager or control_case.fixture_id == variant_case.fixture_id or second["finished_monotonic_ns"] > reports[0]["started_monotonic_ns"]:
            raise AssertionError("paired control and invalid variant do not have distinct ordered lifetimes")
        evidence = variant_case.root / "evidence" / "paired-supported-control.json"
        mapping = {
            "schema": "genuine-quarantine-variant-control-v1",
            "variant": variant,
            "lineage": lineage,
            "control": {
                "manager_id": control_case.prior_manager.container_id,
                "pid": control_case.prior_manager.pid,
                "start_token": control_case.prior_manager.start_token,
                "boot_id": control_case.pin.holder_boot_id,
                "enrollment_id": control_case.fixture_id,
                "observe": str(control_case.root / "evidence" / "observe.json"),
                "verify": str(control_case.root / "evidence" / "verify.json"),
                "observation_ids": [first["observation_id"], second["observation_id"]],
                "finished_monotonic_ns": second["finished_monotonic_ns"],
                "runtime_resources_released_before_variant": True,
            },
            "invalid_configuration": {
                "manager_id": variant_case.prior_manager.container_id,
                "pid": variant_case.prior_manager.pid,
                "start_token": variant_case.prior_manager.start_token,
                "boot_id": variant_case.pin.holder_boot_id,
                "enrollment_id": variant_case.fixture_id,
                "observe": str(variant_case.root / "evidence" / (variant + "-observe.json")),
                "verify": str(variant_case.root / "evidence" / (variant + "-verify.json")),
                "observation_ids": [report["observation_id"] for report in reports],
                "started_monotonic_ns": reports[0]["started_monotonic_ns"],
                "required_refusal": "installed-bootstrap-mount-profile",
                "lineage": variant_lineage,
                "live_capture": str(altered.root / "variant-live-capture.json"),
            },
            "artifact_path": str(evidence),
        }
        _write_private(evidence, _json(mapping))
        variant_case.artifacts.append(evidence)
        return mapping


def _absolute_dispatcher_alias_probe(root: Path, artifacts: list[Path]) -> dict[str, object]:
    """Exercise actual packaged-shell aliases and systemd source readback."""
    directory = root / "absolute-alias-probe"
    directory.mkdir(mode=0o700)
    tools_directory = directory / "tools"
    tools_directory.mkdir(mode=0o700)
    helper = tools_directory / "maintenance"
    alias = directory / "launcher"
    shell = Path("/bin/sh").resolve(strict=True)
    shell_info = shell.stat()
    if shell_info.st_uid != 0 or not stat.S_ISREG(shell_info.st_mode) or shell_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not shell_info.st_mode & stat.S_IXUSR:
        raise AssertionError("native alias probe requires the protected installed shell")
    alias.symlink_to("/bin/sh")
    _write_private(helper, b"#!/bin/sh\nprintf '%s\\n' 'benign-before'\n")
    helper.chmod(0o700)
    unit = CUSTOM_UNIT_ROOT / (root.name + "-absolute-alias.service")
    evidence = root / "absolute-alias-dispatch.json"
    if unit.exists() or unit.is_symlink():
        raise FileExistsError("native alias probe unit already exists")
    module = Path(containment_route_sources.__file__).resolve(strict=True)
    outcome: dict[str, object] = {
        "schema": "native-absolute-dispatcher-alias-probe-v1",
        "status": "failed",
        "unit": unit.name,
        "module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        "installed_shell": str(shell),
        "installed_shell_sha256": hashlib.sha256(shell.read_bytes()).hexdigest(),
        "alias": str(alias),
        "helper": str(helper),
        "positive_controls": [],
        "substitution_phases": [],
    }
    unit_identity: dict[str, int] | None = None
    unit_digest: str | None = None

    def identity(path: Path) -> dict[str, int]:
        info = path.lstat()
        return {name: getattr(info, "st_" + name) for name in ("dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns")}

    def install_command(command: str) -> None:
        nonlocal unit_identity, unit_digest
        content = ("[Unit]\nDescription=Disposable absolute dispatch probe\n[Service]\nType=oneshot\nEnvironment=PATH=" + str(tools_directory) + "\nExecStart=" + command + "\n").encode()
        if unit_identity is None:
            _write_private(unit, content)
        else:
            if identity(unit) != unit_identity or hashlib.sha256(unit.read_bytes()).hexdigest() != unit_digest:
                raise AssertionError("owned native alias probe unit changed")
            unit.write_bytes(content)
        unit_identity = identity(unit)
        unit_digest = hashlib.sha256(content).hexdigest()
        _systemctl("daemon-reload")

    def unchanged_inputs() -> dict[str, object]:
        properties = subprocess.run(("systemctl", "show", "--property=ExecStart,Environment", "--", unit.name), capture_output=True, text=True, check=True, timeout=20).stdout
        return {
            "directory": identity(tools_directory),
            "alias": identity(alias),
            "alias_target": os.readlink(alias),
            "shell": identity(shell),
            "shell_sha256": hashlib.sha256(shell.read_bytes()).hexdigest(),
            "unit": identity(unit),
            "unit_sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
            "actual_systemd_properties": properties,
        }

    try:
        controls = []
        for command in ("/usr/bin/true", "/bin/sh -c /usr/bin/true"):
            install_command(command)
            fact = _systemd_unit(unit.name, extra_cues=("/srv/original-state",), inspect_local_inputs=True)
            if fact.management_candidate or fact.main_pid != 0 or fact.active_state != "inactive":
                raise AssertionError("native direct packaged command control was not unrelated and inactive")
            controls.append({"command": command, "fact": asdict(fact)})
        outcome["positive_controls"] = controls
        install_command(str(alias) + " -c maintenance")
        phases = []
        helper_lifetimes: list[tuple[int, int]] = []
        for label, expected in (("before", "benign-before\n"), ("after", "/srv/original-state\n")):
            if label == "after":
                helper.write_bytes(b"#!/bin/sh\nprintf '%s\\n' '/srv/original-state'\n")
            before = unchanged_inputs()
            helper_identity = identity(helper)
            helper_lifetimes.append((helper_identity["dev"], helper_identity["ino"]))
            helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
            executed = subprocess.run((str(alias), "-c", "maintenance"), env={"PATH": str(tools_directory)}, capture_output=True, text=True, check=True, timeout=10)
            if executed.stdout != expected or executed.stderr:
                raise AssertionError("native shell alias did not execute the actual controlled PATH helper")
            try:
                _systemd_unit(unit.name, extra_cues=("/srv/original-state",), inspect_local_inputs=True)
            except containment_route_sources.CommandSourceUnavailable as exc:
                reason = str(exc)
                if reason != "resolved executable alias uses an unsupported dispatcher profile":
                    raise AssertionError("native shell alias refused for an unexpected source predicate") from exc
            else:
                raise AssertionError("native systemd source reader accepted unbound absolute shell alias dispatch")
            if unchanged_inputs() != before or identity(helper) != helper_identity or hashlib.sha256(helper.read_bytes()).hexdigest() != helper_digest:
                raise AssertionError("native alias execution or readback changed its controlled inputs")
            phases.append({"phase": label, "unchanged_inputs": before, "helper_identity": helper_identity, "helper_sha256": helper_digest, "actual_argv": [str(alias), "-c", "maintenance"], "actual_stdout": executed.stdout, "actual_returncode": executed.returncode, "source_refusal": reason})
            outcome["substitution_phases"] = phases
        first, second = phases
        if first["unchanged_inputs"] != second["unchanged_inputs"] or helper_lifetimes[0] != helper_lifetimes[1] or first["helper_sha256"] == second["helper_sha256"]:
            raise AssertionError("native helper substitution did not preserve alias/unit/environment/directory identity")
        outcome["status"] = "passed"
    except BaseException as exc:
        outcome["failure"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            if unit_identity is not None:
                if identity(unit) != unit_identity or hashlib.sha256(unit.read_bytes()).hexdigest() != unit_digest:
                    raise AssertionError("owned native alias probe unit changed before cleanup")
                unit.unlink()
                _systemctl("daemon-reload")
                if unit.name in _systemd_catalog():
                    raise AssertionError("owned native alias probe unit remains in the actual catalog")
            outcome["unit_cleanup_completed"] = True
        except BaseException as exc:
            cleanup_error = exc
            outcome["status"] = "failed"
            outcome["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
        _write_private(evidence, _json(outcome))
        artifacts.append(evidence)
        if cleanup_error is not None:
            raise cleanup_error
    return outcome


def run_fixture(*, authorization_id: str, ordinary_image: str, caddy_image: str, quarantine_image: str | None = None, source_repository: Path | None = None) -> Path:
    if (quarantine_image is None) != (source_repository is None):
        raise ValueError("quarantine image and genuine reviewed source checkout must be selected together")
    authorization = require_disposable_host(authorization_id)
    for image in (ordinary_image, caddy_image, *((quarantine_image,) if quarantine_image is not None else ())):
        _assert_image(image)
    if source_repository is not None and (not source_repository.is_absolute() or source_repository != source_repository.resolve(strict=True)):
        raise ValueError("reviewed source checkout must be an absolute canonical directory")
    parent = Path("/var/lib/auto-coder")
    parent.mkdir(mode=0o700, exist_ok=True)
    for root in (APPROVAL_ROOT, FIXTURE_ROOT, RESULT_ROOT):
        _private_directory(root)
    summary = RESULT_ROOT / (authorization_id + ".json")
    if summary.exists() or summary.is_symlink():
        raise FileExistsError("this disposable-host authorization already has a result")
    root = FIXTURE_ROOT / ("ac-test-pr2829-" + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    logger.remove()
    logger.add(sys.stdout, colorize=True, format="{time} | {level} | {file}:{function}:{line} | {message}")
    logger.add(root / "test.log", rotation="2 MB", format="{time} | {level} | {file}:{function}:{line} | {message}")
    worker_config = root / "preserved-worker.json"
    _write_private(worker_config, _json({"fixture": "unrelated-preserved-worker", "backend": "none"}))
    worker = subprocess.Popen((sys.executable, "-I", "-B", "-c", "import signal; signal.pause()"), text=True)
    cases: list[Case] = []
    stock_routes: InhibitedStockRoutes | None = None
    cron_dispatch: CronDispatchFixture | None = None
    custom_cron: MaskedCronController | None = None
    stock_evidence: list[Path] = []
    prepared_artifacts: list[list[Path]] = []
    completed_profiles: list[str] = []
    refused_variants: list[str] = []
    variant_pairs: list[dict[str, object]] = []
    result: dict[str, object] = {"schema": "disposable-current-containment-acceptance-v1", "status": "failed", "authorization": authorization, "profiles": completed_profiles, "quarantine_refused_variants": refused_variants, "quarantine_variant_pairs": variant_pairs, "artifact_paths": []}
    try:
        result["absolute_alias_dispatch_probe"] = _absolute_dispatcher_alias_probe(root, stock_evidence)
        stock_routes = InhibitedStockRoutes.install()
        result["stock_systemd_inhibition"] = stock_routes.evidence()
        stock_record = root / "stock-systemd-inhibition.json"
        _write_private(stock_record, _json(result["stock_systemd_inhibition"]))
        stock_evidence.append(stock_record)
        cron_dispatch = CronDispatchFixture.install(root.name)
        result["cron_dispatch_fixture"] = {"source": str(cron_dispatch.source), "sha256": cron_dispatch.digest, "identity": list(cron_dispatch.identity)}
        cron_record = root / "cron-dispatch-fixture.json"
        _write_private(cron_record, _json(result["cron_dispatch_fixture"]))
        stock_evidence.append(cron_record)
        custom_cron = MaskedCronController.install(root.name)
        custom_record = root / "custom-cron-controller.json"
        result["custom_cron_controller"] = {"source": str(custom_cron.source), "source_identity": list(custom_cron.source_identity), "source_sha256": hashlib.sha256(custom_cron.source.read_bytes()).hexdigest(), "mask": str(custom_cron.mask), "mask_identity": list(custom_cron.mask_identity or ())}
        _write_private(custom_record, _json(result["custom_cron_controller"]))
        stock_evidence.append(custom_record)
        ordinary = _prepare_ordinary(root / "ordinary", ordinary_image, worker, worker_config)
        ordinary.stock_routes = stock_routes
        ordinary.cron_dispatch = cron_dispatch
        ordinary.custom_cron = custom_cron
        ordinary.extra_protected += (cron_dispatch.source, custom_cron.source, custom_cron.mask)
        cases.append(ordinary)
        _close_and_approve(ordinary, caddy_image, authorization_id)
        ordinary.positive_pair()
        _negative_cases(ordinary)
        ordinary.cleanup()
        completed_profiles.append("ordinary-v1")
        if quarantine_image is not None and source_repository is not None:
            from scripts.containment_quarantine_fixture import NEGATIVE_VARIANTS, prepare_quarantine

            assert ordinary.prior_evidence is not None
            # The genuine next bootstrap takes a shared fence on the old
            # owner inode. Its new exclusive owner lock remains separate.
            fcntl.flock(ordinary.owner_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            quarantine_root = root / "quarantined"
            with (
                prepare_quarantine(
                    root=quarantine_root,
                    name="ac-test-pr2829-" + uuid.uuid4().hex,
                    image_id=quarantine_image,
                    source_repository=source_repository,
                    predecessor=ordinary.prior_manager,
                    prior_evidence_path=ordinary.prior_evidence,
                    prior_lock=ordinary.state / ".supervisor-owner.lock",
                    host_listener_port=_port(),
                    docker=_docker,
                ) as prepared,
                ExitStack() as cleanup,
            ):
                case = _quarantine_case(prepared, worker, worker_config, artifact_groups=prepared_artifacts)
                case.stock_routes = stock_routes
                case.cron_dispatch = cron_dispatch
                case.custom_cron = custom_cron
                case.extra_protected += (cron_dispatch.source, custom_cron.source, custom_cron.mask)
                cases.append(case)
                # Remove the proxy before the helper removes the manager that
                # supplies its network namespace, including assertion failures.
                cleanup.callback(case.cleanup)
                _close_and_approve(case, caddy_image, authorization_id, stop_manager=prepared.stop)
                case.positive_pair()
                _negative_cases(case)
                case.cleanup()
                completed_profiles.append("quarantined-original-v1")
            template = prepared
            assert template.prior is not None
            for variant in NEGATIVE_VARIANTS:
                paired = _quarantine_variant_pair(
                    root=root,
                    variant=variant,
                    template=template,
                    ordinary=ordinary,
                    source_repository=source_repository,
                    caddy_image=caddy_image,
                    authorization_id=authorization_id,
                    worker=worker,
                    worker_config=worker_config,
                    stock_routes=stock_routes,
                    cron_dispatch=cron_dispatch,
                    custom_cron=custom_cron,
                    cases=cases,
                    artifact_groups=prepared_artifacts,
                )
                variant_pairs.append(paired)
                refused_variants.append(variant)
        result["status"] = "passed"
    except BaseException as exc:
        result["failure"] = type(exc).__name__ + ": " + str(exc)
        logger.exception("Disposable full-containment acceptance failed")
        raise
    finally:
        cleanup_errors = []
        for case in reversed(cases):
            try:
                case.cleanup()
            except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
            if case.owner_fd >= 0:
                os.close(case.owner_fd)
                case.owner_fd = -1
        if custom_cron is not None:
            try:
                custom_cron.cleanup()
            except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        if cron_dispatch is not None:
            try:
                cron_dispatch.cleanup()
            except (OSError, ValueError, AssertionError) as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        if stock_routes is not None:
            try:
                stock_routes.cleanup()
            except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        worker.terminate()
        worker.wait(timeout=15)
        result["artifact_paths"] = [str(path) for case in cases for path in case.artifacts] + [str(path) for group in prepared_artifacts for path in group] + [str(path) for path in stock_evidence] + [str(root / "test.log")]
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["status"] = "failed"
        _write_private(summary, _json(result))
        logger.info("Full fixture result: {}", summary)
        if cleanup_errors:
            raise AssertionError("fixture cleanup did not complete: " + repr(cleanup_errors))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Full production CLI acceptance on a separately authorized empty disposable VM")
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--ordinary-image", required=True)
    parser.add_argument("--caddy-image", required=True)
    parser.add_argument("--quarantine-image")
    parser.add_argument("--source-repository", type=Path)
    arguments = parser.parse_args()
    if (arguments.quarantine_image is None) != (arguments.source_repository is None):
        parser.error("--quarantine-image and --source-repository are required together")
    run_fixture(authorization_id=arguments.authorization_id, ordinary_image=arguments.ordinary_image, caddy_image=arguments.caddy_image, quarantine_image=arguments.quarantine_image, source_repository=arguments.source_repository)


if __name__ == "__main__":
    main()
