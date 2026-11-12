"""Real isolated filesystem/lock probes, not ordinary Docker genesis acceptance."""

import fcntl
import hashlib
import json
import os
import select
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from loguru import logger

from auto_coder.management.original_attempts import OriginalEvidenceUnavailable, canonical
from auto_coder.management.original_genesis_reservation import MISSING, observe, reserve, verify


@pytest.fixture
def _use_real_commands():
    pass


def fingerprint(path):
    return {str(item.relative_to(path)): (item.lstat().st_dev, item.lstat().st_ino, item.lstat().st_mode, item.read_bytes() if item.is_file() else None) for item in path.rglob("*")}


def test_new_roots_are_original_reservation_facts_with_explicit_missing_producers(tmp_path):
    records = []
    sink = logger.add(lambda message: records.append(message.record), filter=lambda record: "genesis_boundary" in record["extra"])
    try:
        binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    finally:
        logger.remove(sink)
    original = fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = verify(binding, fence)
    assert evidence.original == (tmp_path / "selected/reservation.json").read_bytes()
    assert hashlib.sha256(evidence.original).hexdigest() == binding.sha256
    assert evidence.unavailable == MISSING
    assert len(evidence.unavailable) == 3
    assert set(asdict(evidence)) == {"binding", "original", "unavailable"}
    raw = json.loads(evidence.original)
    assert raw["profile"] == "ordinary-genesis-root-reservation-v1"
    assert raw["lifetime"]["pid"] == os.getpid()
    assert set(raw["lifetime"]) == {"pid", "start", "boot", "nonce"}
    assert [item.name for item in binding.roots] == ["namespace", "state", "journal", "history"]
    assert len({(item.device, item.inode) for item in binding.roots}) == 4
    assert fingerprint(tmp_path) == original
    assert len(records) == 1
    assert records[0]["extra"] == {"genesis_reservation": binding.path, "genesis_boundary": "roots-reserved", "evidence_only": True, "short_path": "auto_coder/management/original_genesis_reservation.py"}
    assert records[0]["file"].name == "original_genesis_reservation.py"
    assert records[0]["function"] == "reserve" and records[0]["line"] > 0
    assert "unavailable" in records[0]["message"]


@pytest.mark.parametrize("kind", ["empty", "old-state", "partial", "symlink"])
def test_existing_namespace_never_converts_to_genesis(tmp_path, kind):
    target = tmp_path / "selected"
    if kind == "symlink":
        (tmp_path / "old").mkdir(mode=0o700)
        target.symlink_to(tmp_path / "old")
    else:
        target.mkdir(mode=0o700)
        if kind != "empty":
            (target / ("management-child.sqlite3" if kind == "old-state" else "reservation.lock")).write_bytes(b"original")
    original = fingerprint(tmp_path)
    with pytest.raises((FileExistsError, OriginalEvidenceUnavailable)):
        reserve(target, "ordinary-test-deployment")
    assert fingerprint(tmp_path) == original


@pytest.mark.parametrize("mutation", ["record", "record-inode", "record-hardlink", "lock", "lock-inode", "lock-hardlink", "root-inode", "root-symlink", "foreign", "state-effect", "history-effect", "partial", "permissions"])
def test_read_only_verifier_refuses_changes_and_preserves_changed_originals(tmp_path, mutation):
    binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    root = Path(binding.path)
    record = root / "reservation.json"
    if mutation == "record":
        record.write_bytes(record.read_bytes() + b" ")
    elif mutation == "record-inode":
        original = record.read_bytes()
        record.rename(root / "removed")
        record.write_bytes(original)
        record.chmod(0o600)
        (root / "removed").unlink()
    elif mutation == "record-hardlink":
        os.link(record, tmp_path / "alias")
    elif mutation == "lock":
        (root / "reservation.lock").write_bytes(b"changed")
    elif mutation == "lock-inode":
        (root / "reservation.lock").rename(root / "removed")
        (root / "reservation.lock").touch(mode=0o600)
        (root / "removed").unlink()
    elif mutation == "lock-hardlink":
        os.link(root / "reservation.lock", tmp_path / "alias")
    elif mutation in {"root-inode", "root-symlink"}:
        (root / "state").rename(tmp_path / "removed")
        if mutation == "root-inode":
            (root / "state").mkdir(mode=0o700)
        else:
            (root / "state").symlink_to(tmp_path / "removed")
    elif mutation == "foreign":
        foreign = reserve(tmp_path / "foreign", "other-deployment")
        binding = replace(binding, path=foreign.path)
    elif mutation in {"state-effect", "history-effect"}:
        (root / ("state" if mutation == "state-effect" else "history") / "unexpected").write_bytes(b"effect")
    elif mutation == "partial":
        record.unlink()
    else:
        record.chmod(0o640)
    original = fingerprint(tmp_path)
    with pytest.raises((OriginalEvidenceUnavailable, FileNotFoundError)):
        with observe(binding) as fence:
            verify(binding, fence)
    assert fingerprint(tmp_path) == original


