"""Tests for durable canonical PR blocker ledger (GitHub Issue #2135).

Covers:
- AS-001: Stable identity is not stable wording (REQ-001..005, REQ-009).
- AS-002: A compound historical root is not one Boolean (REQ-002, REQ-004..006).
- AS-003: Evidence disappears and returns (REQ-003, REQ-004, REQ-008, REQ-009).
- AS-004: Contention and stale observation (REQ-007, REQ-008).
- AS-005: Failed persistence cannot appear as progress (REQ-007..010).
- Explicit empty namespace vs missing/corrupted state (REQ-008).
- ReviewerSessionRegistry decoupling (REQ-009).
"""

from __future__ import annotations

import multiprocessing
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from auto_coder.canonical_pr_blocker_ledger import (
    AssociationAmbiguityError,
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerDisposition,
    BlockerLedgerSnapshot,
    BlockerLedgerUnavailableError,
    BlockerPersistenceError,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    EvidenceAvailability,
    IdempotencyConflictError,
    InconsistentScopeAssociationError,
    QualifiedRequirement,
    ReconciliationDecision,
    StaleLedgerRevisionError,
    UnknownBlockerReferenceError,
)
from auto_coder.reviewer_session_registry import ReviewerSession, ReviewerSessionRegistry


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "canonical_pr_blockers.db"


@pytest.fixture()
def ledger(db_path: Path) -> CanonicalPRBlockerLedger:
    return CanonicalPRBlockerLedger(db_path=db_path)


API_ORIGIN = "https://api.github.com"
REPO = "kitamura-tetsuo/auto-coder"
PR_NUMBER = 2135


