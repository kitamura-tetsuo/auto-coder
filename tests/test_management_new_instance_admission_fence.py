"""Real Linux processes contend on the actual non-authorizing NEW guard fence."""

import hashlib
import json
import select
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_management_new_instance_preflight import _bytes, _environment, request_file
from tests.test_management_new_instance_profile import configured_request

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


def _probe(request: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", "probe-admission-fence", "--request-file", str(request)],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _begin(request: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-m", "auto_coder.management.fresh_host", "begin", "--request-file", str(request)],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


_HOLD = """
import sys
from pathlib import Path
from auto_coder.management.fresh_new_admission_fence import hold_new_admission_fence
with hold_new_admission_fence(Path(sys.argv[1])) as observation:
    print('REAL_FENCE_HELD', flush=True)
    sys.stdin.readline()
    print(observation.status, flush=True)
"""


def _holder(request: Path, *, script: str = _HOLD) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(request)],
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 20)[0], "owner did not hold the real guard inode"
    assert process.stdout.readline() == "REAL_FENCE_HELD\n"
    assert process.poll() is None
    return process


def test_real_host_admission_fence_probe_is_non_authorizing(configured_request: Path) -> None:
    root = configured_request.parent
    _begin(configured_request)
    old_state = _bytes(root)
    result = _probe(configured_request)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {
        "request_id": json.loads(configured_request.read_bytes())["request_id"],
        "origin_sha256": hashlib.sha256((root / "guard/new-instance-origin.json").read_bytes()).hexdigest(),
        "guard_device": (root / "guard").stat().st_dev,
        "guard_inode": (root / "guard").stat().st_ino,
        "status": "admission-fence-observed-only",
        "launch_authorized": False,
    }
    assert _bytes(root) == old_state
    assert list((root / "state").iterdir()) == []
    assert list((root / "operations").iterdir()) == []
    assert not (root / "LAUNCHED").exists()


def test_actual_overlapping_guard_flock_refuses_second_owner(configured_request: Path) -> None:
    root = configured_request.parent
    _begin(configured_request)
    initial = _bytes(root)
    first = _holder(configured_request)
    try:
        blocked = _probe(configured_request)
        assert blocked.returncode == 1
        assert blocked.stdout == ""
        assert "Resource temporarily unavailable" in blocked.stderr
        assert first.poll() is None
        assert _bytes(root) == initial
        assert first.stdin is not None
        first.stdin.write("resume\n")
        first.stdin.flush()
        stdout, stderr = first.communicate(timeout=10)
        assert first.returncode == 0, stderr
        assert stdout == "admission-fence-observed-only\n"
        after = _probe(configured_request)
        assert after.returncode == 0, after.stderr
        assert json.loads(after.stdout)["launch_authorized"] is False
        assert _bytes(root) == initial
        assert not (root / "LAUNCHED").exists()
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate(timeout=5)


def test_killed_holder_releases_only_lock_not_a_first_launch_claim(configured_request: Path) -> None:
    root = configured_request.parent
    _begin(configured_request)
    original = _bytes(root)
    holder = _holder(configured_request)
    holder.kill()
    holder.communicate(timeout=10)
    assert holder.returncode == -9
    checked = _probe(configured_request)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["launch_authorized"] is False
    assert _bytes(root) == original
    assert not (root / "state/fresh-launch-claimed.json").exists()
    assert not (root / "LAUNCHED").exists()


@pytest.mark.parametrize("fault", ["profile", "record", "guard-mode", "state-replaced"])
def test_fence_probe_refuses_changed_original_evidence(configured_request: Path, fault: str) -> None:
    root = configured_request.parent
    _begin(configured_request)
    if fault == "profile":
        (root / "management.yaml").write_bytes(b"changed management configuration")
    elif fault == "record":
        (root / "guard/new-instance-origin.json").write_bytes(b"invalid record")
    elif fault == "guard-mode":
        (root / "guard").chmod(0o750)
    else:
        (root / "state").rename(root / "original-state")
        (root / "state").mkdir(mode=0o700)
    before = _bytes(root)
    result = _probe(configured_request)
    assert result.returncode == 1
    assert result.stdout == ""
    assert _bytes(root) == before
    assert not (root / "LAUNCHED").exists()


def test_guard_path_replaced_during_held_fence_refuses_exit(configured_request: Path) -> None:
    """A held flock cannot vouch for a newly mounted or renamed path."""
    root = configured_request.parent
    _begin(configured_request)
    original_receipt = (root / "guard/new-instance-origin.json").read_bytes()
    holder = _holder(configured_request)
    detached = root / "detached-guard"
    try:
        (root / "guard").rename(detached)
        (root / "guard").mkdir(mode=0o700)
        assert holder.stdin is not None
        holder.stdin.write("resume\n")
        holder.stdin.flush()
        _, stderr = holder.communicate(timeout=20)
        assert holder.returncode != 0, "stale held guard descriptor was silently accepted"
        assert "guard" in stderr.lower() or "origin" in stderr.lower()
        assert (detached / "new-instance-origin.json").read_bytes() == original_receipt
        assert not (root / "LAUNCHED").exists()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=5)
        (root / "guard").rmdir()
        detached.rename(root / "guard")


_HOLD_POSTCHECK = """
import sys
from pathlib import Path
from auto_coder.management.fresh_new_admission_fence import hold_new_admission_fence
with hold_new_admission_fence(Path(sys.argv[1])) as observation:
    print('REAL_FENCE_HELD', flush=True)
    sys.stdin.readline()
print(observation.status, flush=True)
"""


def test_changed_profile_during_held_fence_cannot_emit_success(configured_request: Path) -> None:
    """An original configuration change invalidates the post-hold observation."""
    root = configured_request.parent
    _begin(configured_request)
    holder = _holder(configured_request, script=_HOLD_POSTCHECK)
    try:
        (root / "management.yaml").write_bytes(b"changed while original guard was locked")
        assert holder.stdin is not None
        holder.stdin.write("resume\n")
        holder.stdin.flush()
        stdout, stderr = holder.communicate(timeout=20)
        assert holder.returncode != 0
        assert stdout == ""
        assert "changed" in stderr or "differs" in stderr
        assert not (root / "LAUNCHED").exists()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=5)
