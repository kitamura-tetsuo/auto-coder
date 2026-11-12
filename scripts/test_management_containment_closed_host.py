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

from auto_coder.management import proxy_watch_cli
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
STOCK_UNITS = ("motd-news.timer", "motd-news.service")
STOCK_MASK_ROOT = Path("/etc/systemd/system")
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
            raise AssertionError("required stock service/timer are absent from the installed catalog")
        initial = tuple(_systemd_unit(name) for name in STOCK_UNITS)
        if any(fact.unit_file_state not in {"enabled", "disabled", "static", "masked"} for fact in initial):
            raise AssertionError("required stock service/timer has no supported installed unit state")
        state = cls(initial_facts=initial)
        for name in STOCK_UNITS:
            path = STOCK_MASK_ROOT / name
            if path.exists() or path.is_symlink():
                state.identities[path] = _stock_mask_identity(path)
        try:
            # Reloading an active timer's mask can leave it failed instead of
            # inactive. Stop both original units before changing their files.
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

    @classmethod
    def start(cls, root: Path, manager: str, manager_image: str, proxy: str, proxy_image: str, caddyfile: Path) -> WatcherFixture:
        state = root / "watcher-state"
        state.mkdir(mode=0o700)
        config, output = root / "watcher-config.json", root / "watcher-output.log"
        _write_private(config, _json({"state_root": str(state), "manager_id": manager, "manager_image": manager_image, "proxy_id": proxy, "proxy_image": proxy_image, "config_sha256": hashlib.sha256(caddyfile.read_bytes()).hexdigest()}))
        _write_private(output, b"")
        source = Path("/run/systemd/system") / WATCHER_UNIT
        command = f"{sys.executable} -I -B -m auto_coder.management.proxy_watch_cli --config {config}"
        if any(character in command for character in ("\n", "\r", "%", '"')) or " " in sys.executable or " " in str(root):
            raise ValueError("fixture watcher command contains unsupported systemd syntax")
        content = f"[Unit]\nDescription=Disposable original management proxy watcher\n[Service]\nType=simple\nExecStart={command}\nRestart=no\nStandardOutput=append:{output}\nStandardError=append:{output}\n".encode()
        _write_private(source, content)
        result = cls(source, source.lstat().st_ino, hashlib.sha256(content).hexdigest(), config, state, output)
        try:
            _systemctl("daemon-reload")
            _systemctl("start", WATCHER_UNIT)
            deadline = time.monotonic() + 30
            while "ATTACHED\n" not in output.read_text():
                if time.monotonic() >= deadline:
                    raise AssertionError("the real installed watcher never observed Caddy attached")
                time.sleep(0.1)
            pid = subprocess.run(("systemctl", "show", "--property=MainPID", "--value", WATCHER_UNIT), check=True, capture_output=True, text=True, timeout=10).stdout.strip()
            if not pid.isdigit() or int(pid) <= 1:
                raise AssertionError("the real watcher has no live unit-owned process")
            _systemctl("stop", WATCHER_UNIT)
            _systemctl("mask", WATCHER_UNIT)
            observe_watcher_unit()
            return result
        except BaseException:
            result.cleanup()
            raise

    def cleanup(self) -> None:
        _systemctl("stop", WATCHER_UNIT)
        mask = Path("/etc/systemd/system") / WATCHER_UNIT
        if mask.is_symlink() and os.readlink(mask) == "/dev/null":
            _systemctl("unmask", WATCHER_UNIT)
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
        before = _footprint(self.protected, self.worker)
        logger.info("Running {} {} through the actual production CLI", self.profile, label)
        result = subprocess.run(arguments, capture_output=True, text=True, check=False, timeout=240)
        if _footprint(self.protected, self.worker) != before:
            raise AssertionError("read-only CLI changed preserved state, OAuth, configuration or worker lifetime")
        output = self.root / "evidence" / (label + ".json")
        _write_private(output, result.stdout.encode())
        self.artifacts.append(output)
        report = assert_report(json.loads(result.stdout), result.returncode, accepted=accepted, authority=authority)
        expected = _REVIEWED_ORDINARY_SOURCE_SHA256 if self.profile == "ordinary-v1" else _REVIEWED_QUARANTINED_SOURCE_SHA256
        if accepted:
            source, profile = report["installed_bootstrap_source"], report["installed_mount_profile"]
            if not isinstance(source, dict) or not isinstance(profile, dict) or source.get("source_sha256") != expected or profile.get("profile") != self.profile:
                raise AssertionError("positive report substituted the original reviewed source profile")
        return report

    def positive_pair(self) -> None:
        first = self.command("observe", accepted=True, verify=False)
        self.previous = self.root / "evidence" / "observe.json"
        second = self.command("verify", accepted=True)
        _assert_independent(first, second)

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


