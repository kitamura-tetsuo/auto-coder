"""Production-origin filesystem/process checks; no Docker-host success oracle.

Loguru sinks are observation barriers after the real durable boundary. They do
not substitute the writer, grant adapter, OS locks or original file observations.
"""

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management.fresh_host_observation import serve_one_fresh_grant
from auto_coder.management.fresh_initialization import claim_fresh_initialization, load_fresh_initialization
from auto_coder.management.original_attempt_evidence import derive, observe, verify
from auto_coder.management.original_attempts import HistoryBinding, OriginalEvidenceUnavailable, enroll, history_lock, read_frontier
from tests.test_management_fresh_initialization import _intent


@pytest.fixture
def _use_real_commands() -> None:
    pass


@pytest.fixture
def _use_real_sleep() -> None:
    pass


def _history(root: Path) -> HistoryBinding:
    history = root / "history"
    history.mkdir(mode=0o700)
    return enroll(history, "isolated-test-deployment")


def _covered(root: Path, binding: HistoryBinding, name: str):
    directory = root / name
    directory.mkdir(mode=0o700)
    return replace(_intent(directory), new_instance=name, new_container_id=hashlib.sha256(name.encode()).hexdigest(), original_history=binding.root, original_history_id=binding.history)


def _fingerprint(root: Path):
    return {str(path.relative_to(root)): (path.stat().st_dev, path.stat().st_ino, path.read_bytes()) for path in root.rglob("*") if path.is_file()}


def _terminal(root: Path, binding: HistoryBinding, name: str):
    # Actual original pre-grant refusal: retained OAuth inode differs.
    intent = _covered(root, binding, name)
    intent = replace(intent, oauth_inode=intent.oauth_inode + 1)
    with pytest.raises(ValueError, match="retained OAuth root identity differs"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    return intent


def test_original_origin_terminal_is_independently_verifiable_and_never_reusable(tmp_path):
    binding = _history(tmp_path)
    intent = _terminal(tmp_path, binding, "failed")
    original = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == ()
        assert [(a.request_id, a.predecessor, a.target, a.state) for a in evidence.attempts] == [(intent.request_id, "old-instance", "failed", "never-created")]
        assert verify(evidence, binding, fence) == evidence
        with pytest.raises(OriginalEvidenceUnavailable, match="forged, stale, truncated"):
            verify(replace(evidence, attempts=()), binding, fence)
        with pytest.raises(OriginalEvidenceUnavailable, match="forged, stale, truncated"):
            verify(replace(evidence, originals=()), binding, fence)
    with pytest.raises(OriginalEvidenceUnavailable, match="no replay"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    # Actual host grant entry refuses before opening any socket/callback.
    with pytest.raises(OriginalEvidenceUnavailable, match="one-time admission"):
        serve_one_fresh_grant(tmp_path / "fresh-guard.sock", intent, None, None, -1, -1)
    assert _fingerprint(tmp_path) == original
    assert list(Path(intent.host_state_root).iterdir()) == []
    assert list(Path(intent.host_journal_root).iterdir()) == []


def test_complete_frontier_retains_claim_and_reverse_failed_branch(tmp_path):
    binding = _history(tmp_path)
    first = _covered(tmp_path, binding, "first")
    claim_fresh_initialization(Path(first.host_state_root), first)
    successor = replace(_covered(tmp_path, binding, "successor"), old_instance=first.new_instance, old_container_id=first.new_container_id)
    claim_fresh_initialization(Path(successor.host_state_root), successor)
    failed = replace(_covered(tmp_path, binding, "failed"), old_instance=first.new_instance, old_container_id=first.new_container_id, oauth_inode=first.oauth_inode + 1000)
    with pytest.raises(ValueError):
        claim_fresh_initialization(Path(failed.host_state_root), failed)
    original = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert [(a.target, a.predecessor, a.state) for a in evidence.attempts] == [("first", "old-instance", "claimed"), ("successor", "first", "claimed"), ("failed", "first", "never-created")]
        assert evidence.unavailable == (first.request_id + ": original grant boundary not crossed", successor.request_id + ": original grant boundary not crossed")
        with pytest.raises(OriginalEvidenceUnavailable):
            verify(replace(evidence, unavailable=()), binding, fence)
    assert _fingerprint(tmp_path) == original


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "boolean-pid", "numeric-start", "noncanonical-start", "bad-boot", "noncanonical-nonce"],
)
def test_independent_reader_refuses_forged_grant_lifetime_with_valid_frontier(tmp_path, mutation):
    """A test-only forged digest chain is not original producer evidence."""
    from auto_coder.management import original_attempts as producer

    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "grant-lifetime")
    claim_fresh_initialization(Path(intent.host_state_root), intent)
    # This enters the real prospective original grant-history writer, not a host grant.
    with producer.grant_boundary(intent):
        pass
    root = Path(binding.root)
    with observe(binding) as fence:
        produced = derive(binding, fence)
        assert len(produced.attempts) == 1
        assert produced.attempts[0].state == "grant-possible"
        assert not any("lifetime incomplete" in reason for reason in produced.unavailable)

    original_stores = _fingerprint(Path(intent.host_state_root))
    event_path = root / "00000002.json"
    event = json.loads(event_path.read_bytes())
    assert event["kind"] == "grant-possible"
    lifetime = event["value"]["lifetime"]
    if mutation == "missing":
        del event["value"]["lifetime"]
    elif mutation == "extra":
        lifetime["unapproved"] = "value"
    elif mutation == "boolean-pid":
        lifetime["pid"] = True
    elif mutation == "numeric-start":
        lifetime["start"] = 123
    elif mutation == "noncanonical-start":
        lifetime["start"] = "0" + lifetime["start"]
    elif mutation == "bad-boot":
        lifetime["boot"] = "invalid"
    else:
        lifetime["nonce"] = lifetime["nonce"].upper()

    # Forge an internally valid file/hash frontier; detect semantic provenance
    # corruption rather than relying on a broken hash as the refusal oracle.
    event_bytes = producer.canonical(event)
    event_path.write_bytes(event_bytes)
    frontier_path = root / "frontier.json"
    frontier = json.loads(frontier_path.read_bytes())
    frontier["digest"] = hashlib.sha256(event_bytes).hexdigest()
    frontier_path.write_bytes(producer.canonical(frontier))
    before = _fingerprint(tmp_path)
    assert len(read_frontier(root, binding)[1]) == 3
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == ("original grant lifetime incomplete",)
        with pytest.raises(OriginalEvidenceUnavailable):
            verify(evidence, binding, fence)
    assert _fingerprint(tmp_path) == before
    assert _fingerprint(Path(intent.host_state_root)) == original_stores


