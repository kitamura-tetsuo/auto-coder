"""Exercise protected enrollment and kernel checks on a disposable Docker fixture.

This manually invoked host probe never authorizes full containment. It installs
only a uniquely identified test-only approval component, verifies it in two
separate subprocesses, and removes only resources it created. A successful
kernel *sub-observation* is not positive full-profile containment acceptance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from auto_coder.management.containment_enrollment import ApprovedEnrollment, OriginalFile, OriginalNetworkPin, observe_approved_enrollment
from auto_coder.management.containment_observation import _docker as inspect_complete_docker
from auto_coder.management.containment_observation import read_scope
from auto_coder.management.containment_route_manifest import propose_unapproved_route_draft
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import HostObservationUnavailable, PriorContainer, capture_prior_container
from auto_coder.management.installed_bootstrap_source import _REVIEWED_ORDINARY_SOURCE_SHA256

APPROVAL_ROOT = Path("/var/lib/auto-coder/approved-containment-enrollments")
FIXTURE_ROOT = Path("/var/lib/auto-coder/containment-readback-fixtures")
IMAGE_TAG = "auto-coder:management-local-abc65544"
INSTALLED_BOOTSTRAP = "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/management/bootstrap_supervisor.py"
_SYSTEMD_UNIT_ROOT = Path("/run/systemd/system")
_SYSTEMD_MASK_ROOT = Path("/etc/systemd/system")


def _docker(*arguments: str, timeout: int = 30) -> str:
    result = subprocess.run(("docker", *arguments), check=True, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def _private_directory(root: Path) -> bool:
    if root.is_symlink() or not root.is_absolute() or root != root.resolve(strict=False):
        raise ValueError("fixture root is not canonical")
    created = not root.exists()
    if created:
        root.mkdir(mode=0o700)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("fixture root must be an existing root-private directory")
    return created


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _original(path: Path, purpose: str) -> OriginalFile:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("test original is not protected")
    return OriginalFile(purpose, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino)


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _capture_manager(container: str, image: str, state: Path, bootstrap: Path, oauth: Path) -> PriorContainer:
    deadline = time.monotonic() + 20
    while True:
        try:
            return capture_prior_container(
                container,
                image,
                expected_mounts=((str(state), "/management-child-state", True), (str(bootstrap), "/bootstrap", False), (str(oauth), "/management-oauth", True)),
                require_owner_lock=True,
                require_deployment_identity=True,
            )
        except HostObservationUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _call_cli(command: str, scope: Path, previous: Path | None = None) -> dict[str, object]:
    arguments = [sys.executable, "-m", "auto_coder.management.containment_observation", command, "--scope", str(scope)]
    if previous is not None:
        arguments.extend(("--previous", str(previous)))
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=90, check=False)
    if result.returncode != 2:
        raise AssertionError("containment CLI unexpectedly returned a non-refusal code")
    report = json.loads(result.stdout)
    if not isinstance(report, dict) or report.get("status") != "unavailable":
        raise AssertionError("test-only enrollment incorrectly produced an accepted containment verdict")
    return report


def _authorities(report: dict[str, object]) -> set[str]:
    reasons = report.get("unavailable")
    if not isinstance(reasons, list):
        raise AssertionError("containment refusal reasons are missing")
    authorities: set[str] = set()
    for item in reasons:
        if not isinstance(item, dict) or not isinstance(item.get("authority"), str):
            raise AssertionError("containment refusal reason is malformed")
        authorities.add(item["authority"])
    return authorities


def _verify_expected(scope: Path, previous: Path, expected: set[str]) -> dict[str, object]:
    """Require explicit evidence, retrying only unrelated Docker inventory races."""
    report: dict[str, object] = {}
    for _ in range(8):
        report = _call_cli("verify", scope, previous)
        authorities = _authorities(report)
        if expected <= authorities:
            return report
        if "docker" not in authorities:
            break
        time.sleep(0.2)
    safe_reasons = [(item["authority"], item["reason"]) for item in report.get("unavailable", []) if isinstance(item, dict) and item.get("authority") in ("docker", "saved-report:docker", "kernel-subcomponent")]
    raise AssertionError("required independent refusal was not observed: " + repr({"expected": sorted(expected), "actual": sorted(_authorities(report)), "diagnostics": safe_reasons}))


def _assert_component(report: dict[str, object], *, present: bool) -> None:
    if (report.get("approved_enrollment") is not None) != present:
        raise AssertionError("protected enrollment component state differs")
    authorities = _authorities(report)
    if ("approved-enrollment-component" in authorities) == present:
        raise AssertionError("protected enrollment refusal component differs")
    if not {"deployment-enrollment", "ordinary-route-closure", "original-source-profile", "kernel-containment"} <= authorities:
        raise AssertionError("test-only enrollment removed normative containment refusals")


def _assert_kernel_subcomponent(report: dict[str, object], *, present: bool, pinned: bool = False) -> None:
    component = report.get("kernel_subobservation")
    authorities = _authorities(report)
    if (component is not None) != present:
        diagnostics = {
            "kernel-subcomponent",
            "docker",
            "approved-route-manifest",
            "saved-report:docker",
            "saved-report:route_manifest",
            "saved-report:kernel_subobservation",
        }
        details = [item for item in report.get("unavailable", []) if isinstance(item, dict) and item.get("authority") in diagnostics]
        raise AssertionError("real kernel sub-observation presence differs: " + repr(details))
    if present:
        if not isinstance(component, dict) or component.get("retained_original_network_pin") is not False:
            raise AssertionError("kernel component claimed an observer-owned retained namespace pin")
        if not all(component.get(key) is True for key in ("original_cgroups_empty", "unpinned_network_task_scan_absent", "host_listener_absent")):
            raise AssertionError("kernel component has incomplete original task or listener checks")
        if component.get("external_namespace_pin_verified") is not pinned:
            raise AssertionError("external original namespace pin observation differs")
        if ("kernel-subcomponent-unpinned" in authorities) == pinned:
            raise AssertionError("kernel namespace pin refusal reason differs")
    elif "kernel-subcomponent" not in authorities:
        raise AssertionError("kernel refusal did not identify its unavailable component")


def _assert_route_component(report: dict[str, object]) -> None:
    component = report.get("route_manifest")
    if not isinstance(component, dict) or not isinstance(component.get("systemd"), list) or not component["systemd"]:
        reasons = [(item["authority"], item["reason"]) for item in report.get("unavailable", []) if isinstance(item, dict) and item.get("authority") in ("docker", "approved-route-manifest", "approved-enrollment-component")]
        raise AssertionError("test-only root route component was not observed by the real CLI: " + repr(reasons))
    if component.get("manifest", {}).get("complete_ordinary_routes_attested") is not True:
        raise AssertionError("test-only fixture root attestation was not read")
    if "approved-route-manifest" in _authorities(report):
        raise AssertionError("separately inspected test-only route component was refused")
    if not {"deployment-enrollment", "ordinary-route-closure", "original-source-profile", "kernel-containment"} <= _authorities(report):
        raise AssertionError("test-generated route component incorrectly removed normative full-profile refusals")


def _verify_after_transient_docker_inventory(scope: Path, previous: Path) -> dict[str, object]:
    """Retry only observed Docker inventory races; never revise an approved route."""
    report: dict[str, object] = {}
    for attempt in range(10):
        report = _call_cli("verify", scope, previous)
        authorities = _authorities(report)
        if isinstance(report.get("route_manifest"), dict) and isinstance(report.get("kernel_subobservation"), dict) and not {"saved-report:docker", "saved-report:route_manifest", "saved-report:kernel_subobservation"} & authorities:
            return report
        reasons = report.get("unavailable", [])
        transient = (
            any(
                isinstance(item, dict)
                and isinstance(item.get("reason"), str)
                and ((item.get("authority") == "docker" and item.get("reason") == "Docker configuration or lifetime changed during observation") or (item.get("authority") == "approved-route-manifest" and "whole-engine Docker inventory differs from approved routes" in item.get("reason", "")))
                for item in reasons
            )
            or "saved-report:docker" in authorities
        )
        if not transient or attempt == 9:
            break
        logger.warning("Independent verifier observed a transient Docker inventory change; retrying the unchanged approval")
        time.sleep(0.3)
    return report


def _assert_reviewed_ordinary_source(report: dict[str, object]) -> None:
    """Require a real installed image source matching the fixed reviewed bytes."""
    component = report.get("installed_bootstrap_source")
    if not isinstance(component, dict) or component.get("reviewed_lock_before_launch") is not True or component.get("source_sha256") != _REVIEWED_ORDINARY_SOURCE_SHA256 or type(component.get("source_bytes")) is not int or component["source_bytes"] <= 0:
        reasons = [item for item in report.get("unavailable", []) if isinstance(item, dict) and item.get("authority") == "installed-bootstrap-source"]
        raise AssertionError("reviewed original-source readback is unavailable: " + repr(reasons))
    if "installed-bootstrap-source" in _authorities(report):
        raise AssertionError("reviewed installed source produced contradictory refusal")


def _assert_reviewed_ordinary_mounts(report: dict[str, object]) -> None:
    """Require original mount and config binding from the production CLI."""
    component = report.get("installed_mount_profile")
    if (
        not isinstance(component, dict)
        or component.get("profile") != "ordinary-v1"
        or component.get("bootstrap_config_in_container") != "/bootstrap/config.json"
        or component.get("management_config_in_container") != "/bootstrap/management.yaml"
        or not all(isinstance(component.get(field), str) and len(component[field]) == 64 for field in ("management_original_mounts_sha256", "proxy_original_mounts_sha256", "bootstrap_config_sha256", "management_config_sha256"))
    ):
        reasons = [item for item in report.get("unavailable", []) if isinstance(item, dict) and item.get("authority") == "installed-bootstrap-mount-profile"]
        raise AssertionError("reviewed original ordinary mount/config readback is unavailable: " + repr(reasons))
    if "installed-bootstrap-mount-profile" in _authorities(report):
        raise AssertionError("original ordinary mount/config readback produced contradictory refusal")


def _systemctl(*arguments: str) -> None:
    """Perform only explicitly named disposable-fixture systemd operations."""
    try:
        subprocess.run(("systemctl", *arguments), capture_output=True, text=True, check=True, timeout=20)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AssertionError("disposable systemd action failed: " + " ".join(arguments)) from exc


@dataclass
class DisposableSystemdRoutes:
    """Three runtime-only restarters protected by persistent test-only masks."""

    names: tuple[str, ...]
    sources: tuple[tuple[Path, int, str], ...]
    masked: bool = False

    @classmethod
    def install(cls, *, fixture_id: str, manager_id: str) -> DisposableSystemdRoutes:
        from auto_coder.management.containment_route_manifest import _systemd_catalog

        if not re.fullmatch(r"ac-test-pr2829-[0-9a-f]{32}", fixture_id) or not re.fullmatch(r"[0-9a-f]{64}", manager_id):
            raise ValueError("disposable systemd fixture identity differs")
        if not _SYSTEMD_UNIT_ROOT.is_dir() or not _SYSTEMD_MASK_ROOT.is_dir():
            raise ValueError("installed systemd directories are unavailable")
        prefix = fixture_id + "-management"
        names = (prefix + ".service", prefix + ".timer", prefix + ".socket")
        if set(names).intersection(_systemd_catalog()):
            raise ValueError("disposable systemd unit is already installed or loaded")
        source_texts = (
            "[Unit]\nDescription=Disposable PR2829 manager restart fixture\n" "[Service]\nType=oneshot\nExecStart=/usr/bin/docker start " + manager_id + "\n",
            "[Unit]\nDescription=Disposable PR2829 manager restart timer\n" "[Timer]\nOnCalendar=*-*-* 00:00:00\nUnit=" + names[0] + "\n",
            "[Unit]\nDescription=Disposable PR2829 manager restart socket\n" "[Socket]\nListenStream=/run/" + prefix + ".sock\nService=" + names[0] + "\n",
        )
        for name in names:
            for root in (_SYSTEMD_UNIT_ROOT, _SYSTEMD_MASK_ROOT, Path("/usr/lib/systemd/system"), Path("/lib/systemd/system")):
                path = root / name
                if path.exists() or path.is_symlink():
                    raise ValueError("disposable systemd unit name already exists")
        state = cls(names, ())
        try:
            for name, content in zip(names, source_texts):
                path = _SYSTEMD_UNIT_ROOT / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                identity = path.lstat()
                state.sources += ((path, identity.st_ino, hashlib.sha256(content.encode("utf-8")).hexdigest()),)
            _systemctl("daemon-reload")
            # A runtime mask reports masked-runtime, which cannot satisfy the
            # independently specified protected permanent-mask requirement.
            # The unit fragments themselves exist only for this runtime fixture.
            _systemctl("mask", *names)
            state.masked = True
            state.assert_inhibited()
            return state
        except BaseException:
            state.cleanup()
            raise

    def assert_inhibited(self) -> None:
        from auto_coder.management.containment_route_manifest import _systemd_unit

        for name in self.names:
            if not _systemd_unit(name).inhibited:
                raise AssertionError("disposable systemd route is not masked and inactive: " + name)

    def temporarily_unmask_and_verify_refusal(self, scope: Path, previous: Path) -> None:
        target = self.names[0]
        _systemctl("unmask", target)
        try:
            observed = _verify_expected(scope, previous, {"approved-route-manifest", "saved-report:route_manifest"})
            if observed.get("route_manifest") is not None:
                raise AssertionError("enabled disposable systemd restarter was accepted")
            logger.info("Unmasking only the disposable manager service invalidated independent route verification")
        finally:
            _systemctl("mask", target)
        self.assert_inhibited()

    def cleanup(self) -> None:
        # Never remove an unexpected path or changed file from the host.
        for name in self.names:
            mask = _SYSTEMD_MASK_ROOT / name
            if mask.is_symlink() and os.readlink(mask) == "/dev/null":
                _systemctl("unmask", name)
            elif mask.exists() or mask.is_symlink():
                raise AssertionError("disposable systemd mask was externally replaced")
        for path, inode, digest in reversed(self.sources):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_ino != inode or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise AssertionError("disposable systemd unit source was externally replaced")
            path.unlink()
        if self.sources:
            _systemctl("daemon-reload")
        for name in self.names:
            if (_SYSTEMD_UNIT_ROOT / name).exists() or (_SYSTEMD_UNIT_ROOT / name).is_symlink() or (_SYSTEMD_MASK_ROOT / name).exists() or (_SYSTEMD_MASK_ROOT / name).is_symlink():
                raise AssertionError("disposable systemd unit remained after cleanup")


def run_fixture(*, exercise_test_route_manifest: bool = False, exercise_disposable_systemd: bool = False, test_image: str = IMAGE_TAG, require_reviewed_ordinary_source: bool = False) -> None:
    if os.geteuid() != 0 or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
        raise PermissionError("host root is required for this isolated fixture")
    if exercise_disposable_systemd and not exercise_test_route_manifest:
        raise ValueError("disposable systemd fixture requires independently read-back route manifest")
    # The image must already exist locally; never pull or replace an image tag.
    if not isinstance(test_image, str) or not 1 <= len(test_image) <= 200 or not re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+)", test_image):
        raise ValueError("test image selection is not an exact local image reference")
    image = _docker("image", "inspect", test_image, "--format", "{{.Id}}")
    if not image.startswith("sha256:") or len(image) != 71:
        raise ValueError("preinstalled test image identity is unavailable")

    created_approval_root = _private_directory(APPROVAL_ROOT)
    created_fixture_root = _private_directory(FIXTURE_ROOT)
    fixture_id = ("ac-test-pr2829-" if exercise_disposable_systemd else "ac-readback-test-") + uuid.uuid4().hex
    private = FIXTURE_ROOT / fixture_id
    private.mkdir(mode=0o700)
    enrollment_file = APPROVAL_ROOT / (fixture_id + ".json")
    test_route_file = APPROVAL_ROOT / (fixture_id + ".routes.json")
    manager_name = fixture_id + "-manager"
    proxy_name = fixture_id + "-proxy"
    created_containers: list[str] = []
    namespace_pin_fd: int | None = None
    systemd_fixture: DisposableSystemdRoutes | None = None
    try:
        logger.add(sys.stdout, level="INFO", format="{time} | {level} | {message}")
        logger.add(private / "test.log", rotation="1 MB", level="INFO")
        state, bootstrap, oauth = (private / name for name in ("state", "bootstrap", "oauth"))
        for directory in (state, bootstrap, oauth):
            directory.mkdir(mode=0o700)
        config = bootstrap / "config.json"
        _write_private(
            config,
            json.dumps(
                {
                    "instance_id": fixture_id,
                    "state_root": "/management-child-state",
                    "oauth_root": "/management-oauth",
                    "journal_root": "/management-child-state/journal",
                    "listener_url": "http://127.0.0.1:8081",
                    "initial_runtime": "bootstrap",
                    "crash_budget": 0,
                    "runtimes": [
                        {
                            "reference": "bootstrap",
                            "receipt": "sha256:b8cb1f05b3261967b1432b6d312f0cc6d787924f9dd84eba2227ac40d597b6d9",
                            "executable": "/opt/auto-coder/.venv/bin/auto-coder",
                            "arguments": ["management", "serve", "--config", "/bootstrap/management.yaml"],
                        }
                    ],
                },
                sort_keys=True,
            ).encode(),
        )
        _write_private(bootstrap / "management.yaml", b"fixture: true\n")
        _write_private(private / "proxy-config", b"fixture-only proxy\n")
        manager = _docker(
            "run",
            "-d",
            "--name",
            manager_name,
            "--user",
            "0:0",
            "--publish",
            f"127.0.0.1:{_port()}:8080",
            "--mount",
            f"type=bind,src={state},dst=/management-child-state",
            "--mount",
            f"type=bind,src={bootstrap},dst=/bootstrap,readonly",
            "--mount",
            f"type=bind,src={oauth},dst=/management-oauth",
            "--entrypoint",
            "python",
            image,
            "-m",
            "auto_coder.management.bootstrap_supervisor",
            "--config",
            "/bootstrap/config.json",
        )
        if not re.fullmatch(r"[0-9a-f]{64}", manager):
            raise AssertionError("fixture Docker returned an ambiguous manager identity")
        created_containers.append(manager)
        prior_manager = _capture_manager(manager, image, state, bootstrap, oauth)
        # Retain the real original nsfs object while the manager is alive; the
        # fixture parent stays in the host network namespace and only holds an
        # FD. The producer and verifier must each independently reopen it.
        namespace_pin_fd = os.open(f"/proc/{prior_manager.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
        if os.readlink(f"/proc/self/fd/{namespace_pin_fd}") != prior_manager.net_namespace:
            raise AssertionError("test-only namespace pin does not match the live original manager")
        pin_stat = os.fstat(namespace_pin_fd)
        pin_holder_start = Path("/proc/self/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19]
        pin = OriginalNetworkPin(os.getpid(), pin_holder_start, Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(), namespace_pin_fd, prior_manager.net_namespace, pin_stat.st_dev, pin_stat.st_ino)
        proxy = _docker("run", "-d", "--name", proxy_name, "--network", "container:" + manager, "--user", "0:0", "--entrypoint", "/bin/sleep", image, "120")
        if not re.fullmatch(r"[0-9a-f]{64}", proxy):
            raise AssertionError("fixture Docker returned an ambiguous proxy identity")
        created_containers.append(proxy)
        prior_proxy = capture_prior_container(proxy, image)
        escaped = _docker("run", "-d", "--name", fixture_id + "-namespace-holder", "--network", "container:" + manager, "--user", "0:0", "--entrypoint", "/bin/sleep", image, "120")
        if not re.fullmatch(r"[0-9a-f]{64}", escaped):
            raise AssertionError("fixture Docker returned an ambiguous namespace-holder identity")
        created_containers.append(escaped)
        escaped_pid = int(_docker("inspect", "--type", "container", "--format", "{{.State.Pid}}", escaped))
        if escaped_pid <= 0 or os.readlink(f"/proc/{escaped_pid}/ns/net") != prior_manager.net_namespace:
            raise AssertionError("live escaped-task control does not occupy the captured original network namespace")
        _docker("cp", manager + ":" + INSTALLED_BOOTSTRAP, str(private / "bootstrap-source"))
        (private / "bootstrap-source").chmod(0o600)
        _docker("stop", "--time", "1", proxy, timeout=25)
        _docker("stop", "--time", "1", manager, timeout=25)
        evidence_dir = private / "captured"
        evidence_dir.mkdir(mode=0o700)
        prior_digest = _write_evidence(evidence_dir, {"management": asdict(prior_manager), "proxy": asdict(prior_proxy)})
        original_files = tuple(
            sorted(
                (
                    _original(config, "bootstrap-config"),
                    _original(private / "bootstrap-source", "bootstrap-source"),
                    _original(bootstrap / "management.yaml", "management-config"),
                    _original(private / "proxy-config", "proxy-config"),
                ),
                key=lambda item: item.purpose,
            )
        )
        approved = ApprovedEnrollment(
            "approved-current-containment-enrollment-v2",
            fixture_id,
            fixture_id,
            "test-only-" + fixture_id,
            "ordinary-v1",
            prior_manager.engine_id,
            prior_manager.container_id,
            prior_manager.image_id,
            prior_proxy.container_id,
            prior_proxy.image_id,
            original_files,
            str(evidence_dir / "prior-evidence.json"),
            prior_digest,
            True,
            pin,
        )
        _write_private(enrollment_file, json.dumps(asdict(approved), sort_keys=True).encode())
        scope = private / "scope.json"
        _write_private(
            scope, json.dumps({"deployment_id": approved.deployment_id, "enrollment_id": approved.enrollment_id, "authorization_id": approved.authorization_id, "profile": approved.profile, "management_id": approved.management_id, "proxy_id": approved.proxy_id, "files": []}, sort_keys=True).encode()
        )
        for _ in range(8):
            first = _call_cli("observe", scope)
            if "kernel-subcomponent" in _authorities(first):
                break
            if "docker" not in _authorities(first):
                break
            time.sleep(0.2)
        _assert_component(first, present=True)
        _assert_kernel_subcomponent(first, present=False)
        logger.info("Original manager and proxy exited, but the independently identified live fixture namespace holder prevents kernel sub-observation")
        _docker("stop", "--time", "1", escaped, timeout=25)
        # Stopping the last namespace occupant leaves only the externally
        # retained original nsfs FD, which two separate CLI processes reopen.
        for _ in range(5):
            first = _call_cli("observe", scope)
            if first.get("kernel_subobservation") is not None:
                break
            time.sleep(0.2)
        _assert_kernel_subcomponent(first, present=True, pinned=True)
        if exercise_test_route_manifest:
            # This is an isolated test-only root fixture simulating review;
            # neither auto-generated source classification nor this flag is
            # original-production route authority or a containment verdict.
            selected_scope = read_scope(scope)
            enrolled = observe_approved_enrollment(selected_scope)
            if exercise_disposable_systemd:
                systemd_fixture = DisposableSystemdRoutes.install(fixture_id=fixture_id, manager_id=manager)
            draft = propose_unapproved_route_draft(enrolled, inspect_complete_docker(selected_scope))
            if draft["complete_ordinary_routes_attested"] is not False or draft["remote_script_origins"] or draft["local_script_routes"]:
                raise AssertionError("route proposal unexpectedly self-authorized or invented unreviewed controller origins")
            if exercise_disposable_systemd:
                # The newly installed units are reviewed *test-only* ordinary
                # restarters. They must be independently identified as such;
                # merely adding a unit to a Docker-scoped caller list is not
                # enrollment or route inhibition.
                assert systemd_fixture is not None
                names = set(systemd_fixture.names)
                if sum(item["unit"] in names for item in draft["systemd_routes"]) != len(names):
                    raise AssertionError("disposable systemd routes are omitted from the full catalog")
                for item in draft["systemd_routes"]:
                    if item["unit"] in names:
                        item["role"] = "restarter"
            draft["complete_ordinary_routes_attested"] = True
            _write_private(test_route_file, json.dumps(draft, sort_keys=True).encode())
            first = _call_cli("observe", scope)
            _assert_route_component(first)
        if require_reviewed_ordinary_source:
            _assert_reviewed_ordinary_source(first)
            _assert_reviewed_ordinary_mounts(first)
        previous = private / "previous.json"
        _write_private(previous, json.dumps(first, sort_keys=True).encode())
        verified = _verify_after_transient_docker_inventory(scope, previous) if exercise_test_route_manifest else _call_cli("verify", scope, previous)
        _assert_component(verified, present=True)
        _assert_kernel_subcomponent(verified, present=True, pinned=True)
        if require_reviewed_ordinary_source:
            _assert_reviewed_ordinary_source(verified)
            _assert_reviewed_ordinary_mounts(verified)
        if exercise_test_route_manifest:
            _assert_route_component(verified)
            logger.info("Test-only root route manifest was independently re-read by the real observe and verify CLI; full containment remained unavailable")
            if systemd_fixture is not None:
                systemd_fixture.temporarily_unmask_and_verify_refusal(scope, previous)
                logger.info("Disposable systemd service, timer and socket were independently inhibited; enabling the service alone caused refusal")
            test_route_file.unlink()
        logger.info("Real stopped Docker manager/proxy and separate verifier independently reopened the same live original namespace pin")
        _docker("update", "--restart=unless-stopped", proxy)
        _verify_expected(scope, previous, {"docker-restart:" + proxy, "saved-report:docker"})
        _docker("update", "--restart=no", proxy)
        logger.info("Enabled and restored a restart policy on the stopped fixture proxy only; separate consumer rejected changed Docker authority")
        alternative = _docker("create", "--name", fixture_id + "-alternate-manager", "--network", "none", "--entrypoint", "/bin/sleep", image, "120")
        if not re.fullmatch(r"[0-9a-f]{64}", alternative):
            raise AssertionError("fixture Docker returned an ambiguous alternative identity")
        created_containers.append(alternative)
        _verify_expected(scope, previous, {"docker-alternative-management:" + alternative, "saved-report:docker"})
        _docker("rm", alternative)
        created_containers.remove(alternative)
        logger.info("Unselected stopped management-image alternative was detected and rejected without touching unrelated Docker objects")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", prior_manager.published_port))
            listener.listen(1)
            conflict = _verify_expected(scope, previous, {"kernel-subcomponent", "saved-report:kernel_subobservation"})
            _assert_kernel_subcomponent(conflict, present=False)
        logger.info("Real loopback listener on the disposable published port caused kernel refusal; it was released")
        # The previous positive kernel component cannot outlive its externally
        # owned pin: closing the one real FD must invalidate the later consumer.
        os.close(namespace_pin_fd)
        namespace_pin_fd = None
        released = _verify_expected(scope, previous, {"kernel-subcomponent", "saved-report:kernel_subobservation"})
        _assert_kernel_subcomponent(released, present=False)
        logger.info("Released the original namespace pin; separate consumer refused the saved positive sub-observation")
        changed = private / "proxy-config"
        changed.write_bytes(b"substituted fixture proxy configuration\n")
        refused = _call_cli("verify", scope, previous)
        _assert_component(refused, present=False)
        if "saved-report:approved_enrollment" not in _authorities(refused):
            raise AssertionError("separate consumer did not reject saved approval after source mutation")
        logger.info("Source substitution refused after producer exit; no full containment verdict was issued")
    finally:
        if systemd_fixture is not None:
            systemd_fixture.cleanup()
        if namespace_pin_fd is not None:
            os.close(namespace_pin_fd)
        for container in reversed(created_containers):
            subprocess.run(("docker", "rm", "-f", container), capture_output=True, check=False, timeout=30)
        if enrollment_file.exists():
            enrollment_file.unlink()
        if test_route_file.exists():
            test_route_file.unlink()
        if private.exists():
            shutil.rmtree(private)
        for root, created in ((FIXTURE_ROOT, created_fixture_root), (APPROVAL_ROOT, created_approval_root)):
            if created and root.exists() and not any(root.iterdir()):
                root.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test-only host Docker enrollment component; never full containment acceptance")
    parser.add_argument("--confirm-test-only-fixture", action="store_true")
    parser.add_argument("--exercise-test-route-manifest", action="store_true", help="also simulate one test-only root route authority and separately read it back")
    parser.add_argument("--exercise-disposable-systemd", action="store_true", help="install, mask, verify, unmask and remove only uniquely named test systemd service/timer/socket; requires --exercise-test-route-manifest")
    parser.add_argument("--test-image", default=IMAGE_TAG, help="select an already-present local image tag or exact sha256 ID; never pull images")
    parser.add_argument("--require-reviewed-ordinary-source", action="store_true", help="require actual reviewed bootstrap image-source readback in both CLI processes")
    arguments = parser.parse_args()
    if not arguments.confirm_test_only_fixture:
        parser.error("explicit --confirm-test-only-fixture is required")
    run_fixture(exercise_test_route_manifest=arguments.exercise_test_route_manifest, exercise_disposable_systemd=arguments.exercise_disposable_systemd, test_image=arguments.test_image, require_reviewed_ordinary_source=arguments.require_reviewed_ordinary_source)


if __name__ == "__main__":
    main()