def test_verification_requires_this_open_description_current_kernel_lock(tmp_path):
    binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    descriptor = os.open(Path(binding.path) / "reservation.lock", os.O_RDONLY)
    try:
        with observe(binding) as fence:
            assert verify(binding, fence).unavailable == MISSING
            with pytest.raises(OriginalEvidenceUnavailable, match="current reservation fence unavailable"):
                verify(binding, descriptor)
            fcntl.flock(fence, fcntl.LOCK_UN)
            with pytest.raises(OriginalEvidenceUnavailable, match="current reservation fence unavailable"):
                verify(binding, fence)
    finally:
        os.close(descriptor)


def test_namespace_mutation_during_original_read_refuses(tmp_path, monkeypatch):
    from auto_coder.management import original_genesis_reservation as reader

    binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    actual = reader.read_original
    changed = False

    def interleaved(path):
        nonlocal changed
        result = actual(path)
        if path.name == "reservation.json" and not changed:
            (path.parent / "history" / "unobserved-event").write_bytes(b"original")
            changed = True
        return result

    monkeypatch.setattr(reader, "read_original", interleaved)
    with observe(binding) as fence:
        with pytest.raises(OriginalEvidenceUnavailable, match="namespace changed or partial"):
            verify(binding, fence)
    assert changed
    assert (Path(binding.path) / "history/unobserved-event").read_bytes() == b"original"


def test_self_consistent_record_cannot_change_source_or_lifetime(tmp_path):
    binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    path = Path(binding.path) / "reservation.json"
    raw = json.loads(path.read_bytes())
    for field, value, message in (("source", "sha256:" + "0" * 64, "source or scope differs"), ("lifetime", {}, "producer lifetime incomplete")):
        altered = dict(raw, **{field: value})
        encoded = canonical(altered)
        path.write_bytes(encoded)
        submitted = replace(binding, sha256=hashlib.sha256(encoded).hexdigest(), source=altered["source"])
        original = fingerprint(tmp_path)
        with observe(submitted) as fence:
            with pytest.raises(OriginalEvidenceUnavailable, match=message):
                verify(submitted, fence)
        assert fingerprint(tmp_path) == original


@pytest.mark.parametrize("deployment", ["", "  ", "x" * 129])
def test_invalid_scope_creates_nothing(tmp_path, deployment):
    with pytest.raises(OriginalEvidenceUnavailable, match="explicit deployment scope required"):
        reserve(tmp_path / "selected", deployment)
    assert list(tmp_path.iterdir()) == []


def test_unprotected_parent_is_not_repaired(tmp_path):
    tmp_path.chmod(0o750)
    with pytest.raises(OriginalEvidenceUnavailable, match="unprotected original evidence"):
        reserve(tmp_path / "selected", "ordinary-test-deployment")
    assert tmp_path.stat().st_mode & 0o777 == 0o750
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("boundary", [1, 3, 5, 9, 10, 11])
def test_fsync_error_preserves_partial_state_and_never_returns_binding(tmp_path, monkeypatch, boundary):
    from auto_coder.management import original_genesis_reservation as producer

    actual = os.fsync
    count = 0

    def fail_sync(descriptor):
        nonlocal count
        count += 1
        if count == boundary:
            raise OSError("injected durable write failure")
        actual(descriptor)

    monkeypatch.setattr(producer.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="injected durable write failure"):
        reserve(tmp_path / "selected", "ordinary-test-deployment")
    assert count == boundary
    original = fingerprint(tmp_path)
    with pytest.raises(FileExistsError):
        reserve(tmp_path / "selected", "ordinary-test-deployment")
    assert fingerprint(tmp_path) == original