def test_derived_original_digest_and_consumed_bytes_share_one_read(tmp_path, monkeypatch):
    """Unit substitution: catch a second parse read after an original snapshot."""
    from auto_coder.management import original_attempt_evidence as reader

    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "single-read")
    claim_fresh_initialization(Path(intent.host_state_root), intent)
    target = Path(intent.host_state_root) / "fresh-initialization.json"
    baseline = _fingerprint(tmp_path)
    original_snapshot = reader._snapshot
    original_read = reader.read_original
    armed = False
    substituted = 0

    def snapshot(path):
        nonlocal armed
        result = original_snapshot(path)
        if path == target:
            armed = True
        return result

    def read(path):
        nonlocal armed, substituted
        if path == target and armed:
            armed = False
            substituted += 1
            return b"{}", original_read(path)[1]
        return original_read(path)

    monkeypatch.setattr(reader, "_snapshot", snapshot)
    monkeypatch.setattr(reader, "read_original", read)
    with observe(binding) as fence:
        evidence = reader.derive(binding, fence)
    assert substituted == 0
    assert evidence.unavailable == (intent.request_id + ": original grant boundary not crossed",)
    assert _fingerprint(tmp_path) == baseline


def test_missing_legacy_histories_and_new_enrollment_are_unavailable_without_writes(tmp_path):
    legacy = _intent(tmp_path)
    claim_fresh_initialization(Path(legacy.host_state_root), legacy)
    binding = HistoryBinding(str(tmp_path / "absent-history"), "deployment", "unknown", "unknown")
    before = _fingerprint(tmp_path)
    evidence = derive(binding, -1)
    assert evidence.attempts == () and evidence.unavailable
    assert _fingerprint(tmp_path) == before
    binding = _history(tmp_path)
    before = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.attempts == ()
        assert evidence.unavailable == ("prospective enrollment has no original attempts; legacy coverage unavailable",)
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize("mutation", ["frontier", "lock", "anchor", "deployment", "truncation", "partial", "profile"])
def test_verifier_reopens_original_authority_and_refuses_changed_inputs(tmp_path, mutation):
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "first")
    with observe(binding) as fence:
        old = derive(binding, fence)
        assert old.unavailable == ()
    root = Path(binding.root)
    if mutation == "frontier":
        _terminal(tmp_path, binding, "second")
    elif mutation == "lock":
        path = root / "history.lock"
        path.rename(root / "removed.lock")
        path.write_bytes(b"{}")
        path.chmod(0o600)
    elif mutation == "anchor":
        path = root / "00000000.json"
        path.write_bytes(path.read_bytes().replace(b'"pristine":true', b'"pristine":false'))
    elif mutation == "deployment":
        old = replace(old, binding=replace(binding, deployment="another-deployment"))
    elif mutation == "truncation":
        (root / "00000001.json").unlink()
    elif mutation == "partial":
        (root / "frontier.pending").write_bytes(b'{"count":')
    else:
        path = root / "authority.json"
        path.write_bytes(path.read_bytes().replace(b"fresh-original-attempts-v1", b"unknown-original-profile"))
    before = _fingerprint(tmp_path)
    with observe(binding) as fence:
        with pytest.raises(OriginalEvidenceUnavailable):
            verify(old, binding, fence)
    assert _fingerprint(tmp_path) == before


