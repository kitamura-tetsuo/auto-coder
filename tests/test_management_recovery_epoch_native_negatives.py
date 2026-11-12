"""Real-file/process safety guards for the native negative fixture helper.

These component checks do not claim Docker/systemd or complete issuer acceptance.
The separately authorized VM runs every production-issuer and report-CLI case.
"""

from __future__ import annotations

import fcntl
import os
import signal
import stat
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest
from scripts import recovery_epoch_native_negatives as fixture
from scripts.test_management_containment_enrollment_host import _write_private

from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.containment_kernel_subobservation import _holder_start_token
from auto_coder.management.recovery_containment_model import RecoveryLock
from tests.test_management_recovery_containment import _enrollment

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands():
    yield


@pytest.fixture
def _use_real_sleep():
    yield


def _context(tmp_path: Path) -> fixture.NativeNegativeContext:
    epoch = tmp_path / "epoch"
    epoch.mkdir(mode=0o700)
    _write_private(epoch / "epoch.lock", b"")
    _write_private(epoch / "inventory.json", b'{"component":"no-authority"}')
    return fixture.NativeNegativeContext(_enrollment(), tmp_path, epoch)


def test_unreserved_guard_observes_but_never_rewrites_original_inputs(tmp_path: Path) -> None:
    context = _context(tmp_path)
    before = {path.name: fixture._projection(path) for path in context.epoch_root.iterdir()}
    assert fixture._epoch_snapshot(context.epoch_root) == fixture._epoch_snapshot(context.epoch_root)
    fixture.assert_unreserved_epoch(context.epoch_root)
    assert {path.name: fixture._projection(path) for path in context.epoch_root.iterdir()} == before


@pytest.mark.parametrize("name", ["reserved.json", "reserved-head.json", "intent.json", "intent-head.json", "consumed.json", "consumed-head.json", "receipt.json", "receipt-head.json", ".partial", "unexpected.json"])
def test_any_original_publication_or_partial_name_permanently_aborts_fixture(tmp_path: Path, name: str) -> None:
    context = _context(tmp_path)
    path = context.epoch_root / name
    _write_private(path, b"uncertain original bytes")
    before = {item.name: fixture._projection(item) for item in context.epoch_root.iterdir()}
    with pytest.raises(fixture.EpochAlreadyPublished, match="never repair or retry"):
        fixture.assert_unreserved_epoch(context.epoch_root)
    assert {item.name: fixture._projection(item) for item in context.epoch_root.iterdir()} == before


@pytest.mark.parametrize("change", ["symlink", "hardlink", "file-mode", "directory-mode", "missing-input"])
def test_unreserved_guard_requires_bounded_original_objects(tmp_path: Path, change: str) -> None:
    context = _context(tmp_path)
    lock = context.epoch_root / "epoch.lock"
    if change == "symlink":
        lock.unlink()
        lock.symlink_to(context.epoch_root / "inventory.json")
    elif change == "hardlink":
        os.link(lock, tmp_path / "alias")
    elif change == "file-mode":
        lock.chmod(0o640)
    elif change == "directory-mode":
        context.epoch_root.chmod(0o750)
    else:
        lock.unlink()
    with pytest.raises(fixture.EpochAlreadyPublished):
        fixture.assert_unreserved_epoch(context.epoch_root)


def test_epoch_inode_injection_restores_the_same_original_only_before_publication(tmp_path: Path) -> None:
    context = _context(tmp_path)
    original = context.epoch_root / "epoch.lock"
    before = fixture._projection(original)
    backup = tmp_path / "epoch.original"
    inventory = fixture._projection(context.epoch_root / "inventory.json")
    with fixture._replacement(context.epoch_root, original, backup, b""):
        assert fixture._projection(original)[:2] != before[:2]
        assert fixture._projection(backup) == before
        fixture.assert_unreserved_epoch(context.epoch_root)
    assert fixture._projection(original) == before
    assert fixture._projection(context.epoch_root / "inventory.json") == inventory
    assert not backup.exists()