RESERVER = """
import os, sys
from pathlib import Path
from auto_coder.management.original_genesis_reservation import reserve
actual = os.fsync
count = 0
def barrier(fd):
    global count
    actual(fd)
    count += 1
    if count == int(sys.argv[2]):
        print('durable-boundary', flush=True)
        sys.stdin.readline()
os.fsync = barrier
reserve(Path(sys.argv[1]), 'ordinary-test-deployment')
"""


def wait_boundary(process):
    assert select.select([process.stdout], [], [], 15)[0], "producer did not reach durable boundary"
    assert process.stdout.readline() == "durable-boundary\n"
    assert process.poll() is None


@pytest.mark.parametrize("boundary", range(1, 12))
def test_killed_producer_partial_or_published_capture_is_never_reused(tmp_path, boundary, _use_real_commands):
    path = tmp_path / "selected"
    process = subprocess.Popen([sys.executable, "-B", "-c", RESERVER, str(path), str(boundary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_boundary(process)
        # The first producer is materially alive at the exact fsynced boundary.
        original = fingerprint(tmp_path)
        with pytest.raises(FileExistsError):
            reserve(path, "ordinary-test-deployment")
        assert fingerprint(tmp_path) == original
        process.kill()
        process.communicate(timeout=10)
        assert process.returncode == -9
        original = fingerprint(tmp_path)
        with pytest.raises(FileExistsError):
            reserve(path, "ordinary-test-deployment")
        assert fingerprint(tmp_path) == original
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_real_owner_contention_and_separate_read_only_process(tmp_path, _use_real_commands):
    path = tmp_path / "selected"
    process = subprocess.Popen([sys.executable, "-B", "-c", RESERVER, str(path), "11"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_boundary(process)
        descriptor = os.open(path / "reservation.lock", os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        process.communicate("continue\n", timeout=15)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    # A different capture retains its binding in this caller, never from disk.
    binding = reserve(tmp_path / "independent", "ordinary-test-deployment")
    original = fingerprint(tmp_path)
    command = """
import json, sys
from auto_coder.management.original_genesis_reservation import ReservationBinding, ReservedRoot, observe, verify
raw = json.load(sys.stdin)
raw['roots'] = tuple(ReservedRoot(**root) for root in raw['roots'])
for field in ('lock', 'record'):
    raw[field] = tuple(raw[field])
binding = ReservationBinding(**raw)
with observe(binding) as fence:
    evidence = verify(binding, fence)
print(json.dumps(evidence.unavailable))
"""
    result = subprocess.run([sys.executable, "-B", "-c", command], input=json.dumps(asdict(binding)), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == list(MISSING)
    assert fingerprint(tmp_path) == original


def test_observer_refuses_a_material_current_owner_then_reopens_read_only(tmp_path, _use_real_commands):
    binding = reserve(tmp_path / "selected", "ordinary-test-deployment")
    command = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print('durable-boundary', flush=True)
sys.stdin.readline()
os.close(fd)
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", command, str(Path(binding.path) / "reservation.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_boundary(process)
        original = fingerprint(tmp_path)
        with pytest.raises(OriginalEvidenceUnavailable, match="reservation observation contested"):
            with observe(binding):
                pytest.fail("contested observation entered")
        assert fingerprint(tmp_path) == original
        process.communicate("release\n", timeout=10)
        assert process.returncode == 0
        with observe(binding) as fence:
            assert verify(binding, fence).unavailable == MISSING
        assert fingerprint(tmp_path) == original
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
