"""Real-object helpers for the separately authorized disposable recovery VM.

The calling harness first runs require_disposable_host on the initially empty
VM. These helpers retain live original namespaces, capture actual records and
install an explicitly reviewed fixture scope; the production observer never
imports this module and never performs these fixture effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol

from scripts.test_management_containment_closed_host import MARKER, WatcherFixture, read_authorization
from scripts.test_management_containment_enrollment_host import _private_directory, _write_private

from auto_coder.management import proxy_watch_cli
from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.containment_kernel_subobservation import _holder_start_token, _require_capture_host
from auto_coder.management.containment_observation import observe_recovery_docker
from auto_coder.management.containment_route_manifest import observe_recovery_route_manifest, observe_selected_bind_roots, propose_unapproved_route_draft
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import _engine_id
from auto_coder.management.installed_bootstrap_mount_profile import _inspect
from auto_coder.management.original_attempts import canonical, source_identity
from auto_coder.management.recovery_containment import capture_recovery_original_pair, load_enrollment, route_selection
from auto_coder.management.recovery_containment_model import RECOVERY_ENROLLMENT_ROOT, RECOVERY_PROFILE, RecoveryEnrollment, RecoveryEnrollmentObservation, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryTarget, RecoveryWatcher
from auto_coder.management.recovery_containment_profile import _mapped_file, read_bound_file


class Generation(Protocol):
    name: str
    root: Path
    container: str
    proxy: str | None

    def path(self, name: str) -> Path: ...


class OriginalChain(Protocol):
    image: str
    proxy_image: str


def selected_file(path: Path, *, source: bool = False) -> RecoveryFile:
    resolved = path.resolve(strict=True)
    info = resolved.lstat()
    result = RecoveryFile(str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest(), info.st_dev, info.st_ino)
    read_bound_file(result, source=source)
    return result


def selected_lock(path: Path) -> RecoveryLock:
    if path != path.resolve(strict=True):
        raise ValueError("fixture lock is not canonical")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ValueError("fixture lock is not the actual private original object")
    return RecoveryLock(str(path), info.st_dev, info.st_ino)


@dataclass
class CapturedGeneration:
    original: RecoveryOriginal
    namespace_fd: int

    def close(self) -> None:
        if self.namespace_fd >= 0:
            os.close(self.namespace_fd)
            self.namespace_fd = -1


def capture_generation(chain: OriginalChain, generation: Generation, *, generation_id: str | None = None) -> CapturedGeneration:
    """Capture while the genuine manager and paired Caddy are still alive."""
    _require_capture_host()
    if generation.proxy is None:
        raise ValueError("the original paired Caddy must already exist")
    mounts = observe_selected_bind_roots((generation.container, generation.proxy))
    bootstrap = selected_file(generation.path("bootstrap") / "config.json")
    config = json.loads(read_bound_file(bootstrap))
    management_destination = config.get("management_config")
    if management_destination is None:
        runtime = next(item for item in config["runtimes"] if item["reference"] == config["initial_runtime"])
        arguments = runtime["arguments"]
        management_destination = arguments[arguments.index("--config") + 1]
    manager_mounts = tuple(item for item in mounts if item.container_id == generation.container)
    management = selected_file(Path(_mapped_file(management_destination, manager_mounts, writable=False)))
    owner = selected_lock(generation.path("state") / ".supervisor-owner.lock")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    preliminary = RecoveryOriginal(generation_id or generation.name, config["instance_id"], generation.container, chain.image, generation.proxy, chain.proxy_image, "started", boot, bootstrap, management, selected_file(generation.path("Caddyfile")), mounts, owner, None, None)
    actual = _inspect(generation.container)
    state = actual.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 1:
        raise ValueError("fixture original is not currently running")
    descriptor = os.open(f"/proc/{state['Pid']}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        namespace = os.readlink(f"/proc/self/fd/{descriptor}")
        info = os.fstat(descriptor)
        captured_boot, manager, proxy = capture_recovery_original_pair(preliminary, source=source_identity())
        if captured_boot != boot or manager.net_namespace != namespace:
            raise ValueError("fixture original namespace changed during capture")
        directory = generation.path("recovery-original-capture")
        directory.mkdir(mode=0o700)
        _write_evidence(directory, {"management": asdict(manager), "proxy": asdict(proxy)})
        pin = OriginalNetworkPin(os.getpid(), _holder_start_token(os.getpid()), boot, descriptor, namespace, info.st_dev, info.st_ino)
        complete = replace(preliminary, prior_evidence=selected_file(directory / "prior-evidence.json"), namespace_pin=pin)
        parsed = RecoveryOriginal.parse(json.loads(json.dumps(asdict(complete))))
        return CapturedGeneration(parsed, descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def start_watcher(generation: Generation, original: RecoveryOriginal, *, first: bool = False) -> tuple[WatcherFixture, RecoveryWatcher]:
    """Run, observe and inhibit the real watcher before retaining its EX lock."""
    unit = "auto-coder-management-proxy-watch" + ("" if first else "-" + generation.name) + ".service"
    fixture = WatcherFixture.start(generation.root, original.management_id, original.management_image_id, original.proxy_id, original.proxy_image_id, Path(original.proxy_config.path), unit=unit)
    selected = RecoveryWatcher(unit, selected_file(fixture.config), selected_file(Path(proxy_watch_cli.__file__), source=True), selected_lock(fixture.state / "proxy-watch.lock"))
    return fixture, selected


def target_from_container(container_id: str, *, bootstrap_config: Path, management_config: Path, published_port: int) -> RecoveryTarget:
    """Select the real already running prelaunch target and its created owner."""
    _require_capture_host()
    inspected = _inspect(container_id)
    config = json.loads(bootstrap_config.read_bytes())
    mounts = observe_selected_bind_roots((container_id,))
    state = next(item.source for item in mounts if item.destination == config["state_root"])
    image = inspected.get("Image")
    if not isinstance(image, str):
        raise ValueError("fixture target image is absent")
    return RecoveryTarget.parse(json.loads(json.dumps(asdict(RecoveryTarget(config["instance_id"], container_id, image, selected_file(bootstrap_config), selected_file(management_config), mounts, selected_lock(Path(state) / ".supervisor-owner.lock"), published_port)))))


def enroll_recovery_scope(
    captures: tuple[CapturedGeneration, ...],
    target: RecoveryTarget,
    watchers: tuple[RecoveryWatcher, ...],
    *,
    deployment_id: str,
    enrollment_id: str,
    authorization: dict[str, object],
) -> RecoveryEnrollmentObservation:
    """Install fixture approval only under the still-current separate VM marker.

    The caller owns complete controller setup/inhibition. The route draft is
    inspected here and marked approved only after all detected candidate and
    explicitly selected watcher routes already have their real inhibited facts.
    This helper neither stops controllers nor treats an unapproved list as proof.
    """
    _require_capture_host()
    actual_authorization = read_authorization(MARKER)
    if actual_authorization != authorization or authorization["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip() or authorization["machine_id"] != Path("/etc/machine-id").read_text().strip() or authorization["engine_id"] != _engine_id():
        raise PermissionError("fixture authorization no longer selects this exact VM")
    selected = RecoveryEnrollment(
        "approved-management-recovery-containment-v1",
        deployment_id,
        enrollment_id,
        str(authorization["authorization_id"]),
        RECOVERY_PROFILE,
        str(authorization["engine_id"]),
        source_identity(),
        tuple(sorted((item.original for item in captures), key=lambda item: item.generation_id)),
        target,
        tuple(sorted(watchers, key=lambda item: item.unit)),
        True,
    )
    RecoveryEnrollment.parse(json.loads(json.dumps(asdict(selected))))
    _private_directory(RECOVERY_ENROLLMENT_ROOT)
    path = RECOVERY_ENROLLMENT_ROOT / (enrollment_id + ".json")
    route_path = RECOVERY_ENROLLMENT_ROOT / (enrollment_id + ".routes.json")
    if path.exists() or path.is_symlink() or route_path.exists() or route_path.is_symlink():
        raise FileExistsError("fixture recovery approval already exists")
    _write_private(path, canonical(asdict(selected)))
    approved = load_enrollment(enrollment_id)
    managers = tuple(item.management_id for item in selected.originals) + (selected.target.management_id,)
    docker = observe_recovery_docker(managers, tuple(item.proxy_id for item in selected.originals))
    draft = propose_unapproved_route_draft(route_selection(approved), docker, closed_profile=True)
    # The isolated host harness has already audited its stock routes. This is
    # the explicit separately authorized fixture approval, not observer output.
    draft["complete_ordinary_routes_attested"] = True
    _write_private(route_path, canonical(draft))
    observe_recovery_route_manifest(route_selection(approved), docker)
    return approved
