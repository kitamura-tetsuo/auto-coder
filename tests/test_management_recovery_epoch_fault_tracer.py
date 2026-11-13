"""Native syscall barrier checks, separate from full VM issuer acceptance.

These tests trace the real durable-state component in independent processes.
The VM workflow separately traces the actual zero-argument host issuer with
real Docker/history/containment/READY and authenticated consumer custody.
"""

import errno
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.recovery_epoch_fault_tracer import BOUNDARY_SCENARIOS, PUBLICATIONS, KernelPublicationBarrier, parse_boundary

from auto_coder.management.recovery_epoch_state import EpochUnavailable, _hold_recovery_epoch
from tests.test_management_recovery_epoch_state import _inventory, _snapshot

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands():
    pass


@pytest.fixture
def _use_real_sleep():
    pass


_CHILD = """
import os
import sys
from pathlib import Path
from auto_coder.management.recovery_epoch_state import EpochBinding, _hold_recovery_epoch, issuer_lifetime
assert os.read(int(sys.argv[2]), 1) == b'1'
os.close(int(sys.argv[2]))
with _hold_recovery_epoch(Path(sys.argv[1]), expected_uid=os.geteuid()) as state:
    inventory = state.inventory
    boot, pid, start = issuer_lifetime()
    binding = EpochBinding(inventory.deployment_id, inventory.authorization_id, inventory.enrollment_ids, inventory.request_id, inventory.preparation_id, inventory.target_id, inventory.source_sha256, inventory.config_sha256, 'd' * 64, boot, pid, start)
    state.reserve(binding)
    state.record_intent(binding)
    state.consume(binding)
print('all-publications-completed', flush=True)
"""


def _start(process: subprocess.Popen, root: Path, name: str, when: str) -> KernelPublicationBarrier:
    barrier = KernelPublicationBarrier(process, root=root, name=name, when=when)
    try:
        barrier.start()
    except OSError as error:
        barrier.close()
        if error.errno in (errno.EPERM, errno.EACCES):
            pytest.skip("this environment prohibits actual ptrace; full native VM acceptance remains required")
        raise
    return barrier


@pytest.mark.parametrize("scenario", BOUNDARY_SCENARIOS)
def test_real_syscall_stop_preserves_exact_publication_prefix(tmp_path: Path, scenario: str) -> None:
    root, _ = _inventory(tmp_path)
    name, when = parse_boundary(scenario)
    reader, writer = os.pipe()
    process = subprocess.Popen([sys.executable, "-B", "-c", _CHILD, str(root), str(reader)], pass_fds=(reader,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(reader)
    barrier = None
    try:
        barrier = _start(process, root, name, when)
        os.write(writer, b"1")
        barrier.resume_initial()
        observation = barrier.wait(timeout=20)
        assert observation.process_id == process.pid and observation.name == name and observation.when == when
        count = PUBLICATIONS.index(name) + (when == "after")
        assert {path.name for path in root.iterdir()} == {"epoch.lock", "inventory.json", *PUBLICATIONS[:count]}
        assert observation.file_fsync_observed == (when == "after")
        assert observation.directory_fsync_observed == (when == "after")
        assert (observation.file_sha256 is not None) == (when == "after")
        retained = _snapshot(root)
        barrier.kill()
        assert process.returncode == -signal.SIGKILL
        assert _snapshot(root) == retained
        if count:
            with pytest.raises(EpochUnavailable, match="existing, partial or conflicting"):
                with _hold_recovery_epoch(root, expected_uid=os.geteuid()):
                    pytest.fail("a published original record was reused")
            assert _snapshot(root) == retained
    finally:
        os.close(writer)
        if barrier is not None:
            barrier.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


def test_releasing_actual_syscall_barrier_continues_original_production_code(tmp_path: Path) -> None:
    root, _ = _inventory(tmp_path)
    reader, writer = os.pipe()
    process = subprocess.Popen([sys.executable, "-B", "-c", _CHILD, str(root), str(reader)], pass_fds=(reader,), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(reader)
    barrier = None
    try:
        barrier = _start(process, root, "intent-head.json", "after")
        os.write(writer, b"1")
        barrier.resume_initial()
        barrier.wait(timeout=20)
        assert not (root / "consumed.json").exists()
        barrier.release()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert "all-publications-completed" in stdout
        assert {path.name for path in root.iterdir()} == {"epoch.lock", "inventory.json", *PUBLICATIONS}
    finally:
        os.close(writer)
        if barrier is not None:
            barrier.close()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)


@pytest.mark.parametrize("scenario", ["", "before", "after-inventory", "before-../reserved", "before-reserved.json", "before-reserved-head-extra", "after-consumed-root"])
def test_boundary_selector_cannot_choose_unreviewed_paths(scenario: str) -> None:
    with pytest.raises(ValueError, match="reviewed publication"):
        parse_boundary(scenario)


def test_all_reviewed_record_and_head_boundaries_are_explicit() -> None:
    assert len(BOUNDARY_SCENARIOS) == 16 and len(set(BOUNDARY_SCENARIOS)) == 16
    assert {parse_boundary(item) for item in BOUNDARY_SCENARIOS} == {(name, when) for name in PUBLICATIONS for when in ("before", "after")}
