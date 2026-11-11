"""Exercise protected enrollment readback on a disposable real Linux/Docker fixture.

This manually invoked host probe never authorizes full containment. It installs
only a uniquely identified test-only approval component, verifies it in two
separate subprocesses, and removes only resources it created.
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
from pathlib import Path

from loguru import logger

from auto_coder.management.containment_enrollment import ApprovedEnrollment, OriginalFile
from auto_coder.management.fresh_host import _write_evidence
from auto_coder.management.fresh_host_observation import HostObservationUnavailable, PriorContainer, capture_prior_container

APPROVAL_ROOT = Path("/var/lib/auto-coder/approved-containment-enrollments")
FIXTURE_ROOT = Path("/var/lib/auto-coder/containment-readback-fixtures")
IMAGE_TAG = "auto-coder:management-local-abc65544"
INSTALLED_BOOTSTRAP = "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/management/bootstrap_supervisor.py"


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


def _assert_component(report: dict[str, object], *, present: bool) -> None:
    if (report.get("approved_enrollment") is not None) != present:
        raise AssertionError("protected enrollment component state differs")
    authorities = _authorities(report)
    if ("approved-enrollment-component" in authorities) == present:
        raise AssertionError("protected enrollment refusal component differs")
    if not {"deployment-enrollment", "ordinary-route-closure", "original-source-profile", "kernel-containment"} <= authorities:
        raise AssertionError("test-only enrollment removed normative containment refusals")


def run_fixture() -> None:
    if os.geteuid() != 0 or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
        raise PermissionError("host root is required for this isolated fixture")
    image = _docker("image", "inspect", IMAGE_TAG, "--format", "{{.Id}}")
    if not image.startswith("sha256:") or len(image) != 71:
        raise ValueError("preinstalled test image identity is unavailable")

    created_approval_root = _private_directory(APPROVAL_ROOT)
    created_fixture_root = _private_directory(FIXTURE_ROOT)
    fixture_id = "ac-readback-test-" + uuid.uuid4().hex
    private = FIXTURE_ROOT / fixture_id
    private.mkdir(mode=0o700)
    enrollment_file = APPROVAL_ROOT / (fixture_id + ".json")
    manager_name = fixture_id + "-manager"
    proxy_name = fixture_id + "-proxy"
    created_containers: list[str] = []
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
        proxy = _docker("run", "-d", "--name", proxy_name, "--network", "container:" + manager, "--user", "0:0", "--entrypoint", "/bin/sleep", image, "120")
        if not re.fullmatch(r"[0-9a-f]{64}", proxy):
            raise AssertionError("fixture Docker returned an ambiguous proxy identity")
        created_containers.append(proxy)
        prior_proxy = capture_prior_container(proxy, image)
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
            "approved-current-containment-enrollment-v1",
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
        )
        _write_private(enrollment_file, json.dumps(asdict(approved), sort_keys=True).encode())
        scope = private / "scope.json"
        _write_private(
            scope, json.dumps({"deployment_id": approved.deployment_id, "enrollment_id": approved.enrollment_id, "authorization_id": approved.authorization_id, "profile": approved.profile, "management_id": approved.management_id, "proxy_id": approved.proxy_id, "files": []}, sort_keys=True).encode()
        )
        first = _call_cli("observe", scope)
        _assert_component(first, present=True)
        previous = private / "previous.json"
        _write_private(previous, json.dumps(first, sort_keys=True).encode())
        verified = _call_cli("verify", scope, previous)
        _assert_component(verified, present=True)
        logger.info("Real stopped Docker manager/proxy and separately invoked verifier observed the protected test-only enrollment component")
        changed = private / "proxy-config"
        changed.write_bytes(b"substituted fixture proxy configuration\n")
        refused = _call_cli("verify", scope, previous)
        _assert_component(refused, present=False)
        if "saved-report:approved_enrollment" not in _authorities(refused):
            raise AssertionError("separate consumer did not reject saved approval after source mutation")
        logger.info("Source substitution refused after producer exit; no full containment verdict was issued")
    finally:
        for container in reversed(created_containers):
            subprocess.run(("docker", "rm", "-f", container), capture_output=True, check=False, timeout=30)
        if enrollment_file.exists():
            enrollment_file.unlink()
        if private.exists():
            shutil.rmtree(private)
        for root, created in ((FIXTURE_ROOT, created_fixture_root), (APPROVAL_ROOT, created_approval_root)):
            if created and root.exists() and not any(root.iterdir()):
                root.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description="Test-only host Docker enrollment component; never full containment acceptance")
    parser.add_argument("--confirm-test-only-fixture", action="store_true")
    arguments = parser.parse_args()
    if not arguments.confirm_test_only_fixture:
        parser.error("explicit --confirm-test-only-fixture is required")
    run_fixture()


if __name__ == "__main__":
    main()
