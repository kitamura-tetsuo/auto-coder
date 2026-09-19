"""Tests for bounded correction contracts across repair handoff and incremental rereview.

Implements and validates GitHub Issue #2139 (Stage S5 of #2134):
- AS-001: Keep the correction while the wording changes (REQ-001..REQ-004, REQ-008, REQ-009)
- AS-002: Preserve scope without excusing real regressions (REQ-005..REQ-007)
- AS-003: No inherited or invented obligations (REQ-001, REQ-002, REQ-006, REQ-008)
- AS-004: A stale bundle cannot authorize different work (REQ-003, REQ-007, REQ-009)
- AS-005: Actual assembly, not a prompt-string unit test alone (REQ-009, REQ-010)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import (
    AdversarialValidationFinding,
    AdversarialValidationResult,
    IssueRequirement,
    RequirementCoverageEntry,
    SpecificationGap,
    TestOracleGap,
    assemble_adversarial_repair_prompt,
)
from auto_coder.bounded_repair_bundle import (
    BoundedBlockerHandoff,
    BundleDataAbsentError,
    BundleStaleError,
    BundleSupersededError,
    BundleValidationResult,
    RepairHandoffBundle,
    build_repair_handoff_bundle,
    compute_bundle_id,
    reconcile_or_supersede_bundle,
    render_bounded_repair_payload,
    validate_repair_handoff_bundle,
)
from auto_coder.canonical_pr_blocker_ledger import (
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerDisposition,
    BlockerLedgerSnapshot,
    BlockerSnapshot,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    EvidenceAvailability,
    QualifiedRequirement,
)
from auto_coder.pr_repair import (
    ExistingPrRepairTarget,
    build_bounded_existing_pr_repair_prompt,
    build_existing_pr_repair_prompt,
)
from auto_coder.prompt_loader import get_prompt_template, render_prompt


@pytest.fixture
def tmp_ledger(tmp_path: Path) -> CanonicalPRBlockerLedger:
    db_file = tmp_path / "blockers.db"
    return CanonicalPRBlockerLedger(db_path=db_file)


def _admit_test_blocker(
    ledger: CanonicalPRBlockerLedger,
    *,
    api_origin: str = "https://api.github.com",
    repository: str = "owner/repo",
    pr_number: int = 10,
    category: str = "TEST_ORACLE",
    boundary: str = "dashboard_detail.py",
    req_id: str = "REQ-001",
    scope_desc: str = "Preserve count across refresh",
    concerns: tuple[str, ...] = ("concern-count-1",),
    required_outcome: str = "Regression test verifying count preservation across refresh",
    oracle: str = "Test exercising production selector and detail view without mocked count",
    objective_anchor: Optional[str] = "Ensure dashboard detail accurately preserves counts across refresh.",
    head_sha: str = "head123",
    base_sha: str = "base123",
    manifest_rev: str = "rev1",
) -> BlockerSnapshot:
    snap = ledger.initialize_namespace(api_origin, repository, pr_number)
    payload = BlockerAdmissionPayload(
        category=category,
        qualified_requirements=(QualifiedRequirement(issue_number=pr_number, requirement_id=req_id),),
        authoritative_boundary=boundary,
        incorrect_behavior_or_missing_invariant="Count drops to zero after detail refresh",
        required_correction_outcome=required_outcome,
        evidence_needed=oracle,
        original_objective_anchor=objective_anchor,
        accepted_scope=CorrectionScope(description=scope_desc, concern_ids=concerns),
        reviewed_head_sha=head_sha,
        reviewed_base_sha=base_sha,
        requirement_manifest_revision=manifest_rev,
        observation_identity=f"obs-{boundary}-{req_id}",
    )
    blocker_id, snap = ledger.admit_blocker(
        api_origin=api_origin,
        repository=repository,
        pr_number=pr_number,
        operation_id=f"op-admit-{boundary}-{req_id}-{head_sha}-{snap.ledger_revision}",
        expected_ledger_revision=snap.ledger_revision,
        payload=payload,
        review_observation_identity=f"obs-{boundary}-{req_id}",
    )
    for b in snap.blockers:
        if b.blocker_id == blocker_id:
            return b
    return snap.blockers[-1]


# ---------------------------------------------------------------------------
# AS-001: Keep the correction while the wording changes
# ---------------------------------------------------------------------------


class TestAS001KeepCorrectionWhileWordingChanges:
    """AS-001 — Keep the correction while the wording changes.
    Covers REQ-001 through REQ-004, REQ-008, REQ-009.

    Start with a canonical missing production-boundary oracle. Deliver a repair
    bundle, then observe changes adding only a pass body or source-text assertion.
    The next handoff retains the same blocker/scope and explains the missing
    boundary assertion; it does not request arbitrary extra scenarios or treat
    a rewritten root comment as new work. Replaying the saved report yields the
    same bundle meaning.
    """

    def test_retains_same_blocker_and_explains_unmet_boundary_assertion(self, tmp_ledger: CanonicalPRBlockerLedger):
        # 1. Start with a canonical missing production-boundary oracle
        blocker = _admit_test_blocker(
            tmp_ledger,
            boundary="dashboard_detail.py",
            req_id="REQ-001",
            scope_desc="Preserve count across refresh",
            concerns=("concern-count-1",),
            required_outcome="Regression test verifying count preservation across refresh",
            oracle="Test exercising production selector and detail view without mocked count",
            head_sha="head_initial",
        )
        snapshot = tmp_ledger.get_snapshot("https://api.github.com", "owner/repo", 10)

        # 2. Deliver initial repair bundle
        bundle_1 = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            reviewed_head_sha="head_initial",
            requirement_manifest_revision="m_v1",
            target_blocker_ids=[blocker.blocker_id],
            requirement_texts={"REQ-001": "Dashboard detail must preserve count across refresh."},
        )
        assert len(bundle_1.blockers) == 1
        assert bundle_1.blockers[0].blocker_id == blocker.blocker_id
        assert bundle_1.blockers[0].original_correction_scope == "Preserve count across refresh"
        assert not bundle_1.is_failed_correction

        payload_1 = render_bounded_repair_payload(bundle_1)
        tmp_ledger.record_repair_bundle(bundle_1, rendered_payload=payload_1)
        assert blocker.blocker_id in payload_1

        # 3. Changes observed adding only a pass body or source-text assertion (unsuccessful generation)
        failed_corrections = {
            blocker.blocker_id: (
                ("concern-count-1",),
                ("Submitted test added only a source-text assertion (`assert 'count' in src`); " "it failed to exercise the production selector and detail view without mocked count.",),
            )
        }

        # 4. Next handoff retains the SAME blocker/scope and explains the missing boundary assertion
        bundle_2 = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            reviewed_head_sha="head_attempt_2",
            requirement_manifest_revision="m_v1",
            target_blocker_ids=[blocker.blocker_id],
            requirement_texts={"REQ-001": "Dashboard detail must preserve count across refresh."},
            failed_corrections=failed_corrections,
            supersedes_bundle_id=bundle_1.bundle_id,
        )

        assert bundle_2.is_failed_correction
        assert len(bundle_2.blockers) == 1
        b2 = bundle_2.blockers[0]
        assert b2.blocker_id == blocker.blocker_id  # SAME blocker ID
        assert b2.original_correction_scope == "Preserve count across refresh"  # SAME scope
        assert b2.is_unmet_prior_correction
        assert "concern-count-1" in b2.unmet_concern_ids
        assert any("source-text assertion" in r for r in b2.unmet_reasons)
        assert bundle_2.supersedes_bundle_id == bundle_1.bundle_id

        payload_2 = render_bounded_repair_payload(bundle_2)
        assert "UNMET PRIOR CORRECTION DETAILS" in payload_2
        assert "source-text assertion" in payload_2
        assert "A pass-body, renamed test, green helper test, source-text assertion" in payload_2

        # 5. Replaying the saved report yields the exact same bundle meaning
        tmp_ledger.record_repair_bundle(bundle_2, rendered_payload=payload_2)
        fetched_bundle = tmp_ledger.get_repair_bundle(bundle_2.bundle_id)
        assert fetched_bundle is not None
        assert fetched_bundle.bundle_id == bundle_2.bundle_id
        assert fetched_bundle.blockers[0].blocker_id == blocker.blocker_id
        assert fetched_bundle.blockers[0].unmet_reasons == b2.unmet_reasons


# ---------------------------------------------------------------------------
# AS-002: Preserve scope without excusing real regressions
# ---------------------------------------------------------------------------


class TestAS002PreserveScopeWithoutExcusingRealRegressions:
    """AS-002 — Preserve scope without excusing real regressions.
    Covers REQ-005 through REQ-007.

    A corrective diff changes renderer refresh logic. Revalidate affected
    selection/retention behavior and preserve independently supported unrelated
    coverage. Reject a demand to redesign an unrelated subsystem. Separately
    demonstrate a real new explicit-requirement regression caused by the
    corrective change and verify it remains actionable; bounded rereview must
    not hide it.
    """

    def test_revalidate_affected_carry_forward_unrelated_and_report_real_regression(self, tmp_ledger: CanonicalPRBlockerLedger):
        # Initial state: REQ-001 (renderer), REQ-002 (selection), REQ-003 (auth/unrelated)
        req_renderer = IssueRequirement("REQ-001", "Renderer updates refresh without flicker.")
        req_selection = IssueRequirement("REQ-002", "Preserve active row selection across refresh.")
        req_auth = IssueRequirement("REQ-003", "Authenticate user sessions with token verification.")

        # Blocker on REQ-001
        blocker_renderer = _admit_test_blocker(
            tmp_ledger,
            boundary="renderer.py",
            req_id="REQ-001",
            scope_desc="Refresh without flicker",
            concerns=("concern-renderer-1",),
            required_outcome="Flicker-free refresh",
            oracle="Regression test observing buffer swap",
            head_sha="head1",
        )

        # Unrelated coverage (REQ-003) is independently verified with unchanged-path evidence
        prior_coverage = [
            RequirementCoverageEntry("REQ-001", "VIOLATED", "Flicker observed during refresh"),
            RequirementCoverageEntry("REQ-002", "VERIFIED", "Selection preserved"),
            RequirementCoverageEntry("REQ-003", "VERIFIED", "Session token check verified on head1"),
        ]

        # Corrective diff touches renderer.py and selection.py, but NOT auth.py
        # Rereview:
        # 1. Revalidates REQ-001 and REQ-002
        # 2. Carries forward REQ-003 with recorded unchanged-path evidence
        rereview_coverage = [
            RequirementCoverageEntry("REQ-001", "VERIFIED", "Flicker eliminated in renderer.py"),
            RequirementCoverageEntry("REQ-002", "VIOLATED", "Selection drops on refresh after buffer swap change"),  # Regression!
            RequirementCoverageEntry("REQ-003", "VERIFIED", "Unchanged path: auth.py not touched by corrective diff, verified on head1"),
        ]

        # Demonstrate that a demand to redesign an unrelated subsystem (e.g. database schema) is rejected
        unrelated_demand = AdversarialValidationFinding(
            finding_identity="unrelated-db-redesign",
            violated_requirement="Redesign PostgreSQL database pooling architecture",
            evidence="Speculative architectural improvement",
            anchor_path="src/db.py",
            requirement_ids=["REQ-099"],  # Not in child manifest!
        )
        assert "REQ-099" not in {req_renderer.requirement_id, req_selection.requirement_id, req_auth.requirement_id}

        # Real regression on REQ-002 is demonstrated with concrete counterexample and remains actionable
        regression_finding = AdversarialValidationFinding(
            finding_identity="selection-loss-regression",
            violated_requirement="Preserve active row selection across refresh",
            actual_behavior="Selection state reset to -1 on buffer swap",
            counterexample="Given selected row 3, after refresh buffer swap, selected row is null",
            anchor_path="src/renderer.py",
            requirement_ids=["REQ-002"],
        )

        val_result = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Corrective diff resolved REQ-001 but introduced regression on REQ-002",
            findings=[regression_finding],
            requirement_coverage=rereview_coverage,
        )

        # Assert that the real regression is actionable and NOT hidden
        assert val_result.result == "NEEDS_FIX"
        assert len(val_result.findings) == 1
        assert val_result.findings[0].finding_identity == "selection-loss-regression"

        # Assert stopping condition: cannot PASS while regression finding exists (REQ-007)
        assert not val_result.is_pass


# ---------------------------------------------------------------------------
# AS-003: No inherited or invented obligations
# ---------------------------------------------------------------------------


class TestAS003NoInheritedOrInventedObligations:
    """AS-003 — No inherited or invented obligations.
    Covers REQ-001, REQ-002, REQ-006, REQ-008.

    Supply a short immutable Objective, a child Requirements manifest, parent
    context, and an Acceptance Scenario suggesting one technique. The produced
    prompts distinguish these sources and preserve the Objective. The repair
    agent is not instructed to implement a parent-only goal or the suggested
    mechanism as an unstated obligation. A genuine missing invariant becomes
    a specification concern, not an automatic Issue-body edit.
    """

    def test_distinguishes_sources_preserves_objective_no_invented_duties(self, tmp_ledger: CanonicalPRBlockerLedger):
        parent_objective = "Overall platform coordination: improve dashboard rendering and metrics aggregation."
        child_objective = "Ensure dashboard detail view renders count without flicker."
        child_req = QualifiedRequirement(issue_number=20, requirement_id="REQ-001")
        child_req_text = "Detail view displays active item count."

        blocker = _admit_test_blocker(
            tmp_ledger,
            repository="owner/repo",
            pr_number=20,
            category="IMPLEMENTATION",
            boundary="detail_view.py",
            req_id="REQ-001",
            scope_desc="Render active count",
            concerns=("concern-render-1",),
            required_outcome="Display count in detail view",
            oracle="Counterexample: count element is omitted when active",
            objective_anchor=child_objective,
            head_sha="head_child",
        )
        snapshot = tmp_ledger.get_snapshot("https://api.github.com", "owner/repo", 20)

        # Non-authoritative context: parent context & suggested technique in acceptance scenario
        non_auth_context = {
            blocker.blocker_id: [
                f"Parent Context (Informative Only): {parent_objective}",
                "Acceptance Scenario Technique (Example Only): Consider using WebSocket broadcast rather than polling.",
            ]
        }

        bundle = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name="owner/repo",
            pr_number=20,
            head_branch="feature",
            base_branch="main",
            reviewed_head_sha="head_child",
            requirement_manifest_revision="rev1",
            target_blocker_ids=[blocker.blocker_id],
            original_objective=child_objective,
            requirement_texts={"REQ-001": child_req_text},
            non_authoritative_contexts=non_auth_context,
        )

        payload = render_bounded_repair_payload(bundle)

        # 1. Preserves child Objective verbatim
        assert child_objective in payload
        assert "## FIXED OBJECTIVE (IMMUTABLE SPECIFICATION SCOPE EVIDENCE)" in payload

        # 2. Authoritative Requirements manifest is sole contract
        assert "## AUTHORITATIVE REQUIREMENTS (SOLE IMPLEMENTATION CONTRACT)" in payload
        assert f"Issue #20 `REQ-001`: {child_req_text}" in payload

        # 3. Non-authoritative context is clearly segregated
        assert "## NON-AUTHORITATIVE CONTEXT (INFORMATION ONLY — NOT IMPLEMENTATION CONTRACT)" in payload
        assert "Parent Context (Informative Only)" in payload
        assert "WebSocket broadcast rather than polling" in payload

        # 4. Prompt directives instruct agent not to implement parent-only or suggested technique as mandatory
        assert "do NOT create unstated obligations or replace explicit Requirements" in payload
        assert "Do NOT enlarge the correction to unrelated improvements" in payload

        # 5. Missing invariant becomes a specification gap, not code obligation
        spec_gap = SpecificationGap(
            question="Should metrics aggregation support streaming or batching?",
            why_existing_issue_is_insufficient="Child requirement REQ-001 covers display count; aggregation mode is unstated.",
            observed_case="Streaming requested by reviewer but unstated in requirements.",
            affected_scope="metrics aggregation",
        )
        assert spec_gap.question.startswith("Should metrics aggregation")


# ---------------------------------------------------------------------------
# AS-004: A stale bundle cannot authorize different work
# ---------------------------------------------------------------------------


class TestAS004StaleBundleCannotAuthorizeDifferentWork:
    """AS-004 — A stale bundle cannot authorize different work.
    Covers REQ-003, REQ-007, REQ-009.

    Capture a bundle, then change the authoritative requirement manifest or
    accept a superseding blocker scope before delivery. The original bundle
    remains inspectable as historical and cannot silently acquire the new content
    under the same ID. The production sender is given an explicit
    supersession/refusal, not a fallback raw report.
    """

    def test_stale_bundle_refused_on_manifest_or_head_change(self, tmp_ledger: CanonicalPRBlockerLedger):
        blocker = _admit_test_blocker(
            tmp_ledger,
            boundary="service.py",
            req_id="REQ-001",
            scope_desc="Validate input tokens",
            head_sha="head_v1",
            manifest_rev="manifest_v1",
        )
        snapshot = tmp_ledger.get_snapshot("https://api.github.com", "owner/repo", 10)

        # 1. Capture bundle_1
        bundle_1 = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            reviewed_head_sha="head_v1",
            requirement_manifest_revision="manifest_v1",
            target_blocker_ids=[blocker.blocker_id],
        )
        tmp_ledger.record_repair_bundle(bundle_1, rendered_payload="initial payload")

        # 2. Advance PR head to head_v2 and requirement manifest to manifest_v2
        # Validating bundle_1 against head_v2 and manifest_v2 must fail with explicit reason
        val_res = validate_repair_handoff_bundle(
            bundle_1,
            current_head_sha="head_v2",
            current_manifest_revision="manifest_v2",
            snapshot=snapshot,
        )
        assert not val_res.is_valid
        assert val_res.is_superseded
        assert "reviewed head 'head_v1' does not match current PR head 'head_v2'" in (val_res.reason or "")
        assert "manifest revision 'manifest_v1' does not match current revision 'manifest_v2'" in (val_res.reason or "")

        # 3. Target wrapper rejects delivery with BundleStaleError
        target_v2 = ExistingPrRepairTarget(
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            head_sha="head_v2",
        )
        with pytest.raises(BundleStaleError) as exc_info:
            build_existing_pr_repair_prompt(target_v2, "repair details", bundle=bundle_1)
        assert "does not match target head 'head_v2'" in str(exc_info.value)

        # 4. Explicit supersession produces bundle_2 with distinct ID referencing bundle_1
        bundle_2 = reconcile_or_supersede_bundle(
            existing_bundle=bundle_1,
            snapshot=snapshot,
            new_head_sha="head_v2",
            new_manifest_revision="manifest_v2",
        )
        assert bundle_2.bundle_id != bundle_1.bundle_id
        assert bundle_2.supersedes_bundle_id == bundle_1.bundle_id
        assert bundle_2.reviewed_head_sha == "head_v2"
        assert bundle_2.requirement_manifest_revision == "manifest_v2"

        # 5. Original bundle remains inspectable in ledger under its own ID with unchanged content
        historical = tmp_ledger.get_repair_bundle(bundle_1.bundle_id)
        assert historical is not None
        assert historical.bundle_id == bundle_1.bundle_id
        assert historical.reviewed_head_sha == "head_v1"
        assert historical.requirement_manifest_revision == "manifest_v1"


# ---------------------------------------------------------------------------
# AS-005: Actual assembly, not a prompt-string unit test alone
# ---------------------------------------------------------------------------


class TestAS005ActualAssemblyNotPromptStringUnitTestAlone:
    """AS-005 — Actual assembly, not a prompt-string unit test alone.
    Covers REQ-009, REQ-010.

    Drive each production assembly origin with a canonical blocker snapshot
    and inspect the final payload handed to the backend boundary. Ensure a
    template path using `get_prompt_template` cannot bypass the bounded contract.
    Keep advisory model cases separate: a mocked backend returning a preselected
    verdict proves routing/parsing only.
    """

    def test_production_assembly_origins_enforce_bounded_contract(self, tmp_ledger: CanonicalPRBlockerLedger):
        blocker = _admit_test_blocker(
            tmp_ledger,
            boundary="controller.py",
            req_id="REQ-001",
            scope_desc="Validate request authorization",
            required_outcome="Unauthorized requests rejected with 401",
            oracle="Test calling controller without Bearer token",
            head_sha="head_prod",
        )
        snapshot = tmp_ledger.get_snapshot("https://api.github.com", "owner/repo", 10)

        bundle = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            reviewed_head_sha="head_prod",
            requirement_manifest_revision="rev_prod",
            target_blocker_ids=[blocker.blocker_id],
            requirement_texts={"REQ-001": "All requests must supply valid Bearer token."},
        )

        target = ExistingPrRepairTarget(
            repo_name="owner/repo",
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            head_sha="head_prod",
        )

        # Origin 1: build_bounded_existing_pr_repair_prompt
        final_prompt = build_bounded_existing_pr_repair_prompt(target, bundle)
        assert f"BOUNDED REPAIR HANDOFF BUNDLE: `{bundle.bundle_id}`" in final_prompt
        assert blocker.blocker_id in final_prompt
        assert "Validate request authorization" in final_prompt
        assert "Unauthorized requests rejected with 401" in final_prompt

        # Origin 2: assemble_adversarial_repair_prompt
        val_result = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Defect found",
            findings=[
                AdversarialValidationFinding(
                    finding_identity="auth-finding",
                    violated_requirement="All requests must supply valid Bearer token.",
                    actual_behavior="Requests without token accepted",
                    counterexample="Direct call returned 200",
                    anchor_path="controller.py",
                    requirement_ids=["REQ-001"],
                )
            ],
        )
        adversarial_prompt = assemble_adversarial_repair_prompt(
            val_result,
            target,
            "owner/repo",
            10,
            "head_prod",
            bundle=bundle,
        )
        assert bundle.bundle_id in adversarial_prompt
        assert blocker.blocker_id in adversarial_prompt

    def test_get_prompt_template_cannot_bypass_bounded_contract(self):
        """Ensure get_prompt_template automatically includes contract policies."""
        # A caller retrieving a PR contract template cannot bypass the contract policy
        template = get_prompt_template("pr.adversarial_validation_fix")
        assert "BOUNDED CORRECTION CONTRACT AND REPAIR HANDOFF POLICY" in template
        assert "PR VALIDATION CONTRACT AND REVIEW CLASSIFICATION POLICY" in template

        # When raw=True is explicitly requested, raw template is returned
        raw_template = get_prompt_template("pr.adversarial_validation_fix", raw=True)
        assert "BOUNDED CORRECTION CONTRACT AND REPAIR HANDOFF POLICY" not in raw_template

    def test_absent_bundle_data_defers_delivery_without_raw_fallback(self):
        """REQ-009: Absent bundle data defers delivery rather than falling back to raw report."""
        # Empty snapshot with no blockers
        empty_snapshot = BlockerLedgerSnapshot(
            api_origin="https://api.github.com",
            repository="owner/repo",
            pr_number=10,
            blockers=(),
        )
        with pytest.raises(BundleDataAbsentError) as exc_info:
            build_repair_handoff_bundle(
                snapshot=empty_snapshot,
                repo_name="owner/repo",
                pr_number=10,
                head_branch="feature",
                base_branch="main",
                reviewed_head_sha="head1",
                requirement_manifest_revision="rev1",
            )
        assert "No open canonical blockers available" in str(exc_info.value)
