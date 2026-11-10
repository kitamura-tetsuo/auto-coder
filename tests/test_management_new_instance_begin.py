"""Real subprocess/filesystem tests for a durable non-authorizing NEW-origin record."""

import hashlib
import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_management_new_instance_preflight import _bytes, _environment, request_file

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


def _command(operation: str, request: Path) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "auto_coder.management.fresh_host",
        operation,
        "--request-file",
        str(request),
    ]


def _run(operation: str, request: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(operation, request),
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_real_host_begin_and_separate_process_verifier_preserve_oauth(request_file: Path) -> None:
    root = request_file.parent
    original = _bytes(root)
    result = _run("begin", request_file)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "origin-recorded-only"
    assert receipt["launch_authorized"] is False
    assert receipt["request_id"] == json.loads(original["request.json"])["request_id"]
    record = root / "guard/new-instance-origin.json"
    assert receipt["record_path"] == str(record)
    assert receipt["record_sha256"] == hashlib.sha256(record.read_bytes()).hexdigest()
    assert record.stat().st_mode & 0o777 == 0o600
    assert json.loads(record.read_bytes())["producer_lifetime"]["pid"] > 0

    verifier = _run("inspect-begin", request_file)
    assert verifier.returncode == 0, verifier.stderr
    assert json.loads(verifier.stdout) == receipt
    recorded = _bytes(root)
    assert {path: content for path, content in recorded.items() if not path.startswith("guard/")} == original
    assert all(list((root / name).iterdir()) == [] for name in ("state", "operations"))
    assert not (root / "LAUNCHED").exists()

    replay = _run("begin", request_file)
    assert replay.returncode == 1
    assert "existing or partial state" in replay.stderr
    assert _run("preflight", request_file).returncode == 1
    assert _bytes(root) == recorded


@pytest.mark.parametrize("mutation", ["request", "application", "mode", "record", "extra"])
def test_independent_verifier_rejects_stale_or_conflicting_origin(request_file: Path, mutation: str) -> None:
    assert _run("begin", request_file).returncode == 0
    root = request_file.parent
    record = root / "guard/new-instance-origin.json"
    if mutation == "request":
        request_file.write_bytes(request_file.read_bytes() + b" ")
    elif mutation == "application":
        (root / "app/main.py").write_text("changed\n")
    elif mutation == "mode":
        record.chmod(0o644)
    elif mutation == "record":
        record.write_bytes(record.read_bytes().replace(b"new-instance-origin-only", b"recovery-origin-forged"))
    else:
        (root / "guard/extra-record.json").write_text("conflicting")
    changed = _bytes(root)
    result = _run("inspect-begin", request_file)
    assert result.returncode == 1
    assert result.stdout == ""
    assert _run("begin", request_file).returncode == 1
    assert _bytes(root) == changed
    assert not (root / "LAUNCHED").exists()


_BARRIER = """
import os
import runpy
import sys
real_open = os.open
phase = sys.argv[1]
request = sys.argv[2]

def hold_original_open(path, flags, mode=0o777, **kwargs):
    if path == 'new-instance-origin.json' and flags & os.O_EXCL:
        if phase == 'before':
            print('ORIGIN_BARRIER', flush=True)
            sys.stdin.readline()
        descriptor = real_open(path, flags, mode, **kwargs)
        if phase == 'after':
            print('ORIGIN_BARRIER', flush=True)
            sys.stdin.readline()
        return descriptor
    return real_open(path, flags, mode, **kwargs)

os.open = hold_original_open
sys.argv = ['fresh_host', 'begin', '--request-file', request]
runpy.run_module('auto_coder.management.fresh_host', run_name='__main__')
"""


def _paused(request_file: Path, phase: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", _BARRIER, phase, str(request_file)],
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 15)[0], "origin writer did not reach actual filesystem barrier"
    assert process.stdout.readline() == "ORIGIN_BARRIER\n"
    return process


def test_two_real_writers_at_one_use_origin_boundary_allow_one_only(request_file: Path) -> None:
    root = request_file.parent
    first = _paused(request_file, "before")
    second = _paused(request_file, "before")
    try:
        assert first.poll() is None and second.poll() is None
        assert first.stdin and second.stdin
        first.stdin.write("go\n")
        second.stdin.write("go\n")
        first.stdin.flush()
        second.stdin.flush()
        out_a, err_a = first.communicate(timeout=15)
        out_b, err_b = second.communicate(timeout=15)
        assert sorted((first.returncode, second.returncode)) == [0, 1], (out_a, err_a, out_b, err_b)
        assert sum("origin-recorded-only" in output for output in (out_a, out_b)) == 1
        assert _run("inspect-begin", request_file).returncode == 0
        assert not (root / "LAUNCHED").exists()
        assert all(list((root / name).iterdir()) == [] for name in ("state", "operations"))
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_real_writer_killed_after_one_use_open_poisoned_replay(request_file: Path) -> None:
    root = request_file.parent
    original = _bytes(root)
    child = _paused(request_file, "after")
    try:
        assert child.poll() is None
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode == -9
        recorded = root / "guard/new-instance-origin.json"
        assert recorded.exists()
        assert recorded.read_bytes() == b""
        after = _bytes(root)
        assert _run("begin", request_file).returncode == 1
        assert _run("inspect-begin", request_file).returncode == 1
        assert _bytes(root) == after
        assert all(after[key] == value for key, value in original.items())
        assert not (root / "LAUNCHED").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