def test_as001_stable_identity_is_not_stable_wording(ledger: CanonicalPRBlockerLedger, db_path: Path) -> None:
    """AS-001: Create a blocker, reconcile a paraphrased observation at newer head,

    restart store client, change reviewer routing. Same ID and original scope remain.
    A second defect citing the same requirement but different correction remains separate.
    """
    # 1. Initialize namespace
    snapshot = ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    assert snapshot.ledger_revision == 1
    assert len(snapshot.blockers) == 0

    # 2. Admit a blocker through the public admission boundary
    payload1 = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/canonical_pr_blocker_ledger.py",
        incorrect_behavior_or_missing_invariant="Blocker ID is not preserved across heads",
        required_correction_outcome="Blocker ID must be preserved controller-wide",
        evidence_needed="Durable ledger lookup test",
        original_objective_anchor="Give each PR review blocker a durable identity",
        accepted_scope=CorrectionScope(
            description="Ensure blocker identity is stable",
            concern_ids=("concern-1",),
        ),
        evidence="Observed ID regenerated after new commit",
        reviewed_head_sha="head-1",
        reviewed_base_sha="base-0",
        review_attempt_id="att-1",
        observation_identity="obs-1",
    )
    blocker_id1, snapshot = ledger.admit_blocker(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-admit-1",
        expected_ledger_revision=1,
        payload=payload1,
    )
    assert blocker_id1.startswith("blk_")
    assert snapshot.ledger_revision == 2
    assert len(snapshot.blockers) == 1
    b1 = snapshot.get_blocker(blocker_id1)
    assert b1 is not None
    assert b1.original_objective_anchor == "Give each PR review blocker a durable identity"
    assert b1.accepted_scope.description == "Ensure blocker identity is stable"
    assert b1.disposition == BlockerDisposition.OPEN

    # 3. Paraphrased observation at a newer head from another reviewer routing
    paraphrased_candidate = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/canonical_pr_blocker_ledger.py",
        incorrect_behavior_or_missing_invariant="Completely different wording: ID drifts when git HEAD advances",
        required_correction_outcome="Different wording: Maintain stable identifier",
        evidence_needed="Regression test on commit change",
        original_objective_anchor="Give each PR review blocker a durable identity",
        accepted_scope=CorrectionScope(
            description="Paraphrased scope wording",
            concern_ids=("concern-1",),
        ),
        evidence="Second observation from Claude Opus",
        reviewed_head_sha="head-2",
        reviewed_base_sha="base-0",
        review_attempt_id="att-2",
        observation_identity="obs-2",
    )
    assoc_id, snapshot = ledger.reconcile_observation(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-reconcile-1",
        expected_ledger_revision=2,
        candidate_payload=paraphrased_candidate,
        blocker_ids_considered=(blocker_id1,),
        decision=ReconciliationDecision.ASSOCIATE,
        associated_blocker_id=blocker_id1,
        evidence="Human or analyzer matched defect semantics",
        review_observation_identity="obs-2",
    )
    assert assoc_id == blocker_id1
    assert snapshot.ledger_revision == 3
    # The original scope is preserved, NOT replaced by the paraphrased wording
    b1_after = snapshot.get_blocker(blocker_id1)
    assert b1_after is not None
    assert b1_after.accepted_scope.description == "Ensure blocker identity is stable"
    assert len(b1_after.reconciliations) == 1
    assert b1_after.reconciliations[0].observation_identity == "obs-2"

    # 4. Restart store client and change reviewer routing
    restarted_ledger = CanonicalPRBlockerLedger(db_path=db_path)
    reloaded_snapshot = restarted_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert reloaded_snapshot.ledger_revision == 3
    b1_reloaded = reloaded_snapshot.get_blocker(blocker_id1)
    assert b1_reloaded is not None
    assert b1_reloaded.blocker_id == blocker_id1
    assert b1_reloaded.accepted_scope.description == "Ensure blocker identity is stable"

    # 5. A second defect with the same qualified requirement but different correction remains separate
    payload2 = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-001"),),
        authoritative_boundary="src/auto_coder/canonical_pr_blocker_ledger.py",
        incorrect_behavior_or_missing_invariant="Namespace ignores custom GitHub Enterprise API origin",
        required_correction_outcome="Incorporate API origin into namespace key",
        evidence_needed="Custom origin test",
        original_objective_anchor="Give each PR review blocker a durable identity",
        accepted_scope=CorrectionScope(
            description="API origin support in namespace",
            concern_ids=("concern-origin",),
        ),
        evidence="Origin mismatch in multi-host deployment",
        reviewed_head_sha="head-2",
        reviewed_base_sha="base-0",
        review_attempt_id="att-3",
        observation_identity="obs-3",
    )
    blocker_id2, snapshot2 = restarted_ledger.reconcile_observation(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-reconcile-distinct",
        expected_ledger_revision=3,
        candidate_payload=payload2,
        blocker_ids_considered=(blocker_id1,),
        decision=ReconciliationDecision.DISTINCT_DEFECT,
        evidence="Distinct defect: origin handling vs head sha handling",
        review_observation_identity="obs-3",
    )
    assert blocker_id2 != blocker_id1
    assert snapshot2.ledger_revision == 4
    assert len(snapshot2.blockers) == 2
    # Both cite REQ-001 but have distinct blocker IDs
    req_blockers = snapshot2.get_blockers_by_requirement(2135, "REQ-001")
    assert len(req_blockers) == 2
    assert {b.blocker_id for b in req_blockers} == {blocker_id1, blocker_id2}


