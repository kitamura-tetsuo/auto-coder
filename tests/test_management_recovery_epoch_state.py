"""Native persistence tests, separate from complete host-issuer acceptance.

Inventories here are isolated state fixtures. They do not establish historical
completeness, independent containment or authorization to launch a supervisor.
"""

import fcntl
import hashlib
import inspect
import json
import os
import select
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management import recovery_epoch_state as epoch
from auto_coder.management.original_attempts import canonical

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


def _write(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def _inventory(tmp_path: Path) -> tuple[Path, epoch.EpochInventory]:
    root = tmp_path / "epoch"
    root.mkdir(mode=0o700)
    _write(root / "epoch.lock", b"")
    directory, lock = root.stat(), (root / "epoch.lock").stat()
    inventory = epoch.EpochInventory(1, str(root), directory.st_dev, directory.st_ino, lock.st_dev, lock.st_ino, "deployment-A", "reviewed-A", ("original-A", "original-B"), "request-A", "preparation-A", "a" * 64, "b" * 64, "c" * 64)
    _write(root / "inventory.json", canonical(asdict(inventory)))
    return root, inventory


def _binding(inventory: epoch.EpochInventory) -> epoch.EpochBinding:
    boot, pid, start = epoch.issuer_lifetime()
    return epoch.EpochBinding(inventory.deployment_id, inventory.authorization_id, inventory.enrollment_ids, inventory.request_id, inventory.preparation_id, inventory.target_id, inventory.source_sha256, inventory.config_sha256, "d" * 64, boot, pid, start)


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    return {path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes()) for path in root.iterdir() if path.is_file()}


def _complete(state: epoch.RecoveryEpochState, binding: epoch.EpochBinding) -> epoch.EpochReceipt:
    state.reserve(binding)
    state.record_intent(binding)
    return state.consume(binding)


def _line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 15)[0], "native process did not reach its observation barrier"
    return process.stdout.readline().strip()


def test_durable_phases_bind_exact_inventory_and_never_reissue(tmp_path):
    root, inventory = _inventory(tmp_path)
    initial = _snapshot(root)
    binding = _binding(inventory)
    with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
        assert state.inventory == inventory
        receipt = _complete(state, binding)
        assert receipt.grant_delivered is False
        assert receipt.inventory_sha256 == hashlib.sha256(initial["inventory.json"][2]).hexdigest()
        assert receipt.binding_sha256 == hashlib.sha256(canonical(asdict(binding))).hexdigest()
        assert receipt.consumed_sha256 == hashlib.sha256((root / "consumed.json").read_bytes()).hexdigest()
        assert receipt.receipt_sha256 == hashlib.sha256((root / "receipt.json").read_bytes()).hexdigest()
        with pytest.raises(epoch.EpochUnavailable, match="already consumed"):
            state.consume(binding)
    retained = _snapshot(root)
    assert {"inventory.json", "epoch.lock"} | {phase + suffix for phase in ("reserved", "intent", "consumed", "receipt") for suffix in (".json", "-head.json")} == set(retained)
    previous = ""
    for sequence, phase in enumerate(("reserved", "intent", "consumed", "receipt")):
        record = json.loads(retained[phase + ".json"][2])
        assert record == {"schema_version": 1, "phase": phase, "sequence": sequence, "previous": previous, "inventory_sha256": receipt.inventory_sha256, "binding": json.loads(canonical(asdict(binding)))}
        head = json.loads(retained[phase + "-head.json"][2])
        assert head["record_sha256"] == hashlib.sha256(retained[phase + ".json"][2]).hexdigest()
        assert head["previous"] == previous
        previous = hashlib.sha256(retained[phase + "-head.json"][2]).hexdigest()
    with pytest.raises(epoch.EpochUnavailable, match="closed"):
        state.reserve(binding)
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("a lost receipt became a second invocation")
    assert _snapshot(root) == retained
    assert {name: retained[name] for name in initial} == initial


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_id", "foreign"),
        ("authorization_id", "unreviewed"),
        ("enrollment_ids", ("different",)),
        ("request_id", "other"),
        ("preparation_id", "other"),
        ("target_id", "e" * 64),
        ("source_sha256", "e" * 64),
        ("config_sha256", "e" * 64),
        ("evidence_sha256", "unknown"),
        ("host_pid", True),
        ("host_start_token", "1"),
        ("host_boot_id", "different"),
    ],
)
def test_binding_differences_refuse_before_persistence(tmp_path, field, value):
    root, inventory = _inventory(tmp_path)
    initial = _snapshot(root)
    with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
        with pytest.raises(epoch.EpochUnavailable):
            state.reserve(replace(_binding(inventory), **{field: value}))
    assert _snapshot(root) == initial


@pytest.mark.parametrize("name", ["inventory.json", "epoch.lock"])
def test_missing_or_aliased_fixed_files_are_never_recreated(tmp_path, name):
    root, _ = _inventory(tmp_path)
    path = root / name
    os.link(path, tmp_path / "alias")
    before = _snapshot(root)
    with pytest.raises(epoch.EpochUnavailable):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("a hardlinked fixed file was accepted")
    assert _snapshot(root) == before
    path.unlink()
    before = _snapshot(root)
    with pytest.raises(epoch.EpochUnavailable):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("a missing fixed file was synthesized")
    assert _snapshot(root) == before


