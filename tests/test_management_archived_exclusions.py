"""Real journals and kernel locks verify exclusion-only A -> B -> C boundaries."""

import fcntl
import json
import os
import select
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.archived_exclusions import ArchivedExclusion, acquire_exclusion_fences, parse_exclusions
from auto_coder.management.exclusion_host import check_exclusion_peer
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, AdmissionStatus, EffectReceipt, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.retained_journal import RetainedGuardedOperationStore, RetainedJournal


def identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def generation(root: Path, name: str, *, active: bool = False, management: bool = False):
    root.mkdir(mode=0o700, exist_ok=True)
    authority = AcceptedAuthority("issuer", "resource", "project", "uid", "client", "grant", ("read", "deploy"), 1, 1)
    target = ManagedTarget(name, "management", "kitamura-tetsuo/auto-coder") if management else ManagedTarget("worker", "beta", "org/repo")
    action = "management-service-self-update" if management else "restart_worker"
    params = (ActionParameter("source_sha", "a" * 40),) if management else (ActionParameter("target_id", "beta"),)
    intent = OperationIntent(target, action, params, "old-key-" + name)
    store = ManagementOperationStore(root)
    record = store.admit(intent, authority, lifecycle_snapshot=None if management else {"schema_version": 1}).record
    assert record is not None
    if not active:
        record = store.transition(record.operation_id, 0, OperationPhase.FAILED, evidence=EffectReceipt(record.operation_id, "disposable-fixture", 1, "no_effect_sent")).record
        assert record is not None
    lock = root / "owner.lock"
    lock.touch(mode=0o600)
    return store, record, lock


def reader(root: Path, name: str, *, archives=()):
    database, lock = root / "management-operations.sqlite3", root / "owner.lock"
    return RetainedJournal(database, lock, database_identity=identity(database), fence_identity=identity(lock), prior_management_instance=name, exclusions=archives)


def binding(root: Path, name: str, index: int = 0) -> ArchivedExclusion:
    database, lock = root / "management-operations.sqlite3", root / "owner.lock"
    return ArchivedExclusion(name, str(root), f"/management-excluded/{index}/journal", *identity(database), str(lock), f"/management-excluded/{index}/owner.lock", *identity(lock))


def test_terminal_archive_keys_refuse_without_online_projection_or_database_changes(tmp_path: Path):
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _, old_a, _ = generation(a, "manager-a")
    _, old_b, _ = generation(b, "manager-b")
    originals = {p: p.read_bytes() for p in (a / "management-operations.sqlite3", b / "management-operations.sqlite3")}
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        current = RetainedGuardedOperationStore(c, retained)
        denied = current.admit(old_a.intent, old_a.authority)
        assert denied.status is AdmissionStatus.CONFLICT and denied.record is None
        assert retained.owned_evidence(old_a.operation_id, old_a.intent.target, "beta", old_a.authority) is None
        assert retained.owned_evidence(old_b.operation_id, old_b.intent.target, "beta", old_b.authority)[0] == old_b
        allowed = current.admit(replace(old_a.intent, idempotency_key="new-key"), old_a.authority)
        assert allowed.status is AdmissionStatus.CREATED and allowed.record is not None
        assert current.effect_permitted(allowed.record)
        assert current.get(old_a.operation_id).record is None
    assert {p: p.read_bytes() for p in originals} == originals