def test_as002_compound_historical_root_is_not_one_boolean(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-002: Import an authenticated root owning corrections A and B.

    Record current correction evidence for A only. A resolves without making B
    or root resolved. Recurrence of A uses A's original ID and preserves history.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    root_alias = BlockerAlias(
        alias_type="github_root_comment",
        alias_value="comment-99999",
    )

    # Blocker A
    payload_a = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-002"),),
        authoritative_boundary="module_a.py",
        incorrect_behavior_or_missing_invariant="Defect A",
        required_correction_outcome="Correction outcome A",
        evidence_needed="Test A",
        accepted_scope=CorrectionScope("Scope A", ("concern-a",)),
        aliases=(root_alias,),
    )
    id_a, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-admit-a", 1, payload_a)

    # Blocker B
    payload_b = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-002"),),
        authoritative_boundary="module_b.py",
        incorrect_behavior_or_missing_invariant="Defect B",
        required_correction_outcome="Correction outcome B",
        evidence_needed="Test B",
        accepted_scope=CorrectionScope("Scope B", ("concern-b",)),
        aliases=(root_alias,),
    )
    id_b, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-admit-b", 2, payload_b)

    # Check root alias owns both blockers
    root_blockers = snapshot.get_blockers_for_alias("github_root_comment", "comment-99999")
    assert len(root_blockers) == 2
    assert {b.blocker_id for b in root_blockers} == {id_a, id_b}

    # Record current correction evidence for A only
    snapshot = ledger.record_transition(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-resolve-a",
        expected_ledger_revision=3,
        blocker_id=id_a,
        target_disposition=BlockerDisposition.VERIFIED_CORRECTION,
        evidence="Passing test for A at head-commit-2",
        transition_reason="Verified fix for A",
    )
    b_a = snapshot.get_blocker(id_a)
    b_b = snapshot.get_blocker(id_b)
    assert b_a is not None and b_a.disposition == BlockerDisposition.VERIFIED_CORRECTION
    # B remains OPEN!
    assert b_b is not None and b_b.disposition == BlockerDisposition.OPEN

    # Root comment is NOT resolved as a whole:
    open_blockers = snapshot.get_open_blockers()
    assert len(open_blockers) == 1
    assert open_blockers[0].blocker_id == id_b

    # Recurrence of A uses A's original ID and preserves history
    snapshot = ledger.record_transition(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-recurrence-a",
        expected_ledger_revision=4,
        blocker_id=id_a,
        target_disposition=BlockerDisposition.RECURRENCE,
        evidence="Demonstrated regression in test_a on rebase head-commit-3",
        transition_reason="Demonstrated recurrence of defect A",
    )
    b_a_reoccurred = snapshot.get_blocker(id_a)
    assert b_a_reoccurred is not None
    assert b_a_reoccurred.blocker_id == id_a  # Reuses original blocker ID
    assert b_a_reoccurred.disposition == BlockerDisposition.RECURRENCE
    # History is preserved
    assert len(b_a_reoccurred.transitions) == 3
    assert b_a_reoccurred.transitions[0].to_disposition == BlockerDisposition.OPEN
    assert b_a_reoccurred.transitions[1].to_disposition == BlockerDisposition.VERIFIED_CORRECTION
    assert b_a_reoccurred.transitions[2].to_disposition == BlockerDisposition.RECURRENCE