_CHILD = r"""
import json, sys
from dataclasses import asdict
from pathlib import Path
from loguru import logger
from auto_coder.management.fresh_initialization import parse_fresh_intent, claim_fresh_initialization
from auto_coder.management.fresh_host_observation import serve_one_fresh_grant
intent = parse_fresh_intent(json.loads(Path(sys.argv[1]).read_bytes()))
boundary = sys.argv[2]
def barrier(message):
    if "boundary=" + boundary in str(message):
        print("DURABLE:" + boundary, flush=True)
        sys.stdin.readline()
logger.remove()
logger.add(barrier)
print("ENTERING", flush=True)
sys.stdin.readline()
try:
    if boundary == "grant-possible":
        serve_one_fresh_grant(Path(intent.host_state_root) / "fresh-guard.sock", intent, None, None, -1, -1)
    else:
        claim_fresh_initialization(Path(intent.host_state_root), intent)
except Exception as error:
    print(type(error).__name__ + ":" + str(error), flush=True)
    sys.exit(7)
"""


def _child(tmp_path, intent, boundary):
    path = tmp_path / (boundary + "-input.json")
    path.write_text(json.dumps(asdict(intent)))
    child = subprocess.Popen([sys.executable, "-B", "-c", _CHILD, str(path), boundary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    assert child.stdout.readline().strip() == "ENTERING"
    return child


@pytest.mark.parametrize("boundary", ["anchor", "claimed", "never-created", "grant-possible"])
def test_real_process_contention_and_interruption_at_durable_boundaries(tmp_path, boundary, _use_real_commands, _use_real_sleep):
    binding = _history(tmp_path)
    first = _covered(tmp_path, binding, "first")
    if boundary == "never-created":
        first = replace(first, oauth_inode=first.oauth_inode + 1)
    if boundary == "grant-possible":
        claim_fresh_initialization(Path(first.host_state_root), first)
    second = _covered(tmp_path, binding, "second")
    owner = _child(tmp_path, first, boundary)
    contender = None
    try:
        owner.stdin.write("go\n")
        owner.stdin.flush()
        assert owner.stdout.readline().strip() == "DURABLE:" + boundary
        # The first process is stopped at an observable production log boundary
        # after fsync, still holding the actual exclusive history flock.
        contender = _child(tmp_path, second, "anchor")
        contender.stdin.write("go\n")
        contender.stdin.flush()
        output, stderr = contender.communicate(timeout=15)
        assert contender.returncode == 7
        assert output.strip() == "OriginalEvidenceUnavailable:history admission contested", stderr
        assert not any(Path(second.host_state_root).iterdir())
        with pytest.raises(OriginalEvidenceUnavailable, match="contested"):
            with observe(binding):
                pytest.fail("observer crossed the actual producer lock")
        owner.kill()
        owner.wait(timeout=10)
        before = _fingerprint(tmp_path)
        with observe(binding) as fence:
            evidence = derive(binding, fence)
            assert len(evidence.attempts) == 1
            assert evidence.attempts[0].state == boundary
            if boundary == "never-created":
                assert evidence.unavailable == ()
                verify(evidence, binding, fence)
            else:
                assert evidence.unavailable
        assert _fingerprint(tmp_path) == before
        if boundary != "claimed":
            with pytest.raises(OriginalEvidenceUnavailable):
                serve_one_fresh_grant(tmp_path / "fresh-guard.sock", first, None, None, -1, -1)
        assert not (tmp_path / "fresh-guard.sock").exists()
        assert not (Path(first.host_journal_root) / "management-operations.sqlite3").exists()
    finally:
        for child in (owner, contender):
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)


def test_producer_rejects_source_mismatch_missing_authority_and_aliases(tmp_path):
    binding = _history(tmp_path)
    first = _covered(tmp_path, binding, "first")
    claim_fresh_initialization(Path(first.host_state_root), first)
    for changed in (
        replace(_covered(tmp_path, binding, "alias"), new_container_id=first.new_container_id),
        replace(_covered(tmp_path, binding, "foreign"), old_instance="outside-history"),
        replace(_covered(tmp_path, binding, "cycle"), old_instance=first.new_instance, old_container_id=first.new_container_id, new_instance=first.old_instance),
    ):
        before = _fingerprint(tmp_path)
        with pytest.raises(OriginalEvidenceUnavailable):
            claim_fresh_initialization(Path(changed.host_state_root), changed)
        assert _fingerprint(tmp_path) == before
    path = Path(binding.root) / "authority.json"
    raw = json.loads(path.read_bytes())
    raw["binding"]["source"] = "sha256:" + "0" * 64
    path.write_text(json.dumps(raw))
    new = _covered(tmp_path, binding, "different-source")
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="source"):
        claim_fresh_initialization(Path(new.host_state_root), new)
    assert _fingerprint(tmp_path) == before


