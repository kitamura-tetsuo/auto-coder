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
from pathlib import Path

import pytest

from auto_coder.management.fresh_begin import begin_new_instance
from auto_coder.management.fresh_private_engine import (
    SOURCE_CREATED,
    SOURCE_INTENT,
    SOURCE_READY,
    create_private_engine,
    hold_private_engine_source,
)
from tests.test_management_new_instance_profile import configured_request

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTO_CODER_PRIVATE_ENGINE_ACCEPTANCE") != "1",
    reason="requires explicitly opted-in disposable runner Docker privileges",
)


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
        shutil.rmtree(root)