def test_as003_evidence_disappears_and_returns(ledger: CanonicalPRBlockerLedger) -> None:
    """AS-003: Observe open A, unavailable evidence, then previously known evidence.

    Neither absence nor rediscovery creates a new blocker or resolves it.
    Requirement manifest change is recorded as versioned reconciliation need.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload_a = BlockerAdmissionPayload(
        category="REGRESSION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-003"),),
        authoritative_boundary="boundary_x.py",
        incorrect_behavior_or_missing_invariant="Regression X",
        required_correction_outcome="Fix X",
        evidence_needed="Coverage X",
        accepted_scope=CorrectionScope("Scope X", ("concern-x",)),
    )
    id_a, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-admit-x", 1, payload_a)
    assert snapshot.get_blocker(id_a).disposition == BlockerDisposition.OPEN

    # Current evidence is UNAVAILABLE (e.g. CI failed or run missing)
    snapshot = ledger.record_evidence(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-ev-unavail",
        expected_ledger_revision=2,
        blocker_id=id_a,
        evidence_availability=EvidenceAvailability.UNAVAILABLE,
        evidence="CI runner timed out; test results unavailable",
    )
    # Blocker remains OPEN!
    b_a = snapshot.get_blocker(id_a)
    assert b_a.disposition == BlockerDisposition.OPEN

    # Current evidence is OMITTED in a subsequent report
    snapshot = ledger.record_evidence(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-ev-omitted",
        expected_ledger_revision=3,
        blocker_id=id_a,
        evidence_availability=EvidenceAvailability.OMITTED,
        evidence="Report omitted this check",
    )
    assert snapshot.get_blocker(id_a).disposition == BlockerDisposition.OPEN
    assert len(snapshot.blockers) == 1

    # Rediscovery: same previously known evidence
    snapshot = ledger.record_evidence(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-ev-known",
        expected_ledger_revision=4,
        blocker_id=id_a,
        evidence_availability=EvidenceAvailability.KNOWN,
        evidence="Re-observed failure in tests/test_boundary_x.py",
    )
    # Still single blocker, still OPEN, ID preserved
    assert len(snapshot.blockers) == 1
    assert snapshot.blockers[0].blocker_id == id_a
    assert snapshot.blockers[0].disposition == BlockerDisposition.OPEN

    # Requirement manifest changes: recorded as versioned reconciliation need, not silent scope rewrite
    snapshot = ledger.record_requirement_manifest_change(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-manifest-chg",
        expected_ledger_revision=5,
        blocker_id=id_a,
        manifest_revision="manifest-v2-sha",
        reconciliation_need="REQ-003 text updated in issue body",
    )
    b_a_manifest = snapshot.get_blocker(id_a)
    assert b_a_manifest.accepted_scope.description == "Scope X"  # Scope was NOT rewritten
    assert len(b_a_manifest.reconciliation_needs) == 1
    assert "manifest-v2-sha" in b_a_manifest.reconciliation_needs[0]


def _worker_attempt_transition(
    db_path_str: str,
    operation_id: str,
    target_disposition_val: str,
    expected_rev: int,
    entry_barrier: multiprocessing.Barrier,
    release_barrier: multiprocessing.Barrier,
    out_queue: multiprocessing.Queue,
    is_first: bool,
) -> None:
    ledger = CanonicalPRBlockerLedger(db_path=Path(db_path_str))
    # Both wait at entry_barrier establishing both observed expected_rev
    entry_barrier.wait()

    if not is_first:
        # Wait until first worker has committed its transition
        release_barrier.wait()

    try:
        snapshot = ledger.record_transition(
            api_origin="https://api.github.com",
            repository="kitamura-tetsuo/auto-coder",
            pr_number=2135,
            operation_id=operation_id,
            expected_ledger_revision=expected_rev,
            blocker_id="blk_contested",
            target_disposition=BlockerDisposition(target_disposition_val),
            evidence=f"From worker {operation_id}",
        )
        out_queue.put(("SUCCESS", snapshot.ledger_revision))
    except StaleLedgerRevisionError as exc:
        out_queue.put(("STALE", str(exc)))
    except Exception as exc:
        out_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))
    finally:
        if is_first:
            # First worker has completed commit, release second worker
            release_barrier.wait()


def test_as004_contention_and_stale_observation(db_path: Path) -> None:
    """AS-004: Two processes and a barrier establish contention on same observed revision.

    Commit one transition before releasing the other. The stale transition is rejected
    and the first remains intact. Replaying identical operation ID returns original result;
    replaying with different payload is rejected.
    """
    ledger = CanonicalPRBlockerLedger(db_path=db_path)
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # Admit initial blocker with fixed ID for test
    payload = BlockerAdmissionPayload(
        category="TEST_ORACLE",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-007"),),
        authoritative_boundary="storage.py",
        incorrect_behavior_or_missing_invariant="Race condition",
        required_correction_outcome="CAS safety",
        evidence_needed="Contention test",
        accepted_scope=CorrectionScope("CAS scope"),
    )
    # Use admit_blocker
    blocker_id, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-admit-race", 1, payload)
    observed_revision = snapshot.ledger_revision
    assert observed_revision == 2

    # Manually rename blocker_id in db to 'blk_contested' for deterministic worker target
    conn = ledger._connect()
    conn.execute("UPDATE blockers SET blocker_id = 'blk_contested' WHERE blocker_id = ?", (blocker_id,))
    conn.execute("UPDATE blocker_requirements SET blocker_id = 'blk_contested' WHERE blocker_id = ?", (blocker_id,))
    conn.execute("UPDATE blocker_transitions SET blocker_id = 'blk_contested' WHERE blocker_id = ?", (blocker_id,))
    conn.close()

    entry_barrier = multiprocessing.Barrier(2)
    release_barrier = multiprocessing.Barrier(2)
    queue = multiprocessing.Queue()

    p1 = multiprocessing.Process(
        target=_worker_attempt_transition,
        args=(
            str(db_path),
            "op-trans-p1",
            BlockerDisposition.VERIFIED_CORRECTION.value,
            observed_revision,
            entry_barrier,
            release_barrier,
            queue,
            True,
        ),
    )
    p2 = multiprocessing.Process(
        target=_worker_attempt_transition,
        args=(
            str(db_path),
            "op-trans-p2",
            BlockerDisposition.AUTHORIZED_INVALIDATION.value,
            observed_revision,
            entry_barrier,
            release_barrier,
            queue,
            False,
        ),
    )

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = [queue.get(), queue.get()]
    status_types = [r[0] for r in results]
    assert "SUCCESS" in status_types
    assert "STALE" in status_types

    # First write remains intact
    snapshot_after = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert snapshot_after.ledger_revision == 3
    b_contested = snapshot_after.get_blocker("blk_contested")
    assert b_contested is not None
    assert b_contested.disposition == BlockerDisposition.VERIFIED_CORRECTION

    # Replaying identical operation ID returns original result
    snapshot_replay = ledger.record_transition(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-trans-p1",
        expected_ledger_revision=999,  # ignored because of idempotency replay
        blocker_id="blk_contested",
        target_disposition=BlockerDisposition.VERIFIED_CORRECTION,
        evidence="From worker op-trans-p1",
    )
    assert snapshot_replay.ledger_revision == 3

    # Replaying with conflicting payload is rejected
    with pytest.raises(IdempotencyConflictError):
        ledger.record_transition(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id="op-trans-p1",
            expected_ledger_revision=999,
            blocker_id="blk_contested",
            target_disposition=BlockerDisposition.AUTHORIZED_INVALIDATION,  # altered payload!
            evidence="Conflicting payload",
        )


def test_as005_failed_persistence_cannot_appear_as_progress(ledger: CanonicalPRBlockerLedger, db_path: Path) -> None:
    """AS-005: Fail a required durable write, reconstruct public store client.

    No uncommitted identity/disposition is exposed.
    Corrupt or remove required retained state and verify explicit unavailable result
    rather than a newly initialized empty ledger.
    """
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)

    # Simulate failure before commit
    ledger._simulate_failure_before_commit = True
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-008"),),
        authoritative_boundary="boundary_f.py",
        incorrect_behavior_or_missing_invariant="Fail persistence",
        required_correction_outcome="Fail closed",
        evidence_needed="Crash test",
        accepted_scope=CorrectionScope("Crash scope"),
    )

    with pytest.raises(BlockerPersistenceError):
        ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-failing", 1, payload)

    # Reconstruct public store client
    reconstructed_ledger = CanonicalPRBlockerLedger(db_path=db_path)
    snapshot = reconstructed_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    # The failed write left no uncommitted identity or state
    assert len(snapshot.blockers) == 0
    assert snapshot.ledger_revision == 1

    # Remove required retained state (e.g. non-existent namespace or missing DB)
    with pytest.raises(BlockerLedgerUnavailableError) as exc_missing:
        reconstructed_ledger.get_snapshot(API_ORIGIN, REPO, 9999, require_retained_state=True)
    assert "Required retained state is missing" in str(exc_missing.value)

    # Corrupt retained state (write garbage bytes to database file)
    db_path.write_bytes(b"NOT A VALID SQLITE DATABASE FILE - CORRUPTED")
    corrupt_ledger = CanonicalPRBlockerLedger(db_path=db_path)
    with pytest.raises(BlockerLedgerUnavailableError) as exc_corrupt:
        corrupt_ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER, require_retained_state=True)
    assert "corrupt" in str(exc_corrupt.value).lower() or "unreadable" in str(exc_corrupt.value).lower()


def test_immutable_snapshots_prevent_mutation(ledger: CanonicalPRBlockerLedger) -> None:
    """REQ-009: Verify snapshots are immutable frozen dataclasses."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-009"),),
        authoritative_boundary="test.py",
        incorrect_behavior_or_missing_invariant="Mutating snapshot",
        required_correction_outcome="Frozen",
        evidence_needed="Frozen test",
        accepted_scope=CorrectionScope("Frozen scope"),
    )
    bid, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-frozen", 1, payload)
    blocker = snapshot.get_blocker(bid)
    assert blocker is not None

    with pytest.raises(FrozenInstanceError):
        blocker.disposition = BlockerDisposition.VERIFIED_CORRECTION  # type: ignore

    with pytest.raises(FrozenInstanceError):
        snapshot.ledger_revision = 999  # type: ignore