def test_management_key_exclusion_uses_original_instance_not_successor_target(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    _, original, _ = generation(a, "manager-a", management=True)
    generation(b, "manager-b", management=True)
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        result = retained.management_request(original.intent.idempotency_key, "a" * 40, original.authority)
        assert result is not None and result.status is AdmissionStatus.CONFLICT and result.record is None
        assert retained.owned_management(original.operation_id, original.authority) is None
        assert retained.management_request("unrelated-key", "b" * 40, original.authority) is None


@pytest.mark.parametrize("fault", ["active", "history", "database", "fence"])
def test_unobservable_or_unsettled_archive_blocks_admission_and_later_effect(tmp_path: Path, fault: str):
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _, original, _ = generation(a, "manager-a", active=fault == "active")
    generation(b, "manager-b")
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        current = RetainedGuardedOperationStore(c, retained)
        # Pre-existing current intent must still be guarded before its effect.
        admitted = ManagementOperationStore(c).admit(replace(original.intent, idempotency_key="current-key"), original.authority).record
        assert admitted is not None
        if fault == "history":
            with sqlite3.connect(a / "management-operations.sqlite3") as connection:
                connection.execute("DELETE FROM operation_history")
        elif fault in {"database", "fence"}:
            path = a / ("owner.lock" if fault == "fence" else "management-operations.sqlite3")
            path.rename(a / "original-retained")
            path.touch()
        assert not current.effect_permitted(admitted)
        assert current.admit(replace(original.intent, idempotency_key="other-key"), original.authority).status is AdmissionStatus.UNAVAILABLE
        with pytest.raises((ValueError, sqlite3.Error)):
            retained.read()


def test_archived_key_cannot_dispatch_from_a_preexisting_current_row(tmp_path: Path):
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _, original, _ = generation(a, "manager-a")
    generation(b, "manager-b")
    record = ManagementOperationStore(c).admit(original.intent, original.authority).record
    with reader(b, "manager-b", archives=(reader(a, "manager-a"),)) as retained:
        assert record is not None and not RetainedGuardedOperationStore(c, retained).effect_permitted(record)


def test_binding_parser_rejects_incomplete_duplicate_and_unbounded_ancestry(tmp_path: Path):
    generation(tmp_path / "a", "manager-a")
    entry = asdict(binding(tmp_path / "a", "manager-a"))
    assert parse_exclusions([entry])[0].identity() == binding(tmp_path / "a", "manager-a").identity()
    for raw in [
        [{**entry, "unexpected": True}],
        [{k: v for k, v in entry.items() if k != "fence_inode"}],
        [entry] * 33,
        [entry, {**entry, "journal_root": "/management-excluded/1/journal", "fence": "/management-excluded/1/owner.lock"}],
        [{**entry, "fence_device": True}],
        [{**entry, "fence": "/arbitrary/owner.lock"}],
    ]:
        with pytest.raises(ValueError):
            parse_exclusions(raw)


def test_partial_fence_acquisition_releases_earlier_descriptors(tmp_path: Path):
    generation(tmp_path / "a", "manager-a")
    generation(tmp_path / "b", "manager-b")
    first, second = binding(tmp_path / "a", "manager-a"), binding(tmp_path / "b", "manager-b", 1)
    with Path(second.host_fence).open("r") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            acquire_exclusion_fences((first, second), host=True)
    with Path(first.host_fence).open("r") as contender:
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.usefixtures("_use_real_commands")
def test_independent_shared_fences_overlap_host_to_successor_without_owner_gap(tmp_path: Path):
    for name in ("a", "b"):
        generation(tmp_path / name, "manager-" + name)
    bindings = (binding(tmp_path / "a", "manager-a"), binding(tmp_path / "b", "manager-b", 1))
    held = acquire_exclusion_fences(bindings, host=True)
    manifest = tmp_path / "bindings.json"
    manifest.write_text(json.dumps([asdict(item) for item in bindings]))
    program = """import json,sys
from pathlib import Path
from auto_coder.management.archived_exclusions import parse_exclusions,acquire_exclusion_fences
held=acquire_exclusion_fences(parse_exclusions(json.loads(Path(sys.argv[1]).read_text())),host=True)
print('held',flush=True)
sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-I", "-B", "-c", program, str(manifest)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and select.select([process.stdout], [], [], 15)[0]
        assert process.stdout.readline().strip() == "held"
        check_exclusion_peer(bindings, held, process.pid)
        for fd in held:
            os.close(fd)
        held = ()
        for entry in bindings:
            with Path(entry.host_fence).open("r") as old_owner:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(old_owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for fd in held:
            os.close(fd)
        if process.stdin is not None:
            process.stdin.write("exit\n")
            process.stdin.flush()
        process.wait(timeout=15)
    assert process.returncode == 0
    for entry in bindings:
        with Path(entry.host_fence).open("r") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_cross_generation_storage_overlap_is_rejected(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    generation(a, "manager-a")
    generation(b, "manager-b")
    first, second = binding(a, "manager-a"), binding(b, "manager-b", 1)
    nested = replace(second, host_journal_root=str(a / "nested"))
    with pytest.raises(ValueError, match="overlapping"):
        parse_exclusions([asdict(first), asdict(nested)])
    cross_fence = replace(second, host_fence=str(a / "other.lock"))
    with pytest.raises(ValueError, match="overlapping"):
        parse_exclusions([asdict(first), asdict(cross_fence)])
