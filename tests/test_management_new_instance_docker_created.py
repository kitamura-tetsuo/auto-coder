"""Real Docker CREATED-state evidence, never an original launch claim."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.test_management_fresh_host_integration import _test_image
from tests.test_management_new_instance_preflight import _bytes, _environment, request_file
from tests.test_management_new_instance_profile import configured_request

pytestmark = [pytest.mark.supervisor_integration, pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")]


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


@pytest.fixture(scope="module")
def isolated_image(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    tag, image_id = _test_image(tmp_path_factory.mktemp("new-docker-image"))
    yield tag, image_id
    subprocess.run(["docker", "image", "rm", tag], check=True, capture_output=True, text=True, timeout=30)


def _create(request_file: Path, image_id: str, *, variant: str = "") -> tuple[str, str]:
    request = json.loads(request_file.read_bytes())
    name = "ac-new-created-" + uuid.uuid4().hex[:18]
    command = [
        "docker",
        "create",
        "--name",
        name,
        "--network",
        "bridge" if variant == "network" else "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "0:0" if variant == "wrong-user" else f"{os.geteuid()}:{os.getegid()}",
        "--restart",
        "unless-stopped" if variant == "restart" else "no",
    ]
    if variant != "root-writable":
        command.append("--read-only")
    writable = ("state_root", "operation_root", "guard_root", "oauth_root")
    readonly = ("bootstrap_config", "management_config", "executable", "application_root")
    mounts = [(request[key], False) for key in writable] + [(request[key], True) for key in readonly] + [(str(request_file), True)]
    if variant == "missing-mount":
        mounts.pop()
    for path, ro in mounts:
        command.extend(["--mount", "type=bind,src=" + path + ",dst=" + path + (",readonly" if ro else "")])
    if variant == "extra-mount":
        command.extend(["--mount", "type=bind,src=" + str(request_file.parent) + ",dst=/unapproved,readonly"])
    command.extend(["--entrypoint", "/bin/sh", image_id, "-c", "true"])
    container_id = subprocess.check_output(command, text=True, timeout=40).strip()
    return container_id, name


def _inspect(request_file: Path, container_id: str, image_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", "inspect-new-container", "--request-file", str(request_file), "--container-id", container_id, "--image-id", image_id],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _begin(request_file: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", "begin", "--request-file", str(request_file)],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_real_docker_created_scope_produces_non_authorizing_snapshot(configured_request: Path, isolated_image: tuple[str, str]) -> None:
    root = configured_request.parent
    _begin(configured_request)
    _, image_id = isolated_image
    container_id, name = _create(configured_request, image_id)
    try:
        original = _bytes(root)
        result = _inspect(configured_request, container_id, image_id)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        assert value["status"] == "docker-created-snapshot-only"
        assert value["launch_authorized"] is False
        assert value["container_id"] == container_id
        assert value["image_id"] == image_id
        assert value["entrypoint"] == ["/bin/sh"]
        assert value["command"] == ["-c", "true"]
        assert value["origin_sha256"] == hashlib.sha256((root / "guard/new-instance-origin.json").read_bytes()).hexdigest()
        assert value["request_id"] == json.loads(configured_request.read_bytes())["request_id"]
        assert _bytes(root) == original
        assert list((root / "state").iterdir()) == []
        assert list((root / "operations").iterdir()) == []
        assert not (root / "LAUNCHED").exists()
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("variant", ["network", "root-writable", "wrong-user", "restart", "missing-mount", "extra-mount"])
def test_real_docker_created_scope_refuses_unsafe_configuration(configured_request: Path, isolated_image: tuple[str, str], variant: str) -> None:
    _begin(configured_request)
    _, image_id = isolated_image
    container_id, name = _create(configured_request, image_id, variant=variant)
    try:
        original = _bytes(configured_request.parent)
        result = _inspect(configured_request, container_id, image_id)
        assert result.returncode == 1
        assert result.stdout == ""
        assert "fresh host observation unavailable" in result.stderr
        assert _bytes(configured_request.parent) == original
        assert not (configured_request.parent / "LAUNCHED").exists()
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)


def test_real_docker_started_container_never_qualifies_as_created(configured_request: Path, isolated_image: tuple[str, str]) -> None:
    _begin(configured_request)
    _, image_id = isolated_image
    container_id, name = _create(configured_request, image_id)
    try:
        before = _bytes(configured_request.parent)
        assert _inspect(configured_request, container_id, image_id).returncode == 0
        subprocess.run(["docker", "start", "--attach", container_id], check=True, capture_output=True, timeout=30)
        result = _inspect(configured_request, container_id, image_id)
        assert result.returncode == 1
        assert "started, replaced or unavailable" in result.stderr
        assert _bytes(configured_request.parent) == before
        assert not (configured_request.parent / "LAUNCHED").exists()
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=True, capture_output=True, timeout=30)