def test_contract_rebinding_record(ledger: CanonicalPRBlockerLedger) -> None:
    """REQ-003: Explicit contract-rebinding records appended separately."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-003"),),
        authoritative_boundary="test.py",
        incorrect_behavior_or_missing_invariant="Scope expansion needed",
        required_correction_outcome="Rebind",
        evidence_needed="Rebinding test",
        accepted_scope=CorrectionScope("Original scope", ("c1",)),
    )
    bid, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-rebind-orig", 1, payload)

    snapshot = ledger.record_contract_rebinding(
        API_ORIGIN,
        REPO,
        PR_NUMBER,
        operation_id="op-rebind",
        expected_ledger_revision=2,
        blocker_id=bid,
        revised_scope=CorrectionScope("Expanded scope", ("c1", "c2")),
        reason="Approved requirement amendment",
        manifest_revision="manifest-v3",
    )
    b = snapshot.get_blocker(bid)
    assert b is not None
    # Original accepted scope is preserved
    assert b.accepted_scope.description == "Original scope"
    # Transition history contains contract rebinding
    rebind_trans = [t for t in b.transitions if t.contract_rebinding is not None]
    assert len(rebind_trans) == 1
    assert rebind_trans[0].contract_rebinding.revised_scope.description == "Expanded scope"
    assert rebind_trans[0].contract_rebinding.reason == "Approved requirement amendment"


def test_reconciliation_validations(ledger: CanonicalPRBlockerLedger) -> None:
    """REQ-005: Reject unknown/cross-PR references, inconsistent scopes, and ambiguous matches."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-005"),),
        authoritative_boundary="valid.py",
        incorrect_behavior_or_missing_invariant="Bad association",
        required_correction_outcome="Reject bad association",
        evidence_needed="Validation test",
        accepted_scope=CorrectionScope("Valid scope"),
    )
    bid, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-valid", 1, payload)

    # 1. Unknown blocker ID in considered list
    with pytest.raises(UnknownBlockerReferenceError):
        ledger.reconcile_observation(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id="op-bad-ref",
            expected_ledger_revision=2,
            candidate_payload=payload,
            blocker_ids_considered=("blk_nonexistent",),
            decision=ReconciliationDecision.ASSOCIATE,
            associated_blocker_id="blk_nonexistent",
        )

    # 2. Inconsistent category
    inconsistent_candidate = BlockerAdmissionPayload(
        category="TEST_ORACLE",  # Existing is SPECIFICATION
        authoritative_boundary="valid.py",
        accepted_scope=CorrectionScope("Inconsistent candidate"),
    )
    with pytest.raises(InconsistentScopeAssociationError):
        ledger.reconcile_observation(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id="op-inconsistent",
            expected_ledger_revision=2,
            candidate_payload=inconsistent_candidate,
            blocker_ids_considered=(bid,),
            decision=ReconciliationDecision.ASSOCIATE,
            associated_blocker_id=bid,
        )

    # 3. Ambiguous decision
    with pytest.raises(AssociationAmbiguityError):
        ledger.reconcile_observation(
            API_ORIGIN,
            REPO,
            PR_NUMBER,
            operation_id="op-ambiguous",
            expected_ledger_revision=2,
            candidate_payload=payload,
            blocker_ids_considered=(bid,),
            decision=ReconciliationDecision.AMBIGUOUS,
        )


