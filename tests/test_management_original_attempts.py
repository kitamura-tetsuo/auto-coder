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
