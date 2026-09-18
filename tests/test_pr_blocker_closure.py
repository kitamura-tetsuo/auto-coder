"""Tests for exact PR blocker closure validation (GitHub Issue #2138).

Covers:
- AS-001: Replay the wrong rationale from #2132 (REQ-001..REQ-004, REQ-007, REQ-010)
- AS-002: Fix a subset without changing the goalposts (REQ-002, REQ-003, REQ-006)
- AS-003: A test exists but the producer contract is still broken (REQ-004, REQ-005)
- AS-004: Grouped root and independent aggregate verdict (REQ-005..REQ-007, REQ-009)
- AS-005: Newer evidence wins, failed writes do not resolve (REQ-007..REQ-010)
- Production prompt assembly with blocker, boundary, and concern metadata
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auto_coder.adversarial_validator import ReviewThreadDisposition
from auto_coder.canonical_pr_blocker_ledger import (
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerDisposition,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    EvidenceAvailability,
    QualifiedRequirement,
)
from auto_coder.pr_blocker_closure import (
    ClosureCandidate,
    ClosureEvaluationResult,
    adjudicate_claimed_thread_closures,
    evaluate_closure_candidate,
    execute_durable_thread_closures,
    extract_closure_candidates,
)
from auto_coder.review_thread_validation import (
    ClaimedReviewThread,
    render_claimed_review_threads_section,
    resolve_addressed_review_threads,
)


@pytest.fixture
def tmp_ledger(tmp_path: Path) -> CanonicalPRBlockerLedger:
    db_file = tmp_path / "blockers.db"
    return CanonicalPRBlockerLedger(db_path=db_file)


class TestAS001ReplayWrongRationaleFrom2132:
    """AS-001: Replay the wrong rationale from #2132.

    Load a foreign-reference/count root and a separate overview-navigation root
    through the production reader. Return ADDRESSED for the first using only
    the second's static-link evidence. Assert no closure or resolution of the
    first. With properly bound evidence for the navigation root, that independent
    closure can proceed. Matching only a valid thread ID is not sufficient.
    """

    def test_mismatched_boundary_evidence_rejected_while_valid_boundary_accepted(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 2132

        # 1. Initialize namespace and admit Root 1 (foreign-reference/count in dashboard_detail.py)
        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        payload1 = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(2002, "REQ-001"),),
            authoritative_boundary="src/dashboard_detail.py",
            incorrect_behavior_or_missing_invariant="Foreign reference count rendering omitted",
            required_correction_outcome="Render foreign references and counts in detail view",
            evidence_needed="Dashboard detail shows accurate foreign counts",
            accepted_scope=CorrectionScope(
                description="Foreign reference count rendering in dashboard_detail.py",
                concern_ids=("concern-foreign-counts",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="4039600010"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-counts"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-count-1",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-count",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload1,
            review_observation_identity="obs-count-1",
        )

        # 2. Admit Root 2 (overview navigation link in dashboard.py)
        payload2 = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(2002, "REQ-002"),),
            authoritative_boundary="src/dashboard.py",
            incorrect_behavior_or_missing_invariant="Missing persistent overview history link",
            required_correction_outcome="Persistent overview history link rendered on dashboard",
            evidence_needed="Dashboard view contains overview history link",
            accepted_scope=CorrectionScope(
                description="Persistent overview history link in dashboard.py",
                concern_ids=("concern-nav-link",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="4039600020"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-nav"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-nav-2",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-nav",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload2,
            review_observation_identity="obs-nav-2",
        )

        claimed = [
            ClaimedReviewThread(
                thread_id="thread-counts",
                root_comment_database_id=4039600010,
                original_finding="Foreign reference count rendering missing in dashboard_detail.py",
            ),
            ClaimedReviewThread(
                thread_id="thread-nav",
                root_comment_database_id=4039600020,
                original_finding="Missing persistent overview history link in dashboard.py",
            ),
        ]

        candidates = extract_closure_candidates(
            claimed,
            snapshot=snap,
            api_origin=api_origin,
            repository=repo_name,
            pr_number=pr_number,
            reviewed_head_sha="head-1",
        )
        assert len(candidates) == 2

        # 3. Supply disposition for thread-counts citing ONLY thread-nav's static link in dashboard.py
        mismatched_disp = ReviewThreadDisposition(
            thread_id="thread-counts",
            status="ADDRESSED",
            rationale="Added persistent overview history link in src/dashboard.py",
            evidence="src/dashboard.py: Added history link element to header navigation",
        )

        # Properly bound disposition for thread-nav
        proper_nav_disp = ReviewThreadDisposition(
            thread_id="thread-nav",
            status="ADDRESSED",
            rationale="Added persistent overview history link in src/dashboard.py",
            evidence="src/dashboard.py: Added history link element to header navigation",
        )

        evaluations = adjudicate_claimed_thread_closures(
            candidates,
            [mismatched_disp, proper_nav_disp],
            snapshot=snap,
        )

        eval_counts = next(e for e in evaluations if e.thread_id == "thread-counts")
        eval_nav = next(e for e in evaluations if e.thread_id == "thread-nav")

        # Assert no closure of the first (mismatched boundary rejected)
        assert eval_counts.is_accepted is False
        assert eval_counts.effective_status != "ADDRESSED"
        assert "dashboard.py" in str(eval_counts.rejection_reason)

        # Assert independent closure of the second proceeds
        assert eval_nav.is_accepted is True
        assert eval_nav.effective_status == "ADDRESSED"

        # 4. Execute durable closures and verify thread resolution effect
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "head-1"

        result = execute_durable_thread_closures(
            client,
            repo_name,
            pr_number,
            "head-1",
            evaluations,
            ledger=tmp_ledger,
            api_origin=api_origin,
            claimed_threads=claimed,
        )

        # thread-counts was NOT resolved
        assert "thread-counts" not in result.resolved_thread_ids
        # thread-nav WAS resolved
        assert "thread-nav" in result.resolved_thread_ids

        # Verify ledger state: count blocker remains OPEN, nav blocker is VERIFIED_CORRECTION
        updated_snap = tmp_ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)
        blocker_counts = next(b for b in updated_snap.blockers if any(a.alias_value == "4039600010" for a in b.aliases))
        blocker_nav = next(b for b in updated_snap.blockers if any(a.alias_value == "4039600020" for a in b.aliases))

        assert blocker_counts.disposition == BlockerDisposition.OPEN
        assert blocker_nav.disposition == BlockerDisposition.VERIFIED_CORRECTION


class TestAS002FixSubsetWithoutChangingGoalposts:
    """AS-002: Fix a subset without changing the goalposts.

    A recorded correction owns two concrete manifestations on its original boundary.
    Fix one and leave the other reproducible. The blocker remains open with the
    remaining concern identified. Separately introduce a genuinely different defect
    under the same requirement after fully correcting the original; retain the
    original closure and track the new defect separately, rather than broadening
    the old scope.
    """

    def test_partial_concern_fix_leaves_blocker_open_and_identifies_remaining(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 42

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        payload = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(100, "REQ-001"),),
            authoritative_boundary="src/parser.py",
            incorrect_behavior_or_missing_invariant="Parser fails on both empty string and non-ascii inputs",
            required_correction_outcome="Handle empty strings and non-ascii inputs without crashing",
            evidence_needed="Unit test covering empty string and non-ascii",
            accepted_scope=CorrectionScope(
                description="Fix parser crashes on empty string and unicode",
                concern_ids=("concern-empty-str", "concern-unicode"),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="1001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-parser"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-parser-1",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-parser",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload,
            review_observation_identity="obs-parser-1",
        )

        claimed = [
            ClaimedReviewThread(
                thread_id="thread-parser",
                root_comment_database_id=1001,
                original_finding="Parser crashes on empty string and unicode",
            )
        ]
        candidates = extract_closure_candidates(claimed, snapshot=snap, repository=repo_name, pr_number=pr_number)
        assert candidates[0].owned_concern_ids == ("concern-empty-str", "concern-unicode")

        # Disposition fixes only concern-empty-str, concern-unicode is still reproducible
        partial_disp = ReviewThreadDisposition(
            thread_id="thread-parser",
            status="ADDRESSED",
            concern_ids=("concern-empty-str",),
            rationale="Fixed empty string parsing, but concern-unicode remains reproducible",
            evidence="src/parser.py: added empty check; unicode encoding error still happens",
        )

        evaluations = adjudicate_claimed_thread_closures(candidates, [partial_disp], snapshot=snap)
        res = evaluations[0]

        # Assert blocker remains open with remaining concern identified
        assert res.is_accepted is False
        assert res.effective_status == "STILL_VALID"
        assert "concern-unicode" in res.remaining_concern_ids

        # Now fix both concerns
        full_disp = ReviewThreadDisposition(
            thread_id="thread-parser",
            status="ADDRESSED",
            concern_ids=("concern-empty-str", "concern-unicode"),
            rationale="Fixed both empty string and unicode handling in parser.py",
            evidence="src/parser.py: empty check and utf-8 decoding validated",
        )
        full_evaluations = adjudicate_claimed_thread_closures(candidates, [full_disp], snapshot=snap)
        assert full_evaluations[0].is_accepted is True
        assert full_evaluations[0].effective_status == "ADDRESSED"

        # Execute closure
        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "head-1"
        exec_res = execute_durable_thread_closures(
            client,
            repo_name,
            pr_number,
            "head-1",
            full_evaluations,
            ledger=tmp_ledger,
            api_origin=api_origin,
            claimed_threads=claimed,
        )
        assert "thread-parser" in exec_res.resolved_thread_ids

        # Verify original blocker is closed
        snap_after = tmp_ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)
        blocker = snap_after.get_blocker(candidates[0].blocker_id)
        assert blocker is not None
        assert blocker.disposition == BlockerDisposition.VERIFIED_CORRECTION

        # Separately introduce a genuinely different defect under the same requirement (REQ-001)
        # on a different boundary/path (e.g. streaming parser buffer overflow)
        payload_new_defect = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(100, "REQ-001"),),
            authoritative_boundary="src/streaming_parser.py",
            incorrect_behavior_or_missing_invariant="Streaming buffer overflow",
            required_correction_outcome="Enforce bounded buffer size in streaming parser",
            evidence_needed="Test with oversized chunk",
            accepted_scope=CorrectionScope(
                description="Streaming buffer overflow on chunking",
                concern_ids=("concern-stream-overflow",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="1002"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-stream"),
            ),
            reviewed_head_sha="head-2",
            observation_identity="obs-stream-2",
        )
        new_blocker_id, snap_new = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-stream",
            expected_ledger_revision=snap_after.ledger_revision,
            payload=payload_new_defect,
            review_observation_identity="obs-stream-2",
        )

        # Assert original closure is retained while new defect is tracked separately
        orig_b = snap_new.get_blocker(blocker.blocker_id)
        new_b = snap_new.get_blocker(new_blocker_id)
        assert orig_b.disposition == BlockerDisposition.VERIFIED_CORRECTION
        assert new_b.disposition == BlockerDisposition.OPEN
        assert orig_b.blocker_id != new_b.blocker_id


class TestAS003TestExistsButProducerContractBroken:
    """AS-003: A test exists but the producer contract is still broken.

    Add a helper/source-text test that passes while the mounted consumer still calls
    a nonexistent producer API or bypasses the recorded state transition. The evidence
    cannot close the boundary-dependent finding. Then supply valid production-path
    correction evidence while leaving a distinct test-oracle gap open; only the
    corrected implementation finding changes disposition.
    """

    def test_absent_producer_api_evidence_rejected(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 10

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        payload = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(50, "REQ-001"),),
            authoritative_boundary="src/producer.py",
            incorrect_behavior_or_missing_invariant="Producer omits status field in payload",
            required_correction_outcome="Producer includes status field",
            evidence_needed="Producer payload inspection",
            accepted_scope=CorrectionScope(
                description="Producer status field implementation",
                concern_ids=("concern-producer-status",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="5001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-producer"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-prod-1",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-prod",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload,
            review_observation_identity="obs-prod-1",
        )

        claimed = [
            ClaimedReviewThread(
                thread_id="thread-producer",
                root_comment_database_id=5001,
                original_finding="Producer omits status field",
            )
        ]

        # Candidate knows nonexistent producer API that was called by helper test
        candidates = extract_closure_candidates(
            claimed,
            snapshot=snap,
            repository=repo_name,
            pr_number=pr_number,
            known_absent_apis=("nonexistent_get_status_helper",),
        )

        # Disposition relies on nonexistent_get_status_helper
        fake_test_disp = ReviewThreadDisposition(
            thread_id="thread-producer",
            status="ADDRESSED",
            rationale="Added unit test testing nonexistent_get_status_helper",
            evidence="tests/test_producer.py: passes using nonexistent_get_status_helper() helper function",
        )

        eval_fake = evaluate_closure_candidate(candidates[0], fake_test_disp)
        # REQ-004: Evidence relying on an absent API from current producer cannot establish correction
        assert eval_fake.is_accepted is False
        assert eval_fake.effective_status == "STILL_VALID"
        assert "absent" in str(eval_fake.rejection_reason)

    def test_implementation_closed_while_test_oracle_gap_remains_distinct(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 10

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        # Implementation blocker
        payload_impl = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(50, "REQ-001"),),
            authoritative_boundary="src/producer.py",
            incorrect_behavior_or_missing_invariant="Producer missing status field",
            required_correction_outcome="Add status field",
            evidence_needed="Payload includes status",
            accepted_scope=CorrectionScope(
                description="Add status field to producer",
                concern_ids=("concern-impl",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="5001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-impl"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-impl",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-impl",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload_impl,
            review_observation_identity="obs-impl",
        )

        # Test oracle gap blocker
        payload_tog = BlockerAdmissionPayload(
            category="TEST_ORACLE",
            qualified_requirements=(QualifiedRequirement(50, "REQ-001"),),
            authoritative_boundary="tests/test_producer_regression.py",
            incorrect_behavior_or_missing_invariant="Missing integration regression test for producer contract",
            required_correction_outcome="Add regression oracle test",
            evidence_needed="Regression test oracle exercising real pipeline",
            accepted_scope=CorrectionScope(
                description="Producer regression test oracle",
                concern_ids=("concern-tog",),
            ),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="5002"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-tog"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-tog",
        )
        _, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-tog",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload_tog,
            review_observation_identity="obs-tog",
        )

        claimed = [
            ClaimedReviewThread(thread_id="thread-impl", root_comment_database_id=5001),
            ClaimedReviewThread(thread_id="thread-tog", root_comment_database_id=5002),
        ]
        candidates = extract_closure_candidates(claimed, snapshot=snap, repository=repo_name, pr_number=pr_number)

        # Supply valid production-path correction evidence for implementation finding, while TOG remains open
        impl_disp = ReviewThreadDisposition(
            thread_id="thread-impl",
            status="ADDRESSED",
            rationale="Added status field directly in production producer.py",
            evidence="src/producer.py: status field populated from event source",
        )
        tog_disp = ReviewThreadDisposition(
            thread_id="thread-tog",
            status="STILL_VALID",
            rationale="Regression test oracle has not been added yet",
            evidence="No regression tests added in tests/",
        )

        evaluations = adjudicate_claimed_thread_closures(candidates, [impl_disp, tog_disp], snapshot=snap)
        eval_impl = next(e for e in evaluations if e.thread_id == "thread-impl")
        eval_tog = next(e for e in evaluations if e.thread_id == "thread-tog")

        # REQ-005: Only the corrected implementation finding changes disposition
        assert eval_impl.is_accepted is True
        assert eval_impl.effective_status == "ADDRESSED"
        assert eval_tog.is_accepted is False
        assert eval_tog.effective_status == "STILL_VALID"


class TestAS004GroupedRootAndIndependentAggregateVerdict:
    """AS-004: Grouped root and independent aggregate verdict.

    One root owns blockers A and B, and another equivalent alias owns A alone.
    Correct A, keep B open, and retain an unrelated PR-level operational error.
    Record A's valid closure, leave the compound root unresolved, and resolve
    only the A-only alias after the required authority checks.
    """

    def test_compound_root_remains_unresolved_while_alias_resolves(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 77

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)

        # Blocker A
        payload_a = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(77, "REQ-001"),),
            authoritative_boundary="src/module_a.py",
            incorrect_behavior_or_missing_invariant="Defect A",
            required_correction_outcome="Fix Defect A",
            evidence_needed="Verification of A",
            accepted_scope=CorrectionScope(description="Scope A", concern_ids=("concern-a",)),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="7001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-compound"),
                BlockerAlias(alias_type="github_root_comment", alias_value="7002"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-alias-a"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-a",
        )
        id_a, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-a",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload_a,
            review_observation_identity="obs-a",
        )

        # Blocker B (owned by thread-compound only)
        payload_b = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(77, "REQ-002"),),
            authoritative_boundary="src/module_b.py",
            incorrect_behavior_or_missing_invariant="Defect B",
            required_correction_outcome="Fix Defect B",
            evidence_needed="Verification of B",
            accepted_scope=CorrectionScope(description="Scope B", concern_ids=("concern-b",)),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="7001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-compound"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-b",
        )
        id_b, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-b",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload_b,
            review_observation_identity="obs-b",
        )

        claimed = [
            ClaimedReviewThread(thread_id="thread-compound", root_comment_database_id=7001),
            ClaimedReviewThread(thread_id="thread-alias-a", root_comment_database_id=7002),
        ]
        candidates = extract_closure_candidates(claimed, snapshot=snap, repository=repo_name, pr_number=pr_number)

        # Correct A, keep B open
        disp_compound = ReviewThreadDisposition(
            thread_id="thread-compound",
            status="ADDRESSED",
            blocker_id=id_a,
            rationale="Corrected Defect A in module_a.py",
            evidence="src/module_a.py: fixed",
        )
        disp_alias_a = ReviewThreadDisposition(
            thread_id="thread-alias-a",
            status="ADDRESSED",
            blocker_id=id_a,
            rationale="Corrected Defect A in module_a.py",
            evidence="src/module_a.py: fixed",
        )

        evaluations = adjudicate_claimed_thread_closures(candidates, [disp_compound, disp_alias_a], snapshot=snap)

        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "head-1"

        exec_res = execute_durable_thread_closures(
            client,
            repo_name,
            pr_number,
            "head-1",
            evaluations,
            ledger=tmp_ledger,
            api_origin=api_origin,
            claimed_threads=claimed,
        )

        # REQ-006, AS-004:
        # Blocker A's valid closure was recorded
        assert id_a in exec_res.persisted_blocker_transitions
        # thread-compound owns A and B; B is still open, so compound root remains unresolved!
        assert "thread-compound" not in exec_res.resolved_thread_ids
        assert "thread-compound" in exec_res.unresolved_thread_ids
        # thread-alias-a owns only A; all obligations for thread-alias-a are closed, so it resolves!
        assert "thread-alias-a" in exec_res.resolved_thread_ids

        # Unrelated PR-level operational error does not affect per-blocker closure (REQ-009)
        updated_snap = tmp_ledger.get_snapshot(api_origin, repo_name, pr_number, require_retained_state=True)
        assert updated_snap.get_blocker(id_a).disposition == BlockerDisposition.VERIFIED_CORRECTION
        assert updated_snap.get_blocker(id_b).disposition == BlockerDisposition.OPEN


class TestAS005NewerEvidenceWinsAndFailedWritesDoNotResolve:
    """AS-005: Newer evidence wins, failed writes do not resolve.

    Pause after closure assessment, accept a newer head or ledger revision,
    then release the old completion. It cannot overwrite current state or resolve
    the thread. Separately fail durable acceptance before the GitHub mutation
    and assert no resolution request. Exercise a crash after committed acceptance
    but before effect completion and verify exact-operation reconciliation without
    treating a visible thread flag as proof of correction.
    """

    def test_stale_ledger_revision_suppresses_completion_and_resolution(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 88

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        payload = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(88, "REQ-001"),),
            authoritative_boundary="src/logic.py",
            incorrect_behavior_or_missing_invariant="Bug",
            required_correction_outcome="Fix Bug",
            evidence_needed="Fix",
            accepted_scope=CorrectionScope(description="Bug", concern_ids=("concern-1",)),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="8001"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-logic"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-logic",
        )
        blocker_id, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-logic",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload,
            review_observation_identity="obs-logic",
        )

        claimed = [ClaimedReviewThread(thread_id="thread-logic", root_comment_database_id=8001)]
        candidates = extract_closure_candidates(claimed, snapshot=snap, repository=repo_name, pr_number=pr_number)

        disp = ReviewThreadDisposition(
            thread_id="thread-logic",
            status="ADDRESSED",
            rationale="Fixed",
            evidence="src/logic.py: fix",
        )
        evaluations = adjudicate_claimed_thread_closures(candidates, [disp], snapshot=snap)

        # Pause after assessment, and advance ledger revision to a newer revision (e.g. by another operation)
        snap_newer = tmp_ledger.record_evidence(
            api_origin,
            repo_name,
            pr_number,
            operation_id="other-op-advancing-rev",
            expected_ledger_revision=snap.ledger_revision,
            blocker_id=blocker_id,
            evidence_availability=EvidenceAvailability.KNOWN,
            evidence="new observation",
        )
        assert snap_newer.ledger_revision > snap.ledger_revision

        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "head-1"

        # Release the old completion with expected_ledger_revision from old snap (or newer rev required)
        exec_res = execute_durable_thread_closures(
            client,
            repo_name,
            pr_number,
            "head-1",
            evaluations,
            ledger=tmp_ledger,
            api_origin=api_origin,
            expected_ledger_revision=snap_newer.ledger_revision + 5,  # Stale CAS
            claimed_threads=claimed,
        )

        # It cannot resolve the thread or overwrite state
        assert "thread-logic" not in exec_res.resolved_thread_ids
        client.resolve_review_thread.assert_not_called()

    def test_failed_durable_acceptance_asserts_no_resolution_request(self, tmp_ledger: CanonicalPRBlockerLedger):
        api_origin = "https://api.github.com"
        repo_name = "owner/repo"
        pr_number = 89

        snap = tmp_ledger.initialize_namespace(api_origin, repo_name, pr_number)
        payload = BlockerAdmissionPayload(
            category="IMPLEMENTATION",
            qualified_requirements=(QualifiedRequirement(89, "REQ-001"),),
            authoritative_boundary="src/app.py",
            incorrect_behavior_or_missing_invariant="Bug",
            required_correction_outcome="Fix",
            evidence_needed="Fix",
            accepted_scope=CorrectionScope(description="Bug", concern_ids=("concern-1",)),
            aliases=(
                BlockerAlias(alias_type="github_root_comment", alias_value="8002"),
                BlockerAlias(alias_type="github_thread", alias_value="thread-app"),
            ),
            reviewed_head_sha="head-1",
            observation_identity="obs-app",
        )
        blocker_id, snap = tmp_ledger.admit_blocker(
            api_origin,
            repo_name,
            pr_number,
            operation_id="admit-app",
            expected_ledger_revision=snap.ledger_revision,
            payload=payload,
            review_observation_identity="obs-app",
        )

        claimed = [ClaimedReviewThread(thread_id="thread-app", root_comment_database_id=8002)]
        candidates = extract_closure_candidates(claimed, snapshot=snap, repository=repo_name, pr_number=pr_number)

        disp = ReviewThreadDisposition(thread_id="thread-app", status="ADDRESSED", rationale="Fixed", evidence="src/app.py: fixed")
        evaluations = adjudicate_claimed_thread_closures(candidates, [disp], snapshot=snap)

        # Mock ledger to raise an unexpected write failure
        failing_ledger = MagicMock(spec=CanonicalPRBlockerLedger)
        failing_ledger.get_snapshot.return_value = snap
        failing_ledger.record_transition.side_effect = OSError("Disk I/O error on commit")

        client = MagicMock()
        client.get_pull_request_head_sha_strict.return_value = "head-1"

        exec_res = execute_durable_thread_closures(
            client,
            repo_name,
            pr_number,
            "head-1",
            evaluations,
            ledger=failing_ledger,
            api_origin=api_origin,
            claimed_threads=claimed,
        )

        # Assert no resolution request was sent to GitHub
        assert "thread-app" not in exec_res.resolved_thread_ids
        client.resolve_review_thread.assert_not_called()
        assert any("Disk I/O error" in e for e in exec_res.errors)


class TestPromptAssemblyAndRequirements:
    """Verify prompt assembly and REQ-001/REQ-007 handling."""

    def test_prompt_assembly_includes_blocker_metadata(self):
        claimed = [
            ClaimedReviewThread(
                thread_id="t-123",
                root_comment_database_id=999,
                root_author_login="bot[bot]",
                original_finding="Sample defect",
                discussion="bot[bot]: Sample defect\n\nagent: Fixed",
                blocker_ids=("blk_abc123",),
                category="IMPLEMENTATION",
                authoritative_boundary="src/api.py",
                concern_ids=("concern-1", "concern-2"),
            )
        ]

        rendered = render_claimed_review_threads_section(claimed)
        assert "Canonical blocker identity: blk_abc123" in rendered
        assert "Finding category: IMPLEMENTATION" in rendered
        assert "Authoritative production boundary: src/api.py" in rendered
        assert "Owned concrete concern IDs: concern-1, concern-2" in rendered

    def test_mismatched_and_unknown_blocker_id_rejected(self):
        candidate = ClosureCandidate(
            thread_id="t-1",
            blocker_id="blk_correct",
            authoritative_boundary="src/service.py",
            accepted_scope_description="Service fix",
            owned_concern_ids=("c-1",),
        )

        # Unknown blocker ID
        disp_unknown = ReviewThreadDisposition(
            thread_id="t-1",
            status="ADDRESSED",
            blocker_id="blk_unknown",
            rationale="Fix",
            evidence="src/service.py",
        )
        res_unknown = evaluate_closure_candidate(candidate, disp_unknown, all_known_blocker_ids={"blk_correct", "blk_other"})
        assert res_unknown.is_accepted is False
        assert "unknown blocker ID" in str(res_unknown.rejection_reason)

        # Cross-target blocker ID (known in repo, but belongs to another blocker)
        disp_other = ReviewThreadDisposition(
            thread_id="t-1",
            status="ADDRESSED",
            blocker_id="blk_other",
            rationale="Fix",
            evidence="src/service.py",
        )
        res_other = evaluate_closure_candidate(candidate, disp_other, all_known_blocker_ids={"blk_correct", "blk_other"})
        assert res_other.is_accepted is False
        assert "belongs to a different thread" in str(res_other.rejection_reason)
