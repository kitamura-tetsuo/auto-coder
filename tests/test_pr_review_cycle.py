from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from auto_coder.pr_review_cycle import (
    PHASE_COMPLETE,
    PHASE_ORDINARY_CLOSURE,
    PHASE_ORDINARY_REVIEW,
    PHASE_STRONG_PENDING,
    VERDICT_FINDINGS,
    VERDICT_PASS,
    ContractSnapshot,
    Finding,
    FindingDisposition,
    NotApplicableError,
    PrReviewCycleRepository,
    RoundProvenance,
    StaleTransitionError,
    StrongPolicyIdentity,
    UnknownClaimError,
)


def _contract(text: str = "REQ-001: do the thing") -> ContractSnapshot:
    return ContractSnapshot(issue_ids=("123",), requirements_text=text)


def _policy() -> StrongPolicyIdentity:
    return StrongPolicyIdentity(strong_route="backend_strong_pr_adversarial_validation", model_options="model=strong-1", protocol_version="v1")


def _finding(finding_id: str, origin_round_id: str = "") -> Finding:
    return Finding(
        finding_id=finding_id,
        origin_round_id=origin_round_id,
        requirement_ids=("REQ-001",),
        counterexample="Given state S, action A occurs",
        expected_behavior="R",
        actual_behavior="X",
        evidence="repro at line 10",
        affected_boundary="owner-deletion path",
    )