def test_separate_process_verifies_originals_and_rejects_stale_frontier(tmp_path, _use_real_commands):
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "first")
    with observe(binding) as fence:
        evidence = derive(binding, fence)
    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(asdict(binding)))
    submitted_file = tmp_path / "submitted.json"
    submitted_file.write_text(json.dumps(asdict(evidence)))
    command = [sys.executable, "-B", "-m", "auto_coder.management.original_attempt_evidence", "--binding", str(binding_file), "--submitted", str(submitted_file)]
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    before = _fingerprint(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == json.loads(submitted_file.read_bytes())
    assert _fingerprint(tmp_path) == before
    _terminal(tmp_path, binding, "second")
    before = _fingerprint(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=15)
    assert result.returncode == 1
    assert "forged, stale, truncated or cross-deployment" in result.stderr
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize("malformation", ["missing-original-field", "bad-original-list", "bad-attempt-list", "bad-store-pair", "bad-binding", "bad-unavailable", "bad-json"])
def test_separate_consumer_refuses_malformed_submitted_evidence_without_traceback(tmp_path, malformation, _use_real_commands):
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "first")
    with observe(binding) as fence:
        evidence = derive(binding, fence)
    payload = asdict(evidence)
    if malformation == "missing-original-field":
        del payload["originals"][0]["inode"]
    elif malformation == "bad-original-list":
        payload["originals"] = {"unexpected": "object"}
    elif malformation == "bad-attempt-list":
        payload["attempts"] = "not a list"
    elif malformation == "bad-store-pair":
        payload["attempts"][0]["expected_stores"] = [["child"]]
    elif malformation == "bad-binding":
        payload["binding"] = ["not", "a", "binding"]
    elif malformation == "bad-unavailable":
        payload["unavailable"] = [False]
    binding_file = tmp_path / "binding.json"
    binding_file.write_text(json.dumps(asdict(binding)))
    submitted_file = tmp_path / "malformed-evidence.json"
    submitted_file.write_text("{" if malformation == "bad-json" else json.dumps(payload))
    original = _fingerprint(tmp_path)
    command = [sys.executable, "-B", "-m", "auto_coder.management.original_attempt_evidence", "--binding", str(binding_file), "--submitted", str(submitted_file)]
    result = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}, timeout=15)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "original attempt coverage unavailable:" in result.stderr
    assert "Traceback" not in result.stderr
    assert _fingerprint(tmp_path) == original


def test_current_held_fence_is_required_and_released_on_refusal(tmp_path):
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "first")
    descriptor = os.open(Path(binding.root) / "history.lock", os.O_RDONLY)
    try:
        result = derive(binding, descriptor)
        assert result.unavailable == ("consumer does not hold a shared history fence",)
    finally:
        os.close(descriptor)
    with observe(binding) as descriptor:
        assert derive(binding, descriptor).unavailable == ()
    # Observe's read-only descriptor is actually closed; a new writer obtains EX.
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with history_lock(Path(binding.root), exclusive=True):
        pass