def test_reviewer_session_registry_decoupling(ledger: CanonicalPRBlockerLedger, tmp_path: Path) -> None:
    """REQ-009: Reading or closing a reviewer session must not mutate or delete the blocker ledger."""
    ledger.initialize_namespace(API_ORIGIN, REPO, PR_NUMBER)
    payload = BlockerAdmissionPayload(
        category="SPECIFICATION",
        qualified_requirements=(QualifiedRequirement(issue_number=2135, requirement_id="REQ-009"),),
        authoritative_boundary="session.py",
        incorrect_behavior_or_missing_invariant="Session deletion",
        required_correction_outcome="Decoupled",
        evidence_needed="Registry decoupling test",
        accepted_scope=CorrectionScope("Decoupled scope"),
    )
    bid, snapshot = ledger.admit_blocker(API_ORIGIN, REPO, PR_NUMBER, "op-decoupled", 1, payload)

    # Create and manipulate ReviewerSessionRegistry
    session_reg_path = tmp_path / "reviewer_sessions.json"
    registry = ReviewerSessionRegistry(path=session_reg_path)
    session = ReviewerSession(
        repository=REPO,
        pr_number=PR_NUMBER,
        backend_name="codex",
        backend_type="codex",
        model_name="gpt",
        session_id="session-123",
        last_head_sha="head-1",
    )
    registry.save(session)
    assert registry.get(REPO, PR_NUMBER, "codex", "codex", "gpt") is not None

    # Remove PR from ReviewerSessionRegistry
    registry.remove_pr(REPO, PR_NUMBER)
    assert registry.get(REPO, PR_NUMBER, "codex", "codex", "gpt") is None

    # Canonical blocker ledger is completely unaffected!
    snapshot_after = ledger.get_snapshot(API_ORIGIN, REPO, PR_NUMBER)
    assert len(snapshot_after.blockers) == 1
    assert snapshot_after.blockers[0].blocker_id == bid
    assert snapshot_after.blockers[0].accepted_scope.description == "Decoupled scope"