@pytest.mark.parametrize("mutation", ["unknown-field", "missing-field", "noncanonical", "wrong-root", "wrong-lock", "boolean-schema", "duplicate-enrollment"])
def test_fixed_inventory_has_a_finite_exact_schema(tmp_path, mutation):
    root, inventory = _inventory(tmp_path)
    value = asdict(inventory)
    if mutation == "unknown-field":
        value["alternate_root"] = str(tmp_path)
    elif mutation == "missing-field":
        del value["authorization_id"]
    elif mutation == "wrong-root":
        value["root_inode"] = inventory.root_inode + 1
    elif mutation == "wrong-lock":
        value["lock_inode"] = inventory.lock_inode + 1
    elif mutation == "boolean-schema":
        value["schema_version"] = True
    elif mutation == "duplicate-enrollment":
        value["enrollment_ids"] = ["original-A", "original-A"]
    encoded = json.dumps(value).encode() if mutation == "noncanonical" else canonical(value)
    (root / "inventory.json").write_bytes(encoded)
    before = _snapshot(root)
    with pytest.raises(epoch.EpochUnavailable):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("an invalid or aliased inventory was accepted")
    assert _snapshot(root) == before


@pytest.mark.parametrize("name", ["reserved.json", "reserved-head.json", "intent.json", "consumed.json", "receipt-head.json", "pending.json"])
def test_partial_or_conflicting_state_is_preserved_and_unavailable(tmp_path, name):
    root, _ = _inventory(tmp_path)
    _write(root / name, b'{"interrupted":')
    before = _snapshot(root)
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("uncertain state was treated as a fresh epoch")
    assert _snapshot(root) == before


@pytest.mark.parametrize("changed", ["inventory", "lock", "record", "root", "released"])
def test_changes_after_reservation_refuse_without_repair(tmp_path, changed):
    root, inventory = _inventory(tmp_path)
    binding = _binding(inventory)
    before: dict[str, tuple[int, int, bytes]] = {}
    preserved = root
    with pytest.raises(epoch.EpochUnavailable):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
            state.reserve(binding)
            if changed == "root":
                preserved = tmp_path / "detached"
                root.rename(preserved)
                root.mkdir(mode=0o700)
            elif changed == "released":
                fcntl.flock(state._lock, fcntl.LOCK_UN)
            else:
                path = root / {"inventory": "inventory.json", "lock": "epoch.lock", "record": "reserved.json"}[changed]
                original = path.read_bytes()
                path.unlink()
                _write(path, original)
            before = _snapshot(preserved)
            state.record_intent(binding)
    assert _snapshot(preserved) == before
    assert "intent.json" not in before


@pytest.mark.parametrize("boundary", range(1, 25))
@pytest.mark.parametrize("fail_after_sync", [False, True])
def test_every_record_and_head_fsync_failure_preserves_uncertainty(tmp_path, monkeypatch, boundary, fail_after_sync):
    root, inventory = _inventory(tmp_path)
    original_fsync = os.fsync
    count = 0

    def interrupted(descriptor):
        nonlocal count
        count += 1
        if count == boundary and not fail_after_sync:
            raise OSError("injected epoch persistence interruption")
        original_fsync(descriptor)
        if count == boundary and fail_after_sync:
            raise OSError("injected epoch persistence interruption")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", interrupted)
        with pytest.raises(epoch.EpochUnavailable, match="injected epoch persistence interruption"):
            with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
                _complete(state, _binding(inventory))
    assert count == boundary
    before = _snapshot(root)
    assert "reserved.json" in before
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("an uncertain persistence result was reset")
    assert _snapshot(root) == before


@pytest.mark.parametrize("phase", ["reserved", "intent", "consumed", "receipt"])
def test_cancellation_after_durable_publication_poisons_the_same_invocation(tmp_path, phase):
    root, inventory = _inventory(tmp_path)
    binding = _binding(inventory)

    def interrupt_after_real_publication(message):
        if message.record["extra"].get("recovery_epoch_phase") == phase:
            raise KeyboardInterrupt("cancel after durable " + phase)

    sink = epoch.logger.add(interrupt_after_real_publication, catch=False)
    try:
        with pytest.raises(epoch.EpochUnavailable, match="closed, failed or inherited"):
            with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
                with pytest.raises(KeyboardInterrupt, match="cancel after durable"):
                    _complete(state, binding)
                assert (root / (phase + ".json")).is_file()
                assert (root / (phase + "-head.json")).is_file()
                before = _snapshot(root)
                # Catching cancellation must not resurrect the same in-memory
                # holder and complete issuance after an interrupted boundary.
                with pytest.raises(epoch.EpochUnavailable, match="closed, failed or inherited"):
                    state.consume(binding)
                assert _snapshot(root) == before
    finally:
        epoch.logger.remove(sink)
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("canceled publication was reset by another invocation")
    assert _snapshot(root) == before