def test_original_admission_trace_retains_boundaries_without_recovery_success(tmp_path):
    from loguru import logger

    binding = _history(tmp_path)
    records = []
    sink = logger.add(lambda message: records.append(message.record), filter=lambda record: "original_boundary" in record["extra"])
    try:
        intent = _terminal(tmp_path, binding, "refused")
        with observe(binding):
            contender = _covered(tmp_path, binding, "contender")
            with pytest.raises(OriginalEvidenceUnavailable, match="contested"):
                claim_fresh_initialization(Path(contender.host_state_root), contender)
    finally:
        logger.remove(sink)
    assert [record["extra"]["original_boundary"] for record in records] == ["anchor", "never-created", "contested"]
    assert all(record["extra"]["original_history"] == binding.root and record["extra"]["evidence_only"] is True for record in records)
    assert records[0]["extra"]["original_attempt"] == intent.request_id
    assert all(record["file"].name == "original_attempts.py" and record["function"] and record["line"] > 0 for record in records)
    assert not any("succeeded" in record["message"] or "recovery" in record["message"] for record in records)


def test_partial_original_write_is_unavailable_and_not_repaired(tmp_path):
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "first")
    root = Path(binding.root)
    # Fault control represents a crash after exclusive event creation but before
    # a complete event fsync/frontier publication, not a positive producer oracle.
    (root / "00000002.json").write_bytes(b'{"previous":')
    (root / "00000002.json").chmod(0o600)
    before = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == ("partial, omitted or conflicting frontier",)
    new = _covered(tmp_path, binding, "second")
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="partial"):
        claim_fresh_initialization(Path(new.host_state_root), new)
    assert _fingerprint(tmp_path) == before


@pytest.mark.parametrize("filename", ["history.lock", "authority.json", "frontier.json", "00000000.json"])
@pytest.mark.parametrize("permissive_mode", [0o640, 0o644])
def test_original_authority_refuses_group_or_other_read_access(tmp_path, filename, permissive_mode):
    """Protected original history must never be readable by another principal."""
    binding = _history(tmp_path)
    _terminal(tmp_path, binding, "original")
    root = Path(binding.root)
    with observe(binding) as fence:
        assert derive(binding, fence).unavailable == ()

    target = root / filename
    assert target.stat().st_mode & 0o777 == 0o600
    target.chmod(permissive_mode)
    before = _fingerprint(tmp_path)
    contender = _covered(tmp_path, binding, "contender")
    if filename == "history.lock":
        with pytest.raises(OriginalEvidenceUnavailable, match="unprotected original evidence"):
            with observe(binding):
                pass
    else:
        with observe(binding) as fence:
            unavailable = derive(binding, fence)
            assert unavailable.unavailable
            with pytest.raises(OriginalEvidenceUnavailable):
                verify(unavailable, binding, fence)
    with pytest.raises(OriginalEvidenceUnavailable, match="unprotected original evidence"):
        claim_fresh_initialization(Path(contender.host_state_root), contender)
    assert _fingerprint(tmp_path) == before
    assert target.stat().st_mode & 0o777 == permissive_mode
    assert list(Path(contender.host_state_root).iterdir()) == []
    assert list(Path(contender.host_journal_root).iterdir()) == []