def _quarantine_case(prepared: PreparedQuarantine, worker: subprocess.Popen[str], worker_config: Path) -> Case:
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
    preserved = _footprint(case.protected, case.worker)
    process: subprocess.Popen[str] | None = None
    try:
        if library.inotify_add_watch(descriptor, os.fsencode(case.config), 0x20) < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch")
        process = subprocess.Popen((sys.executable, "-I", "-B", "-m", "auto_coder.management.containment_observation", "observe", "--scope", str(case.scope)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([descriptor], [], [], 60)[0]:
            raise AssertionError("actual producer never opened its protected original configuration")
        os.read(descriptor, 4096)
        process.send_signal(signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("actual producer did not pause within its observation")
            time.sleep(0.01)
        if _footprint(case.protected, case.worker) != preserved:
            raise AssertionError("in-flight production observation changed preserved fixture state")
        if kill:
            process.kill()
            output, _ = process.communicate(timeout=15)
            if process.returncode != -signal.SIGKILL or output.strip():
                raise AssertionError("killed in-flight producer published a completed containment report")
            if _footprint(case.protected, case.worker) != preserved:
                raise AssertionError("producer death changed preserved fixture state")
            case.command("independent-after-producer-death", accepted=True, verify=False)
        else:
            case.config.write_bytes(original + b"\n")
            before = _footprint(case.protected, case.worker)
            process.send_signal(signal.SIGCONT)
            output, _ = process.communicate(timeout=240)
            if _footprint(case.protected, case.worker) != before:
                raise AssertionError("contested observation changed fixture state")
            report = assert_report(json.loads(output), process.returncode, accepted=False)
            reasons = report["unavailable"]
            if not isinstance(reasons, list) or not {item["authority"] for item in reasons}.intersection({"approved-enrollment-component", "installed-bootstrap-mount-profile", "complete-observation-race"}):
                raise AssertionError("contested configuration did not cause a source/read-window refusal")
            path = case.root / "evidence" / "configuration-race.json"
            _write_private(path, output.encode())
            case.artifacts.append(path)
            verified = case.command("configuration-race-verify", accepted=False, authority="approved-enrollment-component")
            _assert_independent(report, verified)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=15)
        os.close(descriptor)
        if case.config.read_bytes() != original:
            case.config.write_bytes(original)


def _reapprove_restored_mask(case: Case, manifest: Path) -> None:
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
    target = case.routes.names[0]
    changed = [route for route in routes if route["unit"] == target]
    if len(changed) != 1:
        raise AssertionError("restored fixture mask is absent from actual catalog")
    for route in prior["systemd_routes"]:
        if route["unit"] == target:
            route["sha256"] = changed[0]["sha256"]
    if current != prior:
        raise AssertionError("another approved authority changed while restoring the one fixture mask")
    manifest.write_bytes(_json(current))
    evidence = case.root / "evidence" / "restored-mask-approval.json"
    _write_private(evidence, _json(current))
    case.artifacts.append(evidence)


def _negative_cases(case: Case) -> None:
    assert case.previous is not None and case.routes is not None
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
    case.baseline("released-original-namespace-pin")
    os.close(case.namespace_fd)
    case.namespace_fd = -1
    case.refusal_pair("released-original-namespace-pin", authority="kernel-subcomponent")


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
    stock_evidence: list[Path] = []
    completed_profiles: list[str] = []
    refused_variants: list[str] = []
    result: dict[str, object] = {"schema": "disposable-current-containment-acceptance-v1", "status": "failed", "authorization": authorization, "profiles": completed_profiles, "quarantine_refused_variants": refused_variants, "artifact_paths": []}
    try:
        stock_routes = InhibitedStockRoutes.install()
        result["stock_systemd_inhibition"] = stock_routes.evidence()
        stock_record = root / "stock-systemd-inhibition.json"
        _write_private(stock_record, _json(result["stock_systemd_inhibition"]))
        stock_evidence.append(stock_record)
        ordinary = _prepare_ordinary(root / "ordinary", ordinary_image, worker, worker_config)
        ordinary.stock_routes = stock_routes
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
                case = _quarantine_case(prepared, worker, worker_config)
                case.stock_routes = stock_routes
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
                with (
                    prepare_quarantine(
                        root=root / ("quarantine-" + variant),
                        name="ac-test-pr2829-" + uuid.uuid4().hex,
                        image_id=quarantine_image,
                        source_repository=source_repository,
                        predecessor=ordinary.prior_manager,
                        prior_evidence_path=ordinary.prior_evidence,
                        prior_lock=ordinary.state / ".supervisor-owner.lock",
                        host_listener_port=template.prior.published_port,
                        docker=_docker,
                        variant=variant,
                        runtime_template=template,
                    ) as altered,
                    ExitStack() as cleanup,
                ):
                    variant_case = _quarantine_case(altered, worker, worker_config)
                    variant_case.stock_routes = stock_routes
                    cases.append(variant_case)
                    cleanup.callback(variant_case.cleanup)
                    _close_and_approve(variant_case, caddy_image, authorization_id, stop_manager=altered.stop)
                    reports = variant_case.refusal_pair(variant, authority="installed-bootstrap-mount-profile")
                    for report in reports:
                        source = report.get("installed_bootstrap_source")
                        if not isinstance(report.get("approved_enrollment"), dict) or not isinstance(source, dict) or source.get("source_sha256") != _REVIEWED_QUARANTINED_SOURCE_SHA256 or source.get("reviewed_lock_before_launch") is not True:
                            raise AssertionError("unsupported quarantine variant did not reach the actual enrolled original-source profile")
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
        if stock_routes is not None:
            try:
                stock_routes.cleanup()
            except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
                cleanup_errors.append(type(exc).__name__ + ": " + str(exc))
        worker.terminate()
        worker.wait(timeout=15)
        result["artifact_paths"] = [str(path) for case in cases for path in case.artifacts] + [str(path) for path in stock_evidence] + [str(root / "test.log")]
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