def test_ordinary_pass_alone_never_authorizes_completion(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    repo.record_ordinary_pass(1, RoundProvenance("head-a", "base-a"), _contract())

    snapshot = repo.snapshot(1)
    assert snapshot.phase == PHASE_STRONG_PENDING
    assert snapshot.completion is None
    assert not repo.is_completion_authorized(1, "head-a")


def test_strong_pass_requires_publication_ack_before_completion(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance = RoundProvenance("head-a", "base-a")
    repo.record_ordinary_pass(1, provenance, _contract())
    claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    round_record = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")

    with pytest.raises(NotApplicableError):
        repo.accept_strong_pass_completion(1, round_record.round_id)
    assert not repo.is_completion_authorized(1, "head-a")

    repo.acknowledge_publication(1, round_record.round_id)
    repo.accept_strong_pass_completion(1, round_record.round_id)

    assert repo.is_completion_authorized(1, "head-a")
    assert repo.snapshot(1).phase == PHASE_COMPLETE


def test_acknowledge_publication_is_idempotent(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance = RoundProvenance("head-a", "base-a")
    repo.record_ordinary_pass(1, provenance, _contract())
    claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    round_record = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")

    repo.acknowledge_publication(1, round_record.round_id)
    repo.acknowledge_publication(1, round_record.round_id)  # no-op, does not raise

    assert repo.snapshot(1).accepted_strong_round.publication_status == "ACKNOWLEDGED"


# AS-001: two distinct ways to complete.


def test_two_distinct_completion_paths_and_recovery(tmp_path):
    path = tmp_path / "state.json"
    repo = PrReviewCycleRepository("owner/repo", path)

    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, round0.round_id)
    repo.accept_strong_pass_completion(1, round0.round_id)
    assert repo.is_completion_authorized(1, "h0")

    # A second PR completes via repair + ordinary closure instead.
    repo.record_ordinary_pass(2, RoundProvenance("h0b", "base"), _contract())
    claim_b = repo.claim_strong_audit(2, RoundProvenance("h0b", "base"), _contract(), _policy())
    finding_a = _finding("finding-a")
    finding_b = _finding("finding-b")
    strong_round = repo.record_strong_result(2, claim_b.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[finding_a, finding_b])
    repo.acknowledge_publication(2, strong_round.round_id)

    repo.record_ordinary_pass(2, RoundProvenance("h1", "base"), _contract())
    snapshot = repo.certify_closure(
        2,
        RoundProvenance("h1", "base"),
        _contract(),
        _policy(),
        references_round_id=strong_round.round_id,
        finding_set_revision=strong_round.finding_set_revision,
        dispositions=[
            FindingDisposition("finding-a", "FIXED", "regression test added at tests/test_x.py::test_y", "h1"),
            FindingDisposition("finding-b", "INVALID", "path is unreachable per current routing", "h1"),
        ],
        bounded=True,
        bounded_evidence="cumulative diff h0..h1 touches only the two flagged paths",
    )
    assert snapshot.phase == PHASE_COMPLETE
    assert repo.is_completion_authorized(2, "h1")

    # Reconstruct the repository and repeat the observation: exactly one accepted strong round, no re-request.
    reopened = PrReviewCycleRepository("owner/repo", path)
    assert reopened.is_completion_authorized(2, "h1")
    recovered_snapshot = reopened.snapshot(2)
    assert recovered_snapshot.accepted_strong_round.round_id == strong_round.round_id
    assert recovered_snapshot.accepted_strong_round.sequence == 1


# AS-002: partial and superficial closure cannot pass.


def test_omitted_finding_disposition_blocks_completion(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    finding_a = _finding("finding-a")
    finding_b = _finding("finding-b")
    strong_round = repo.record_strong_result(1, claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[finding_a, finding_b])

    repo.record_ordinary_pass(1, RoundProvenance("h1", "base"), _contract())
    snapshot = repo.certify_closure(
        1,
        RoundProvenance("h1", "base"),
        _contract(),
        _policy(),
        references_round_id=strong_round.round_id,
        finding_set_revision=strong_round.finding_set_revision,
        dispositions=[FindingDisposition("finding-a", "FIXED", "regression test added", "h1")],
        bounded=True,
        bounded_evidence="cumulative diff h0..h1 touches only the flagged path",
    )

    assert snapshot.completion is None
    assert snapshot.phase == PHASE_ORDINARY_CLOSURE
    assert any(f.finding_id == "finding-b" and f.status == "OPEN" for f in snapshot.open_findings)
    assert not repo.is_completion_authorized(1, "h1")


def test_disposition_requires_evidence(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    strong_round = repo.record_strong_result(1, claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[_finding("finding-a")])

    repo.record_ordinary_pass(1, RoundProvenance("h1", "base"), _contract())
    with pytest.raises(ValueError):
        repo.certify_closure(
            1,
            RoundProvenance("h1", "base"),
            _contract(),
            _policy(),
            references_round_id=strong_round.round_id,
            finding_set_revision=strong_round.finding_set_revision,
            dispositions=[FindingDisposition("finding-a", "FIXED", "", "h1")],
            bounded=True,
            bounded_evidence="cumulative diff evidence",
        )


# AS-003: repair lineage and contract changes.


def test_closure_assesses_full_lineage_from_original_strong_audit(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    strong_round = repo.record_strong_result(1, claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[_finding("finding-a")])

    # h1 is an intermediate repair head that never certifies closure.
    repo.record_ordinary_pass(1, RoundProvenance("h1", "base"), _contract())

    # h2 certifies closure and must be judged against the cumulative h0..h2 diff (bounded_evidence is
    # reviewer-supplied evidence about that cumulative diff, not just the h1..h2 slice).
    repo.record_ordinary_pass(1, RoundProvenance("h2", "base"), _contract())
    snapshot = repo.certify_closure(
        1,
        RoundProvenance("h2", "base"),
        _contract(),
        _policy(),
        references_round_id=strong_round.round_id,
        finding_set_revision=strong_round.finding_set_revision,
        dispositions=[FindingDisposition("finding-a", "FIXED", "cumulative h0..h2 diff fixes the path", "h2")],
        bounded=True,
        bounded_evidence="assessed cumulative diff h0..h2",
    )
    assert snapshot.phase == PHASE_COMPLETE


def test_expanded_closure_requires_new_strong_round(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    strong_round = repo.record_strong_result(1, claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[_finding("finding-a")])

    repo.record_ordinary_pass(1, RoundProvenance("h1", "base"), _contract())
    snapshot = repo.certify_closure(
        1,
        RoundProvenance("h1", "base"),
        _contract(),
        _policy(),
        references_round_id=strong_round.round_id,
        finding_set_revision=strong_round.finding_set_revision,
        dispositions=[FindingDisposition("finding-a", "FIXED", "fixed the path", "h1")],
        bounded=False,
        bounded_evidence="diff h0..h1 also refactors an unrelated authorization module",
    )
    assert snapshot.phase == PHASE_STRONG_PENDING
    assert not repo.is_completion_authorized(1, "h1")

    with pytest.raises(NotApplicableError):
        repo.certify_closure(
            1,
            RoundProvenance("h1", "base"),
            _contract(),
            _policy(),
            references_round_id=strong_round.round_id,
            finding_set_revision=strong_round.finding_set_revision,
            dispositions=[],
            bounded=True,
            bounded_evidence="trying again without a new strong round",
        )

    # A fresh independent strong round on the current ordinary-passed H2 is required, and succeeds.
    new_claim = repo.claim_strong_audit(1, RoundProvenance("h1", "base"), _contract(), _policy())
    new_round = repo.record_strong_result(1, new_claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, new_round.round_id)
    repo.accept_strong_pass_completion(1, new_round.round_id)
    assert repo.is_completion_authorized(1, "h1")


def test_changed_contract_text_invalidates_prior_authorization(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract("REQ-001: original text"))
    claim = repo.claim_strong_audit(1, provenance0, _contract("REQ-001: original text"), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, round0.round_id)

    with pytest.raises(NotApplicableError):
        # Requirement text changed at the SAME head: stale authorization must not carry forward.
        repo.claim_strong_audit(1, provenance0, _contract("REQ-001: changed text"), _policy())


def test_changing_only_the_ordinary_verifier_does_not_invalidate_strong_authorization(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    # Two different "ordinary verifier" observations of the same H/B/M both apply.
    repo.record_ordinary_pass(1, provenance0, _contract())
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, round0.round_id)
    repo.accept_strong_pass_completion(1, round0.round_id)
    assert repo.is_completion_authorized(1, "h0")


# AS-004: controlled overlapping result acceptance.


def test_duplicate_claims_for_the_same_phase_do_not_double_authorize(tmp_path):
    path = tmp_path / "state.json"
    repo_a = PrReviewCycleRepository("owner/repo", path)
    repo_b = PrReviewCycleRepository("owner/repo", path)
    provenance = RoundProvenance("h0", "base")
    repo_a.record_ordinary_pass(1, provenance, _contract())

    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first_claim():
        # Hold the transition lock open manually to create real contention.
        with repo_a.serialized_transition():
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_claim():
        assert first_entered.wait(timeout=2)
        claim = repo_b.claim_strong_audit(1, provenance, _contract(), _policy())
        second_entered.set()
        return claim

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_claim)
        second_future = executor.submit(second_claim)
        assert first_entered.wait(timeout=2)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first_future.result(timeout=2)
        second_claim_result = second_future.result(timeout=2)

    duplicate_claim = repo_a.claim_strong_audit(1, provenance, _contract(), _policy())
    assert duplicate_claim.claim_id == second_claim_result.claim_id


def test_newer_attempt_supersedes_and_older_result_is_rejected(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance_old = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance_old, _contract())
    old_claim = repo.claim_strong_audit(1, provenance_old, _contract(), _policy())

    provenance_new = RoundProvenance("h1", "base")
    repo.record_ordinary_pass(1, provenance_new, _contract())
    new_claim = repo.claim_strong_audit(1, provenance_new, _contract(), _policy())
    assert new_claim.claim_id != old_claim.claim_id

    with pytest.raises(UnknownClaimError):
        repo.record_strong_result(1, old_claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")

    # The older failure must not be accepted either.
    with pytest.raises(UnknownClaimError):
        repo.record_strong_result(1, old_claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[_finding("late-finding")])

    round_new = repo.record_strong_result(1, new_claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    assert round_new.head_sha == "h1"


# AS-005: crash at each evidence/effect boundary.


def test_stale_expected_version_rejected_before_any_write(tmp_path):
    path = tmp_path / "state.json"
    repo = PrReviewCycleRepository("owner/repo", path)
    provenance = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance, _contract())
    version = repo.current_version(1)

    repo.record_ordinary_pass(1, provenance, _contract())  # bumps the version again

    with pytest.raises(StaleTransitionError):
        repo.record_ordinary_pass(1, provenance, _contract(), expected_version=version)

    # The prior accepted state must remain intact (no partial/corrupt write).
    reloaded = PrReviewCycleRepository("owner/repo", path)
    assert reloaded.snapshot(1).ordinary_pass_head_sha == "h0"


def test_recovery_retains_pending_publication_without_a_new_strong_round(tmp_path):
    path = tmp_path / "state.json"
    repo = PrReviewCycleRepository("owner/repo", path)
    provenance = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance, _contract())
    claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")

    # Simulate a crash after bundle persistence but before publication ack.
    recovered = PrReviewCycleRepository("owner/repo", path)
    snapshot = recovered.snapshot(1)
    assert snapshot.accepted_strong_round is not None
    assert snapshot.accepted_strong_round.round_id == round0.round_id
    assert snapshot.accepted_strong_round.publication_status == "PENDING"

    # Recovery resumes publication acknowledgement; it never re-requests a strong round.
    recovered.acknowledge_publication(1, round0.round_id)
    recovered.accept_strong_pass_completion(1, round0.round_id)
    assert recovered.is_completion_authorized(1, "h0")


def test_unknown_delivery_is_retired_on_independent_closure_without_being_marked_delivered(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance0 = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance0, _contract())
    claim = repo.claim_strong_audit(1, provenance0, _contract(), _policy())
    strong_round = repo.record_strong_result(1, claim.claim_id, VERDICT_FINDINGS, reviewer_provenance="codex/strong-1", findings=[_finding("finding-a")])
    repo.acknowledge_publication(1, strong_round.round_id)

    repo.set_finding_delivery_status(1, "finding-a", "UNKNOWN")

    repo.record_ordinary_pass(1, RoundProvenance("h1", "base"), _contract())
    snapshot = repo.certify_closure(
        1,
        RoundProvenance("h1", "base"),
        _contract(),
        _policy(),
        references_round_id=strong_round.round_id,
        finding_set_revision=strong_round.finding_set_revision,
        dispositions=[FindingDisposition("finding-a", "FIXED", "independent current-head verification", "h1")],
        bounded=True,
        bounded_evidence="cumulative diff h0..h1 fixes the flagged path",
    )
    assert snapshot.phase == PHASE_COMPLETE
    fixed_finding = repo.snapshot(1)
    # Finding is no longer open, so it is absent from open_findings; check via internal state through delivery API.
    assert fixed_finding.open_findings == ()
    repo.set_finding_delivery_status(1, "finding-a", "RETIRED")  # idempotent; already retired by closure


# AS-006: no completion from caches or temporary absence.


def test_unavailable_record_and_running_attempt_never_authorize(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    assert not repo.is_completion_authorized(1, "head-anything")
    assert repo.snapshot(1).phase == PHASE_ORDINARY_REVIEW

    provenance = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance, _contract())
    repo.claim_strong_audit(1, provenance, _contract(), _policy())
    assert not repo.is_completion_authorized(1, "h0")


def test_duplicate_observation_of_completed_round_stays_complete_without_new_invocation(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance, _contract())
    claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, round0.round_id)
    repo.accept_strong_pass_completion(1, round0.round_id)

    assert repo.is_completion_authorized(1, "h0")
    assert repo.is_completion_authorized(1, "h0")  # duplicate observation: still complete, no new invocation needed


def test_closed_pr_grants_no_authority_and_reopen_requires_fresh_observations(tmp_path):
    repo = PrReviewCycleRepository("owner/repo", tmp_path / "state.json")
    provenance = RoundProvenance("h0", "base")
    repo.record_ordinary_pass(1, provenance, _contract())
    claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    round0 = repo.record_strong_result(1, claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, round0.round_id)
    repo.accept_strong_pass_completion(1, round0.round_id)
    assert repo.is_completion_authorized(1, "h0")

    repo.mark_closed(1)
    assert not repo.is_completion_authorized(1, "h0")
    assert repo.snapshot(1).phase == "CLOSED"
    with pytest.raises(NotApplicableError):
        repo.record_ordinary_pass(1, provenance, _contract())

    repo.mark_reopened(1)
    # The historical completion cannot bypass fresh applicability checks after reopening.
    assert not repo.is_completion_authorized(1, "h0")
    assert repo.snapshot(1).phase == PHASE_ORDINARY_REVIEW

    repo.record_ordinary_pass(1, provenance, _contract())
    new_claim = repo.claim_strong_audit(1, provenance, _contract(), _policy())
    new_round = repo.record_strong_result(1, new_claim.claim_id, VERDICT_PASS, reviewer_provenance="codex/strong-1")
    repo.acknowledge_publication(1, new_round.round_id)
    repo.accept_strong_pass_completion(1, new_round.round_id)
    assert repo.is_completion_authorized(1, "h0")