def test_native_store_identity_receipt_is_once_only_and_missing_store_refuses(tmp_path):
    """Native store boundary unit check, never a positive host-launch oracle."""
    from contextlib import closing

    from auto_coder.management.bootstrap_store import BootstrapStore
    from auto_coder.management.operation_store import ManagementOperationStore
    from auto_coder.management.original_attempts import grant_boundary, record_original_stores
    from auto_coder.management.recovery_mailbox import RecoveryMailbox
    from auto_coder.management.self_update_handoff import HandoffStore

    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "native")
    intent = replace(intent, state_root=intent.host_state_root, journal_root=intent.host_journal_root, oauth_root=intent.host_oauth_root)
    state = Path(intent.host_state_root)
    claim_fresh_initialization(state, intent)
    # Unit-only entry into the provenance gate; no host grant was delivered.
    with grant_boundary(intent):
        pass
    with pytest.raises(FileNotFoundError):
        record_original_stores(state, intent)
    assert not (state / "fresh-original-stores.json").exists()
    old_umask = os.umask(0o077)
    try:
        with closing(BootstrapStore(state, intent.new_instance)._connect()):
            pass
        with closing(ManagementOperationStore(Path(intent.journal_root))._connect()):
            pass
        with closing(HandoffStore(state, intent.new_instance)._connect()):
            pass
        RecoveryMailbox(state, intent.new_instance).initialize()
    finally:
        os.umask(old_umask)
    owner_path = state / ".supervisor-owner.lock"
    owner_path.write_bytes(b"")
    owner_path.chmod(0o600)
    with owner_path.open("rb") as native_owner:
        fcntl.flock(native_owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        record_original_stores(state, intent)
        before = _fingerprint(tmp_path)
        with pytest.raises(FileExistsError):
            record_original_stores(state, intent)
        assert _fingerprint(tmp_path) == before
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == (intent.request_id + ": current original-store owner fence unavailable",)
    owner_fd = os.open(owner_path, os.O_RDONLY)
    fcntl.flock(owner_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    with observe(binding) as fence:
        evidence = derive(binding, fence, (owner_fd,))
        assert evidence.unavailable == (intent.request_id + ": original outcome remains unknown",)
    operations = Path(intent.journal_root) / "management-operations.sqlite3"
    rollback = operations.with_name(operations.name + "-journal")
    rollback.write_bytes(b"unresolved rollback transaction")
    rollback.chmod(0o600)
    before_rollback = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence, (owner_fd,))
        assert evidence.unavailable == (intent.request_id + ": original database rollback journal is unresolved",)
        with pytest.raises(OriginalEvidenceUnavailable):
            verify(evidence, binding, fence, (owner_fd,))
    assert _fingerprint(tmp_path) == before_rollback
    rollback.unlink()
    operations.unlink()
    before = _fingerprint(tmp_path)
    with observe(binding) as fence:
        missing = derive(binding, fence, (owner_fd,))
        assert len(missing.attempts) == 1
        assert len(missing.unavailable) == 1 and "management-operations.sqlite3" in missing.unavailable[0]
    os.close(owner_fd)
    assert _fingerprint(tmp_path) == before


def test_contender_before_anchor_leaves_no_original_intent_or_store(tmp_path, _use_real_commands):
    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "before-anchor")
    with history_lock(Path(binding.root), exclusive=True):
        child = _child(tmp_path, intent, "anchor")
        try:
            child.stdin.write("go\n")
            child.stdin.flush()
            output, error = child.communicate(timeout=15)
            assert child.returncode == 7, error
            assert output.strip() == "OriginalEvidenceUnavailable:history admission contested"
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
    assert read_frontier(Path(binding.root))[1] == []
    assert list(Path(intent.host_state_root).iterdir()) == []
    assert list(Path(intent.host_journal_root).iterdir()) == []
    with observe(binding) as fence:
        assert derive(binding, fence).unavailable == ("prospective enrollment has no original attempts; legacy coverage unavailable",)


