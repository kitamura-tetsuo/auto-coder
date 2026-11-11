"""Disposable runner acceptance for the trusted-host private Engine producer.

This creates one privileged Docker-in-Docker parent only when the dedicated
GitHub Actions job opts in. It never targets a production host or service.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from auto_coder.management.fresh_begin import begin_new_instance
from auto_coder.management.fresh_host_observation import HostObservationUnavailable
from auto_coder.management.fresh_private_engine import (
    SOURCE_CREATED,
    SOURCE_INTENT,
    SOURCE_READY,
    assert_private_engine_live,
    create_private_engine,
    hold_private_engine_source,
)
from tests.test_management_new_instance_first_launch_docker import (
    _authorized_readback,
    _create_real_new_container,
    _prepare_real_new_source,
    _run_host,
    _wait_for_new_outcome,
)
from tests.test_management_new_instance_preflight import request_file
from tests.test_management_new_instance_profile import configured_request

pytestmark = [
    pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep"),
    pytest.mark.skipif(
        os.environ.get("AUTO_CODER_PRIVATE_ENGINE_ACCEPTANCE") != "1",
        reason="requires explicitly opted-in disposable runner Docker privileges",
    ),
]


def _remove_disposable_engine_root(root: Path) -> None:
    """Remove only the test-created /tmp root after the DinD parent exits.

    Docker takes root ownership of its data-root contents. A GitHub-hosted
    runner can remove that disposable subtree with sudo after the parent is
    removed, but no production path or unchecked caller path is accepted.
    """
    if root.parent != Path("/tmp") or not root.name.startswith("acpe-") or not root.is_dir() or root.is_symlink():
        raise AssertionError("refusing to clean a non-disposable private Engine root")
    try:
        shutil.rmtree(root)
    except PermissionError:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise
        subprocess.run(["sudo", "--non-interactive", "rm", "--recursive", "--force", "--", str(root)], check=True, timeout=30)


def test_real_private_engine_parent_preserves_original_and_seals_independent_source(configured_request: Path) -> None:
    """Prove the exact parent Docker effect, socket custody, and live receipts."""
    original = begin_new_instance(configured_request)
    outer_socket = Path("/var/run/docker.sock")
    original_request = configured_request.read_bytes()
    outer = ["docker", "--host", f"unix://{outer_socket}"]
    image = subprocess.run(
        [*outer, "image", "inspect", "docker:29-dind", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    # Keep the real Unix Engine pathname safely below Linux sun_path limits.
    root = Path(tempfile.mkdtemp(prefix="acpe-", dir="/tmp"))
    parent_id: str | None = None
    try:
        evidence = create_private_engine(configured_request, root, outer_socket, image, timeout=90)
        parent_id = evidence.parent_container_id
        assert evidence.origin_sha256 == original.record_sha256
        assert evidence.parent_image_id == image
        assert evidence.outer_engine_id != evidence.private_engine_id
        assert evidence.socket_ino > 0
        assert configured_request.read_bytes() == original_request
        assert {entry.name for entry in root.iterdir()} == {
            "socket",
            "data",
            SOURCE_INTENT,
            SOURCE_CREATED,
            SOURCE_READY,
        }
        with hold_private_engine_source(configured_request, root, outer_socket) as observed:
            assert observed == evidence
        with pytest.raises(FileExistsError):
            create_private_engine(configured_request, root, outer_socket, image, timeout=10)
        assert json.loads((root / SOURCE_CREATED).read_bytes())["parent_container_id"] == parent_id
    finally:
        if parent_id is None and (root / SOURCE_CREATED).is_file():
            parent_id = json.loads((root / SOURCE_CREATED).read_bytes()).get("parent_container_id")
        if parent_id is not None:
            result = subprocess.run([*outer, "logs", "--tail", "60", parent_id], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print("Disposable DinD parent logs:\n", result.stdout[-4000:], result.stderr[-4000:])
            subprocess.run([*outer, "rm", "--force", parent_id], capture_output=True, text=True, timeout=30, check=True)
        _remove_disposable_engine_root(root)


def test_private_source_first_launch_and_retained_grant_readback(request_file: Path) -> None:
    """Cross the private source, host challenge, and real OAuth child boundary."""
    token, request = _prepare_real_new_source(request_file)
    original_oauth = (request_file.parent / "oauth/oauth.sqlite3").read_bytes()
    begin_new_instance(request_file)
    outer_socket = Path("/var/run/docker.sock")
    outer = ["docker", "--host", f"unix://{outer_socket}"]
    parent_image = subprocess.run(
        [*outer, "image", "inspect", "docker:29-dind", "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    root = Path(tempfile.mkdtemp(prefix="acpe-", dir="/tmp"))
    parent_id: str | None = None
    child_id = ""
    child_image_tag = ""
    selected_host = f"unix://{root / 'socket/docker.sock'}"
    original_docker_host = os.environ.get("DOCKER_HOST")
    try:
        source = create_private_engine(request_file, root, outer_socket, parent_image, timeout=90)
        parent_id = source.parent_container_id
        base_image = os.environ["AUTOCODER_FRESH_TEST_BASE_IMAGE"]
        save = subprocess.Popen([*outer, "image", "save", base_image], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert save.stdout is not None
        loaded = subprocess.run(
            ["docker", "--host", selected_host, "image", "load"],
            stdin=save.stdout,
            capture_output=True,
            text=True,
            timeout=120,
        )
        save.stdout.close()
        saved_stderr = save.stderr.read() if save.stderr is not None else b""
        assert save.wait(timeout=30) == 0, saved_stderr
        assert loaded.returncode == 0, loaded.stderr
        os.environ["DOCKER_HOST"] = selected_host

        child_id, image_id, child_image_tag = _create_real_new_container(request_file.parent, request_file, request)
        inspected = _run_host("inspect-new-container", "--request-file", str(request_file), "--container-id", child_id, "--image-id", image_id)
        assert inspected.returncode == 0, inspected.stderr
        arguments = (
            "start-new-container",
            "--request-file",
            str(request_file),
            "--container-id",
            child_id,
            "--image-id",
            image_id,
            "--private-root",
            str(root),
            "--outer-socket",
            str(outer_socket),
        )
        launch = _run_host(*arguments, timeout=150)
        assert launch.returncode == 0, (launch.stderr, subprocess.run(["docker", "logs", child_id], capture_output=True, text=True).stdout)
        assert json.loads(launch.stdout)["status"] == "first-child-observed-awaiting-authenticated-readback"
        claim = json.loads((request_file.parent / "guard/new-docker-first-start.json").read_bytes())
        assert claim["profile"] == "genuinely-new-private-engine-v1"
        assert claim["private_source"]["parent_container_id"] == source.parent_container_id
        assert claim["private_source"]["private_engine_id"] == source.private_engine_id
        assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
        replay = _run_host(*arguments, timeout=15)
        assert replay.returncode != 0 and replay.stdout == ""

        observation_id = json.loads((request_file.parent / "state/new-first-launched.json").read_bytes())["observation_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            readback = _authorized_readback(child_id, token, observation_id)
            if readback.returncode == 0:
                break
            time.sleep(0.3)
        else:
            raise AssertionError("NEW private source never produced fresh authorized readback: " + readback.stderr[-1200:])
        assert _wait_for_new_outcome(request_file.parent)["status"] == "succeeded"
        assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
        # A second CREATED child must poison this same source's live exclusion.
        # Never erase a prior child and then assert a fresh first-launch.
        extra = subprocess.run(
            ["docker", "--host", selected_host, "container", "create", "--entrypoint", "/bin/sh", image_id, "-c", "true"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        try:
            with pytest.raises(HostObservationUnavailable, match="another configured startup source"):
                assert_private_engine_live(request_file, root, outer_socket, source, container_id=child_id)
            assert len((request_file.parent / "state/physical-child-invocations").read_text().splitlines()) == 1
            assert (request_file.parent / "oauth/oauth.sqlite3").read_bytes() == original_oauth
        finally:
            subprocess.run(
                ["docker", "--host", selected_host, "container", "rm", extra],
                check=True,
                capture_output=True,
                timeout=30,
            )
    finally:
        if original_docker_host is None:
            os.environ.pop("DOCKER_HOST", None)
        else:
            os.environ["DOCKER_HOST"] = original_docker_host
        if child_id:
            subprocess.run(["docker", "--host", selected_host, "rm", "--force", child_id], capture_output=True, timeout=30)
        if child_image_tag:
            subprocess.run(["docker", "--host", selected_host, "image", "rm", child_image_tag], capture_output=True, timeout=30)
        if parent_id is None and (root / SOURCE_CREATED).is_file():
            parent_id = json.loads((root / SOURCE_CREATED).read_bytes()).get("parent_container_id")
        if parent_id is not None:
            logs = subprocess.run([*outer, "logs", "--tail", "80", parent_id], capture_output=True, text=True, timeout=10)
            if logs.returncode == 0:
                print("Disposable DinD parent logs:\n", logs.stdout[-4000:], logs.stderr[-4000:])
            subprocess.run([*outer, "rm", "--force", parent_id], capture_output=True, text=True, timeout=30, check=True)
        _remove_disposable_engine_root(root)