def test_publication_during_epoch_injection_keeps_every_original_and_never_resets(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path, backup = context.epoch_root / "epoch.lock", tmp_path / "epoch.original"
    original = fixture._projection(path)
    with pytest.raises(fixture.EpochAlreadyPublished, match="never repair or retry"):
        with fixture._replacement(context.epoch_root, path, backup, b""):
            injected = fixture._projection(path)
            _write_private(context.epoch_root / "reserved.json", b"original partial record")
    assert fixture._projection(path) == injected
    assert fixture._projection(backup) == original
    assert (context.epoch_root / "reserved.json").read_bytes() == b"original partial record"
    assert {item.name for item in context.epoch_root.iterdir()} == {"epoch.lock", "inventory.json", "reserved.json"}


def test_modified_injection_is_retained_instead_of_destructively_restored(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path, backup = tmp_path / "owner", tmp_path / "owner.original"
    _write_private(path, b"original owner")
    original = fixture._projection(path)
    with pytest.raises(AssertionError, match="replacement changed"):
        with fixture._replacement(context.epoch_root, path, backup, b"injected owner"):
            path.write_bytes(b"unexpected issuer mutation")
    assert path.read_bytes() == b"unexpected issuer mutation"
    assert fixture._projection(backup) == original


def test_each_refusal_compares_injected_and_restored_original_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    original, backup = tmp_path / "owner", tmp_path / "owner.original"
    _write_private(original, b"native original")
    before = fixture._projection(original)
    calls = []

    def refusal(label: str) -> None:
        assert fixture._projection(original)[:2] != before[:2]
        calls.append(label)

    fixture._run_case(context, "component-refusal", lambda: fixture._replacement(context.epoch_root, original, backup, b"native original"), refusal, lambda: fixture._projection(original))
    assert calls == ["component-refusal"]
    assert fixture._projection(original) == before


def test_refused_issuer_cannot_modify_unrelated_original_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    path = tmp_path / "original-operation-store"
    _write_private(path, b"original")

    def invalid_refusal(_: str) -> None:
        path.write_bytes(b"unexpected change")

    with pytest.raises(AssertionError, match="changed original/OAuth/worker"):
        fixture._run_case(context, "component-refusal", nullcontext, invalid_refusal, lambda: fixture._projection(path))
    assert path.read_bytes() == b"unexpected change"


def test_refused_issuer_cannot_modify_epoch_inventory_without_a_record(tmp_path: Path) -> None:
    context = _context(tmp_path)

    def invalid_refusal(_: str) -> None:
        (context.epoch_root / "inventory.json").write_bytes(b"unexpected replacement configuration")

    with pytest.raises(AssertionError, match="changed an original epoch input"):
        fixture._run_case(context, "component-refusal", nullcontext, invalid_refusal, lambda: "unchanged original state")
    assert (context.epoch_root / "inventory.json").read_bytes() == b"unexpected replacement configuration"


def test_actual_ex_contender_prevents_independent_shared_fence_and_releases_only_itself(tmp_path: Path) -> None:
    path = tmp_path / "original.lock"
    _write_private(path, b"")
    info = path.stat()
    selected = RecoveryLock(str(path), info.st_dev, info.st_ino)
    code = "import fcntl,sys;f=open(sys.argv[1],'rb');\ntry: fcntl.flock(f,fcntl.LOCK_SH|fcntl.LOCK_NB)\nexcept BlockingIOError: sys.exit(7)\n"
    with fixture._busy_fence(selected):
        assert subprocess.run((sys.executable, "-I", "-B", "-c", code, str(path)), timeout=10).returncode == 7
    assert subprocess.run((sys.executable, "-I", "-B", "-c", code, str(path)), timeout=10).returncode == 0
    assert path.stat().st_ino == selected.inode and path.read_bytes() == b""


def test_actual_busy_fence_does_not_accept_a_different_selected_inode(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    _write_private(path, b"")
    info = path.stat()
    with pytest.raises(AssertionError, match="selected original"):
        with fixture._busy_fence(RecoveryLock(str(path), info.st_dev, info.st_ino + 1)):
            pytest.fail("a different original identity was accepted")
    with path.open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_real_process_barrier_and_pidfd_cleanup_leave_no_running_fixture_child() -> None:
    code = "import sys;print('READY',flush=True);sys.stdin.buffer.read(1)"
    with fixture._live_process((sys.executable, "-I", "-B", "-c", code)) as process:
        pid = process.pid
        assert process.poll() is None and Path(f"/proc/{pid}").exists()
    assert process.returncode is not None
    assert not Path(f"/proc/{pid}").exists()


def test_malformed_real_process_readiness_is_a_failure() -> None:
    with pytest.raises(AssertionError, match="exact readiness"):
        with fixture._live_process((sys.executable, "-I", "-B", "-c", "print('UNAVAILABLE',flush=True)")):
            pytest.fail("an invalid native barrier was accepted")


_RESOURCE_HOLDER = """
import fcntl
import os
import sys
owner = open(sys.argv[1], 'rb')
fcntl.flock(owner, fcntl.LOCK_SH | fcntl.LOCK_NB)
controller = open(sys.argv[2], 'rb')
fcntl.flock(controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
namespace = os.open('/proc/self/ns/net', os.O_RDONLY)
extra = open(sys.argv[1], 'rb') if sys.argv[3] == 'duplicate' else None
print('READY', flush=True)
sys.stdin.buffer.read(1)
"""


@pytest.mark.parametrize("duplicate", [False, True])
def test_report_probe_discovers_actual_stopped_process_descriptors_without_inventing_fences(tmp_path: Path, duplicate: bool) -> None:
    owner, controller = tmp_path / "owner.lock", tmp_path / "controller.lock"
    for path in (owner, controller):
        _write_private(path, b"")
    namespace = os.open("/proc/self/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        ns = os.fstat(namespace)
        pin = OriginalNetworkPin(os.getpid(), _holder_start_token(os.getpid()), Path("/proc/sys/kernel/random/boot_id").read_text().strip(), namespace, os.readlink(f"/proc/self/fd/{namespace}"), ns.st_dev, ns.st_ino)
        selected = _enrollment()
        original = replace(selected.originals[0], owner=RecoveryLock(str(owner), owner.stat().st_dev, owner.stat().st_ino), namespace_pin=pin)
        watcher = replace(selected.watchers[0], lock=RecoveryLock(str(controller), controller.stat().st_dev, controller.stat().st_ino))
        selected = replace(selected, originals=(original,), watchers=(watcher,))
        with fixture._live_process((sys.executable, "-I", "-B", "-c", _RESOURCE_HOLDER, str(owner), str(controller), "duplicate" if duplicate else "one")) as process:
            with pytest.raises(AssertionError, match="SIGSTOP"):
                fixture._issuer_resources(selected, process.pid)
            process.send_signal(signal.SIGSTOP)
            waited, status = os.waitpid(process.pid, os.WUNTRACED)
            assert waited == process.pid and os.WIFSTOPPED(status)
            try:
                if duplicate:
                    with pytest.raises(AssertionError, match="absent or ambiguous"):
                        fixture._issuer_resources(selected, process.pid)
                else:
                    actual = fixture._issuer_resources(selected, process.pid)
                    assert actual.holder_pid == process.pid and actual.holder_start_token == _holder_start_token(process.pid)
                    for values, expected in ((actual.original_owner_fds, owner), (actual.controller_fds, controller)):
                        assert len(values) == 1
                        entry = Path(f"/proc/{process.pid}/fd/{values[0].descriptor}")
                        assert (entry.stat().st_dev, entry.stat().st_ino) == (expected.stat().st_dev, expected.stat().st_ino)
                    assert len(actual.namespace_fds) == 1
                    assert os.readlink(f"/proc/{process.pid}/fd/{actual.namespace_fds[0].descriptor}") == pin.namespace
            finally:
                process.send_signal(signal.SIGCONT)
    finally:
        os.close(namespace)


@pytest.mark.parametrize("action", ["negatives", "reports"])
def test_native_entry_refuses_unapproved_roots_before_any_fixture_effect(tmp_path: Path, action: str) -> None:
    context = _context(tmp_path)
    before = fixture._epoch_snapshot(context.epoch_root)
    with pytest.raises(PermissionError, match="fixed disposable fixture roots"):
        if action == "negatives":
            fixture.run_native_negatives(context, lambda _: pytest.fail("an unauthorized issuer was started"), lambda: pytest.fail("unauthorized fixture state was sampled"))
        else:
            fixture.verify_native_containment_reports(context, os.getpid())
    assert fixture._epoch_snapshot(context.epoch_root) == before
    assert {path.name for path in tmp_path.iterdir()} == {"epoch"}


def test_native_case_oracles_distinguish_actual_issuer_and_separate_report_work() -> None:
    assert len(fixture.REQUIRED_NEGATIVE_CASES) == 9
    assert len(fixture.REQUIRED_REPORT_CASES) == 4
    assert fixture.REQUIRED_NEGATIVE_CASES.isdisjoint(fixture.REQUIRED_REPORT_CASES)
    assert {"busy-original-owner-refused", "busy-controller-fence-refused", "replaced-epoch-lock-refused", "retained-old-network-task-and-listener-refused", "old-published-listener-refused"} <= fixture.REQUIRED_NEGATIVE_CASES
    assert {"forged-native-containment-refused", "cross-deployment-containment-refused", "stale-native-containment-refused"} <= fixture.REQUIRED_REPORT_CASES
    assert stat.S_IMODE(Path(fixture.__file__).stat().st_mode) & 0o111 == 0