def test_ordinary_fresh_and_upgrade_refusals_preserve_original_records(tmp_path):
    from auto_coder.management.bootstrap_upgrade import UpgradeUnavailable, prepare
    from auto_coder.management.operation_store import ManagementOperationStore

    intent = _intent(tmp_path)
    # A real existing journal must never be treated as a new empty target.
    ManagementOperationStore(Path(intent.host_journal_root)).get("absent")
    originals = {
        "oauth/retained-oauth": b"retained oauth bytes",
        "worker-config.yaml": b"worker_target: unchanged\n",
        "controller-config.json": b'{"controller":"unchanged"}',
        "upgrade-plan.json": b'{"outcome":"unresolved"}',
        "old-fresh-launch-claimed.json": b'{"request":"unresolved"}',
    }
    for relative, content in originals.items():
        (tmp_path / relative).write_bytes(content)
    before = _fingerprint(tmp_path)
    with pytest.raises(FileExistsError, match="initialization journal is not empty"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    with pytest.raises(UpgradeUnavailable, match="source credential use requires explicit operator acceptance"):
        prepare(tmp_path / "no-upgrade-root", tmp_path / "no-profile", tmp_path / "no-credential", authorize_source_read=False)
    assert _fingerprint(tmp_path) == before
    assert not (Path(intent.host_state_root) / "fresh-initialization.json").exists()
    assert not (tmp_path / "no-upgrade-root").exists()


def test_partial_history_extension_is_refused_without_modifying_legacy_intent(tmp_path):
    from auto_coder.management.fresh_initialization import parse_fresh_intent

    intent = _intent(tmp_path)
    claim_fresh_initialization(Path(intent.host_state_root), intent)
    original = _fingerprint(tmp_path)
    raw = asdict(intent)
    del raw["original_history_id"]
    with pytest.raises(ValueError, match="partial fresh intent extension"):
        parse_fresh_intent(raw)
    raw = asdict(intent)
    del raw["original_history"]
    del raw["original_history_id"]
    assert parse_fresh_intent(raw) == intent
    assert load_fresh_initialization(Path(intent.host_state_root), host=True)[0] == intent
    assert _fingerprint(tmp_path) == original


def test_provenance_cannot_be_enrolled_inside_retained_oauth_or_target_roots(tmp_path):
    intent = _intent(tmp_path)
    history = Path(intent.host_oauth_root) / "forbidden-history"
    history.mkdir(mode=0o700)
    binding = enroll(history, "isolated-test-deployment")
    intent = replace(intent, original_history=binding.root, original_history_id=binding.history)
    before = _fingerprint(tmp_path)
    with pytest.raises(OriginalEvidenceUnavailable, match="overlaps protected management stores"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    assert _fingerprint(tmp_path) == before


def test_upgrade_retains_typed_original_schema_refusal(tmp_path):
    from types import SimpleNamespace

    from auto_coder.management.bootstrap_upgrade import UpgradeUnavailable, _fresh_intent

    path = tmp_path / "invalid-intent.json"
    path.write_text("{}")
    path.chmod(0o600)
    with pytest.raises(UpgradeUnavailable) as caught:
        _fresh_intent(SimpleNamespace(intent_file=str(path)))
    assert caught.value.stage == "prerequisite-blocked"
    assert caught.value.reason == "fresh initialization intent differs"
    assert path.read_bytes() == b"{}"


# Isolated process barriers wrap genuine writer syscalls. Neither writer nor
# verifier is substituted; a barrier is not an assertion of power-loss durability.
_DURABLE_CHILD = r"""
import json
import sys
from pathlib import Path
from auto_coder.management import original_attempts as origin
from auto_coder.management.fresh_initialization import claim_fresh_initialization, parse_fresh_intent
from auto_coder.management.fresh_host_observation import serve_one_fresh_grant

intent = parse_fresh_intent(json.loads(Path(sys.argv[1]).read_bytes()))
kind, checkpoint = sys.argv[2].split(":", 1)
index = {"anchor": 0, "claimed": 1, "never-created": 1, "grant-possible": 2}[kind]
target_event = f"{index:08d}.json"
real_create = origin._create
real_replace = origin.os.replace
seen_event = False

def stop_at(point):
    if point == checkpoint:
        print("BARRIER:" + point, flush=True)
        sys.stdin.readline()

def create_at_real_boundary(root, name, value):
    global seen_event
    if name == target_event:
        stop_at("before-event")
        real_create(root, name, value)
        seen_event = True
        stop_at("after-event")
    elif name == "frontier.pending" and seen_event:
        stop_at("before-pending")
        real_create(root, name, value)
        stop_at("after-pending")
    else:
        real_create(root, name, value)

def replace_at_real_boundary(source, destination):
    if seen_event and Path(source).name == "frontier.pending":
        stop_at("before-replace")
        real_replace(source, destination)
        stop_at("after-replace")
    else:
        real_replace(source, destination)

origin._create = create_at_real_boundary
origin.os.replace = replace_at_real_boundary
print("ENTERING", flush=True)
sys.stdin.readline()
if kind == "grant-possible":
    serve_one_fresh_grant(Path(intent.host_state_root) / "fresh-guard.sock", intent, None, None, -1, -1)
else:
    claim_fresh_initialization(Path(intent.host_state_root), intent)
"""


@pytest.mark.parametrize("kind", ["anchor", "claimed", "never-created", "grant-possible"])
@pytest.mark.parametrize("checkpoint", ["before-event", "after-event", "before-pending", "after-pending", "before-replace", "after-replace"])
def test_real_writer_killed_inside_durable_publication(tmp_path, kind, checkpoint, _use_real_commands):
    """Kill actual production admissions around original event/frontier writes."""
    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "original")
    if kind == "never-created":
        intent = replace(intent, oauth_inode=intent.oauth_inode + 1)
    if kind == "grant-possible":
        claim_fresh_initialization(Path(intent.host_state_root), intent)

    payload = tmp_path / "durable-input.json"
    payload.write_text(json.dumps(asdict(intent)))
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", _DURABLE_CHILD, str(payload), f"{kind}:{checkpoint}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert child.stdout.readline().strip() == "ENTERING"
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "BARRIER:" + checkpoint
        child.kill()
        _, stderr = child.communicate(timeout=10)
        assert child.returncode != 0, stderr
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)

    original = _fingerprint(tmp_path)
    root = Path(binding.root)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        if checkpoint in {"after-event", "before-pending", "after-pending", "before-replace"}:
            # Real event bytes have been published without a matching final frontier.
            with pytest.raises(OriginalEvidenceUnavailable, match="partial, omitted or conflicting"):
                read_frontier(root, binding)
            assert evidence.unavailable
            with pytest.raises(OriginalEvidenceUnavailable):
                verify(evidence, binding, fence)
        elif kind == "never-created" and checkpoint == "after-replace":
            # Process death is NOT a power-loss/fsync success oracle.
            assert evidence.attempts[-1].state == "never-created"
        else:
            assert evidence.unavailable
    assert _fingerprint(tmp_path) == original

    if checkpoint in {"after-event", "before-pending", "after-pending", "before-replace"}:
        # Reentry cannot heal a partially published original frontier.
        contender = _covered(tmp_path, binding, "contender")
        prior = _fingerprint(tmp_path)
        with pytest.raises(OriginalEvidenceUnavailable, match="partial"):
            claim_fresh_initialization(Path(contender.host_state_root), contender)
        assert _fingerprint(tmp_path) == prior
        if kind != "grant-possible":
            with pytest.raises(OriginalEvidenceUnavailable):
                serve_one_fresh_grant(tmp_path / "new-guard.sock", intent, None, None, -1, -1)
        assert _fingerprint(tmp_path) == prior

    assert not (tmp_path / "new-guard.sock").exists()
    assert not (Path(intent.host_state_root) / "fresh-guard.sock").exists()
    assert not (Path(intent.host_journal_root) / "management-operations.sqlite3").exists()