@pytest.mark.parametrize("boundary", [3, 6, 9, 12, 15, 18, 21, 24])
def test_publication_readback_cannot_adopt_a_same_byte_replacement(tmp_path, monkeypatch, boundary):
    root, inventory = _inventory(tmp_path)
    original_fsync = os.fsync
    count = 0
    replaced = None
    before: dict[str, tuple[int, int, bytes]] = {}
    names = [phase + suffix for phase in ("reserved", "intent", "consumed", "receipt") for suffix in (".json", "-head.json")]

    def replace_after_real_publication(descriptor):
        nonlocal count, replaced, before
        original_fsync(descriptor)
        count += 1
        if count == boundary:
            path = root / names[boundary // 3 - 1]
            original_inode = path.stat().st_ino
            replacement = tmp_path / "unfsynced-copy"
            _write(replacement, path.read_bytes())
            replaced = replacement.stat().st_ino
            assert replaced != original_inode
            replacement.replace(path)
            before = _snapshot(root)

    monkeypatch.setattr(os, "fsync", replace_after_real_publication)
    with pytest.raises(epoch.EpochUnavailable, match="append identity or readback differs"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()) as state:
            _complete(state, _binding(inventory))
    assert count == boundary and replaced is not None
    assert _snapshot(root) == before
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("replaced publication was repaired by another invocation")
    assert _snapshot(root) == before


def test_two_native_contenders_reach_the_held_deployment_flock(tmp_path):
    root, _ = _inventory(tmp_path)
    first_code = """
import os, sys
from pathlib import Path
from auto_coder.management.recovery_epoch_state import _hold_recovery_epoch
from tests.test_management_recovery_epoch_state import _binding
with _hold_recovery_epoch(Path(sys.argv[1]), expected_uid=os.geteuid()) as state:
    state.reserve(_binding(state.inventory))
    print('RESERVED_WITH_FLOCK', flush=True)
    sys.stdin.readline()
"""
    contender_code = """
import os, sys
from pathlib import Path
from auto_coder.management.recovery_epoch_state import _hold_recovery_epoch, EpochUnavailable
try:
    with _hold_recovery_epoch(Path(sys.argv[1]), expected_uid=os.geteuid()):
        raise AssertionError('second contender entered')
except EpochUnavailable as error:
    print(str(error), flush=True)
"""
    first = subprocess.Popen([sys.executable, "-c", first_code, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _line(first) == "RESERVED_WITH_FLOCK"
        before = _snapshot(root)
        second = subprocess.run([sys.executable, "-c", contender_code, str(root)], capture_output=True, text=True, timeout=15, check=True)
        assert second.stdout.strip() == "recovery epoch admission contested"
        assert first.poll() is None
        assert _snapshot(root) == before
    finally:
        first.kill()
        first.communicate(timeout=10)
    assert first.returncode == -9
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("host death erased the deployment reservation")
    assert _snapshot(root) == before


@pytest.mark.parametrize("boundary", [3, 6, 9, 12, 15, 18, 21, 24])
def test_native_host_death_after_each_record_or_head_keeps_the_epoch_burned(tmp_path, boundary):
    root, _ = _inventory(tmp_path)
    code = """
import os, sys
from pathlib import Path
from auto_coder.management.recovery_epoch_state import _hold_recovery_epoch
from tests.test_management_recovery_epoch_state import _binding, _complete
original = os.fsync
count = 0
def barrier(fd):
    global count
    original(fd)
    count += 1
    if count == int(sys.argv[2]):
        print('DURABLE_BOUNDARY:' + str(count), flush=True)
        sys.stdin.readline()
with _hold_recovery_epoch(Path(sys.argv[1]), expected_uid=os.geteuid()) as state:
    os.fsync = barrier
    _complete(state, _binding(state.inventory))
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(root), str(boundary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _line(process) == "DURABLE_BOUNDARY:" + str(boundary)
        before = _snapshot(root)
    finally:
        process.kill()
        process.communicate(timeout=10)
    assert process.returncode == -9
    with pytest.raises(epoch.EpochUnavailable, match="existing, partial or conflicting"):
        with epoch._hold_recovery_epoch(root, expected_uid=os.geteuid()):
            pytest.fail("host death produced a reusable recovery grant")
    assert _snapshot(root) == before


def test_production_factory_ignores_environment_roots_and_has_no_override(tmp_path, monkeypatch):
    assert list(inspect.signature(epoch.hold_recovery_epoch).parameters) == []
    seen = []

    def stop_before_open(root, *, uid):
        seen.append((root, uid))
        raise epoch.EpochUnavailable("fixed-root unit observation")

    monkeypatch.setenv("RECOVERY_EPOCH_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTO_CODER_RECOVERY_EPOCH_ROOT", str(tmp_path))
    monkeypatch.setattr(epoch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(epoch, "_approved_directory", stop_before_open)
    with pytest.raises(epoch.EpochUnavailable, match="fixed-root unit observation"):
        with epoch.hold_recovery_epoch():
            pytest.fail("unit seam should stop before real fixed-root access")
    assert seen == [(Path("/var/lib/auto-coder/recovery-epoch"), 0)]
