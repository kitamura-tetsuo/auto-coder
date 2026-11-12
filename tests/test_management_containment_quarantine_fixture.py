"""Real Unix guard transport regressions; these do not claim host acceptance."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts import containment_quarantine_fixture as quarantine_helper

from auto_coder.management.containment_kernel_subobservation import _holder_start_token

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def _request_process(socket_path: Path, granted: Path, request: dict[str, str]) -> subprocess.Popen:
    code = """import json,sys
from pathlib import Path
from auto_coder.management.fresh_initialization import request_fresh_host_grant
request_fresh_host_grant(Path(sys.argv[1]),**json.loads(sys.argv[2]))
Path(sys.argv[3]).write_text('unexpected grant')
"""
    return subprocess.Popen((sys.executable, "-c", code, str(socket_path), json.dumps(request), str(granted)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _request() -> dict[str, str]:
    return {"request_id": str(uuid.uuid4()), "intent_sha256": "a" * 64, "owner_incarnation": str(uuid.uuid4()), "new_container_id": "b" * 64}


def test_real_guard_holds_request_and_never_grants(tmp_path):
    path, granted = tmp_path / "fresh-guard.sock", tmp_path / "unexpected-grant"
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(path, request_id=expected["request_id"], intent_sha256=expected["intent_sha256"], container_id=expected["new_container_id"])
    process = _request_process(path, granted, expected)
    try:
        request = guard.receive_and_pause(process.pid, expected_start_token=_holder_start_token(process.pid), timeout=10)
        assert request.owner_incarnation == expected["owner_incarnation"]
        assert request.intent_sha256 == expected["intent_sha256"]
        assert len(request.challenge) == 64
        assert Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T"
        assert not granted.exists()
        guard.close()
        process.send_signal(signal.SIGCONT)
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0
        assert "fresh host grant is incomplete" in error
        assert not granted.exists()
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        guard.close()


def test_real_guard_rejects_another_intent_before_suspension(tmp_path):
    path, granted = tmp_path / "fresh-guard.sock", tmp_path / "unexpected-grant"
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(path, request_id=expected["request_id"], intent_sha256="c" * 64, container_id=expected["new_container_id"])
    process = _request_process(path, granted, expected)
    try:
        with pytest.raises(ValueError, match="sealed intent"):
            guard.receive_and_pause(process.pid, expected_start_token=_holder_start_token(process.pid), timeout=10)
        assert Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T"
        guard.close()
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0
        assert "fresh host grant is incomplete" in error
        assert not granted.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        guard.close()


def test_real_guard_refuses_changed_peer_lifetime(tmp_path):
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(tmp_path / "fresh-guard.sock", request_id=expected["request_id"], intent_sha256=expected["intent_sha256"], container_id=expected["new_container_id"])
    try:
        with pytest.raises(ValueError, match="lifetime changed"):
            guard.receive_and_pause(os.getpid(), expected_start_token="0", timeout=0.1)
        assert guard.request is None
    finally:
        guard.close()