_ORDERING_MUTATION_CHILD = r"""
import json
import sys
from pathlib import Path
from auto_coder.management import original_attempts as origin
from auto_coder.management.fresh_initialization import parse_fresh_intent

intent = parse_fresh_intent(json.loads(Path(sys.argv[1]).read_bytes()))
real_create = origin._create
real_replace = origin.os.replace

def omit_publication(root, name, value):
    if name == "frontier.pending":
        return
    real_create(root, name, value)

def omit_frontier_replace(source, destination):
    if Path(source).name != "frontier.pending":
        real_replace(source, destination)

origin._create = omit_publication
origin.os.replace = omit_frontier_replace
with origin.grant_boundary(intent):
    print("MUTATED_GRANT_CROSSED_WITHOUT_FRONTIER", flush=True)
"""


def test_deliberately_reordered_grant_cannot_fool_original_consumer(tmp_path, _use_real_commands):
    """Negative control: a deliberately broken append is not verified success."""
    binding = _history(tmp_path)
    intent = _covered(tmp_path, binding, "original")
    claim_fresh_initialization(Path(intent.host_state_root), intent)
    payload = tmp_path / "mutation-input.json"
    payload.write_text(json.dumps(asdict(intent)))
    result = subprocess.run(
        [sys.executable, "-B", "-c", _ORDERING_MUTATION_CHILD, str(payload)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        timeout=15,
    )
    # The deliberately corrupted writer crossed a nominal grant context; this
    # outcome would be a forbidden false positive if a test trusted its log.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "MUTATED_GRANT_CROSSED_WITHOUT_FRONTIER"

    original = _fingerprint(tmp_path)
    with observe(binding) as fence:
        evidence = derive(binding, fence)
        assert evidence.unavailable == ("partial, omitted or conflicting frontier",)
        with pytest.raises(OriginalEvidenceUnavailable):
            verify(evidence, binding, fence)
    assert _fingerprint(tmp_path) == original
    with pytest.raises(OriginalEvidenceUnavailable, match="partial"):
        claim_fresh_initialization(Path(intent.host_state_root), intent)
    assert _fingerprint(tmp_path) == original
    assert not (Path(intent.host_state_root) / "fresh-guard.sock").exists()
