"""Regression coverage for cross-head recovered changed-file evidence."""

import json
from unittest.mock import MagicMock, patch

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationResult,
    EvidenceRecoveryEntry,
    IssueRequirement,
    _build_recovery_ledger,
    _reconcile_reusable_recovered_evidence,
    build_adversarial_validation_context,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import (
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)
from auto_coder.reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, ReviewerSessionRegistry
from auto_coder.util.github_action import GitHubActionsStatusResult


def _entry(path: str, identity: str, *, status: str = "RECOVERED") -> RecoveredFileEvidence:
    return RecoveredFileEvidence(
        path=path,
        source="GitHub REST recovery",
        status=status,
        evidence="verified via authoritative record",
        requirement_ids=["REQ-001"],
        identity_version=1,
        change_identity=identity,
        requirement_manifest_identity="manifest-1",
        origin_head_sha="head-1",
        origin_validation_snapshot="snapshot-1",
        scope_basis_identity="basis-1" if status == "IRRELEVANT" else "",
    )


def _session(*entries: RecoveredFileEvidence) -> ReviewerSession:
    return ReviewerSession(
        repository="owner/repo",
        pr_number=2049,
        backend_name="reviewer",
        backend_type="codex",
        model_name="strong",
        session_id="session",
        evidence_head_sha="head-1",
        recovered_file_evidence=list(entries),
    )


def test_cross_head_reuse_keeps_only_equivalent_recovered_file() -> None:
    stored = _session(_entry("src/a.py", "old-a"), _entry("src/b.py", "same-b"))
    context = AdversarialValidationContext(
        unverified_files=["src/a.py", "src/b.py"],
        file_change_identities={"src/a.py": "new-a", "src/b.py": "same-b"},
        requirement_manifest_identity="manifest-1",
    )

    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), stored, context, "head-2")

    assert [(entry.path, entry.provenance, entry.origin_head_sha) for entry in result.evidence_recovery] == [("src/b.py", "REUSED_EQUIVALENT", "head-1")]


def test_manifest_change_irrelevance_and_legacy_entries_fail_closed() -> None:
    stored = _session(
        _entry("src/recovered.py", "same"),
        _entry("src/irrelevant.py", "same", status="IRRELEVANT"),
        RecoveredFileEvidence(path="src/legacy.py", status="RECOVERED"),
    )
    context = AdversarialValidationContext(
        unverified_files=["src/recovered.py", "src/irrelevant.py", "src/legacy.py"],
        file_change_identities={path: "same" for path in ("src/recovered.py", "src/irrelevant.py", "src/legacy.py")},
        requirement_manifest_identity="manifest-2",
    )

    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), stored, context, "head-2")

    assert result.evidence_recovery == []


def _context_from_rest_record(
    record: dict[str, object],
    *,
    raw_path: str = "docs/readme.md",
    additional_records: tuple[dict[str, object], ...] = (),
) -> AdversarialValidationContext:
    client = MagicMock()
    client.get_pr_diff.return_value = f"diff --git a/{raw_path} b/{raw_path}\n+++ b/{raw_path}\n+current\n"
    client.get_pr_changed_file_count.return_value = 2 + len(additional_records)
    client.get_pr_changed_files.return_value = [
        {"filename": "docs/readme.md", "status": "modified", "sha": "docs", "additions": 1, "deletions": 0, "changes": 1, "patch": "+docs"},
        record,
        *additional_records,
    ]
    issue = MagicMock(spec=["title", "body"])
    issue.title = "Equivalent recovery"
    issue.body = "REQ-001: Preserve behavior."
    client.get_issue.return_value = issue
    client.get_parent_issue_details.return_value = None
    return build_adversarial_validation_context(
        "owner/repo",
        {"number": 2049, "title": "Reuse", "body": "Closes #2049", "head_sha": "head", "base_sha": "base"},
        AutomationConfig(),
        github_client=client,
    )


def test_realistic_head_qualified_urls_do_not_invalidate_complete_change(tmp_path) -> None:
    base_record = {
        "filename": "src/a.py",
        "status": "modified",
        "sha": "blob-a",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new",
        "blob_url": "https://github.com/o/r/blob/head-1/src/a.py",
        "raw_url": "https://github.com/o/r/raw/head-1/src/a.py",
        "contents_url": "https://api.github.com/repos/o/r/contents/src/a.py?ref=head-1",
    }
    first = _context_from_rest_record(base_record)
    second_record = dict(base_record)
    for key in ("blob_url", "raw_url", "contents_url"):
        second_record[key] = str(second_record[key]).replace("head-1", "head-2")
    second = _context_from_rest_record(second_record)
    assert first.file_change_identities["src/a.py"] == second.file_change_identities["src/a.py"]

    changed_base_record = dict(second_record)
    changed_base_record["patch"] = "@@ -1 +1 @@\n-different-old\n+new"
    changed_base = _context_from_rest_record(changed_base_record)
    assert first.file_change_identities["src/a.py"] != changed_base.file_change_identities["src/a.py"]

    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    persisted = _entry("src/a.py", first.file_change_identities["src/a.py"])
    persisted.requirement_manifest_identity = first.requirement_manifest_identity
    registry.save(_session(persisted))
    reloaded = registry.get("owner/repo", 2049, "reviewer", "codex", "strong")
    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), reloaded, second, "head-2")
    assert [(entry.path, entry.provenance) for entry in result.evidence_recovery] == [("src/a.py", "REUSED_EQUIVALENT")]


def test_incomplete_rest_record_never_creates_reusable_change_identity() -> None:
    incomplete = {
        "filename": "src/a.py",
        "status": "modified",
        "sha": "same-head-blob",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        # No patch or base-side object identity is available.
    }
    first = _context_from_rest_record(incomplete)
    second = _context_from_rest_record(incomplete)
    assert "src/a.py" not in first.file_change_identities

    persisted = _entry("src/a.py", "")
    persisted.requirement_manifest_identity = first.requirement_manifest_identity
    result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), _session(persisted), second, "head-2")
    assert result.evidence_recovery == []


def test_ledger_preserves_invalidation_and_readjudication_provenance(tmp_path) -> None:
    prior = _entry("src/a.py", "old-change")
    stored = _session(prior)
    context = AdversarialValidationContext(
        unverified_files=["src/a.py"],
        file_change_identities={"src/a.py": "new-change"},
        requirement_manifest_identity="manifest-2",
        validation_snapshot="snapshot-2",
    )
    invalidated = _build_recovery_ledger(AdversarialValidationResult(result="INCONCLUSIVE"), stored, context, "head-2")
    assert (invalidated[0].status, invalidated[0].disposition, invalidated[0].origin_validation_snapshot) == (
        "INVALIDATED",
        "INVALIDATED",
        "snapshot-1",
    )

    readjudicated_result = AdversarialValidationResult(
        result="PASS",
        evidence_recovery=[
            # A current reviewer decision, not a historical verdict, supplies this entry.
            EvidenceRecoveryEntry(path="src/a.py", source="current recovery", status="RECOVERED", evidence="new evidence", requirement_ids=["REQ-001"])
        ],
    )
    readjudicated = _build_recovery_ledger(readjudicated_result, stored, context, "head-2")
    assert (readjudicated[0].disposition, readjudicated[0].previous_origin_head_sha, readjudicated[0].origin_head_sha) == (
        "READJUDICATED",
        "head-1",
        "head-2",
    )
    replacement = _session(*readjudicated)
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    registry.save(replacement)
    assert registry.get("owner/repo", 2049, "reviewer", "codex", "strong") == replacement


def test_recovery_survives_raw_diff_coverage_and_is_reusable_when_omitted_again(tmp_path) -> None:
    a_record = {
        "filename": "src/a.py",
        "status": "modified",
        "sha": "blob-a",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new",
    }
    b_record = {
        "filename": "src/b.py",
        "status": "modified",
        "sha": "blob-b",
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "patch": "+b",
    }
    h1 = _context_from_rest_record(a_record, additional_records=(b_record,))
    prior = _entry("src/a.py", h1.file_change_identities["src/a.py"])
    prior.requirement_manifest_identity = h1.requirement_manifest_identity
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    registry.save(_session(prior))

    # H2's raw endpoint now covers a.py, while b.py keeps the REST listing
    # active and therefore independently proves a.py's semantic identity.
    h2 = _context_from_rest_record(a_record, raw_path="src/a.py", additional_records=(b_record,))
    loaded_h1 = registry.get("owner/repo", 2049, "reviewer", "codex", "strong")
    h2_result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), loaded_h1, h2, "head-2")
    h2_ledger = _build_recovery_ledger(h2_result, loaded_h1, h2, "head-2")
    registry.save(_session(*h2_ledger))
    assert h2_ledger[0].origin_head_sha == "head-1"
    assert h2_ledger[0].disposition == "REUSED_EQUIVALENT"

    h3 = _context_from_rest_record(a_record, additional_records=(b_record,))
    loaded_h2 = registry.get("owner/repo", 2049, "reviewer", "codex", "strong")
    h3_result = _reconcile_reusable_recovered_evidence(AdversarialValidationResult(result="PASS"), loaded_h2, h3, "head-3")
    assert [(entry.path, entry.origin_head_sha) for entry in h3_result.evidence_recovery] == [("src/a.py", "head-1")]


@patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
@patch("auto_coder.adversarial_validator.run_llm_prompt")
def test_fresh_recovery_is_not_installed_after_snapshot_changes(mock_prompt, mock_context, tmp_path) -> None:
    path = "src/a.py"
    initial = AdversarialValidationContext(
        repo_name="owner/repo",
        pr_number=2049,
        pr_diff="partial diff",
        all_changed_files=[path],
        unverified_files=[path],
        issue_context="REQ-001: Preserve behavior.",
        issue_requirements=[],
        validation_snapshot="snapshot-2",
        file_change_identities={path: "change-2"},
        requirement_manifest_identity="manifest-2",
    )
    advanced = AdversarialValidationContext(validation_snapshot="snapshot-3")
    mock_context.side_effect = [initial, advanced]
    mock_prompt.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "Recovered",
            "evidence_recovery": [{"path": path, "source": "repository", "status": "RECOVERED", "evidence": "complete", "requirement_ids": []}],
            "findings": [],
        }
    )
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session"
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")

    result = run_adversarial_validation(
        "owner/repo",
        {"number": 2049, "head_sha": "head-2"},
        AutomationConfig(),
        backend_manager=manager,
        session_registry=registry,
    )

    assert (result.result, result.diagnostic_category, result.reviewer_session_checkpoint) == (
        "ERROR",
        "validation_snapshot_stale",
        None,
    )
    assert registry.get("owner/repo", 2049, "reviewer", "codex", "strong") is None


@patch("auto_coder.adversarial_validator.run_exact_head_dynamic_check")
@patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
@patch("auto_coder.adversarial_validator.run_llm_prompt")
def test_dynamic_followup_reconfirms_snapshot_before_final_adjudication(mock_prompt, mock_context, mock_dynamic, tmp_path) -> None:
    path = "src/a.py"
    initial = AdversarialValidationContext(
        repo_name="owner/repo",
        pr_number=2049,
        pr_diff="partial diff",
        all_changed_files=[path],
        unverified_files=[path],
        issue_context="REQ-001: Preserve behavior.",
        issue_requirements=[IssueRequirement("REQ-001", "Preserve behavior.")],
        validation_snapshot="snapshot-2",
        file_change_identities={path: "change-2"},
        requirement_manifest_identity="manifest-2",
    )
    mock_context.side_effect = [initial, AdversarialValidationContext(validation_snapshot="snapshot-3")]
    mock_prompt.return_value = json.dumps(
        {
            "result": "INCONCLUSIVE",
            "summary": "Run focused check",
            "dynamic_check_requested": "tests/test_a.py",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "reviewed"}],
            "evidence_recovery": [{"path": path, "source": "repository", "status": "RECOVERED", "evidence": "complete", "requirement_ids": ["REQ-001"]}],
            "findings": [],
        }
    )
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session"
    manager.continue_session.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "Dynamic check passed",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "test passed"}],
            "findings": [],
        }
    )
    mock_dynamic.return_value.success = True
    mock_dynamic.return_value.output = "passed"
    mock_dynamic.return_value.errors = ""
    mock_dynamic.return_value.verification_error = None
    mock_dynamic.return_value.target_selection_error = None
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")

    result = run_adversarial_validation(
        "owner/repo",
        {"number": 2049, "head_sha": "head-2"},
        AutomationConfig(),
        backend_manager=manager,
        session_registry=registry,
    )

    assert result.diagnostic_category == "validation_snapshot_stale"
    assert result.reviewer_session_checkpoint is None


@patch("auto_coder.adversarial_validator.run_exact_head_dynamic_check")
@patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
@patch("auto_coder.adversarial_validator.run_llm_prompt")
def test_zero_selection_correction_cannot_bypass_final_snapshot_confirmation(mock_prompt, mock_context, mock_dynamic, tmp_path) -> None:
    path = "src/a.py"
    initial = AdversarialValidationContext(
        repo_name="owner/repo",
        pr_number=2049,
        pr_diff="partial diff",
        all_changed_files=[path],
        unverified_files=[path],
        issue_context="REQ-001: Preserve behavior.",
        issue_requirements=[IssueRequirement("REQ-001", "Preserve behavior.")],
        validation_snapshot="snapshot-2",
        file_change_identities={path: "change-2"},
        requirement_manifest_identity="manifest-2",
    )
    mock_context.side_effect = [initial, AdversarialValidationContext(validation_snapshot="snapshot-3")]
    mock_prompt.return_value = json.dumps(
        {
            "result": "INCONCLUSIVE",
            "summary": "Run selector",
            "dynamic_check_requested": "tests/test_a.py::test_missing",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "reviewed"}],
            "evidence_recovery": [],
            "findings": [],
        }
    )
    mock_dynamic.return_value.target_selection_error = "pytest selected zero tests"
    manager = MagicMock()
    manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
    manager._last_session_id = "session"
    manager.continue_session.return_value = json.dumps(
        {
            "result": "PASS",
            "summary": "Corrected without another target",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "corrected"}],
            "evidence_recovery": [{"path": path, "source": "repository", "status": "RECOVERED", "evidence": "complete", "requirement_ids": ["REQ-001"]}],
            "findings": [],
        }
    )
    registry = ReviewerSessionRegistry(tmp_path / "sessions.json")
    ci_status = GitHubActionsStatusResult(
        success=True,
        observation=CIObservationSnapshot(
            ObservationSubject("https://api.github.com", "owner/repo", 2049, "head-2"),
            ObservationRequest("github-actions", "checks+workflows"),
            "cycle",
            1,
            ObservationAvailability.KNOWN,
            (WorkflowObservation(WorkflowExecutionIdentity("1", "10", 1), CIConclusion.SUCCESS, workflow_path=".github/workflows/pr-tests.yml"),),
        ),
    )

    result = run_adversarial_validation(
        "owner/repo",
        {"number": 2049, "head_sha": "head-2"},
        AutomationConfig(),
        backend_manager=manager,
        session_registry=registry,
        ci_status=ci_status,
        refresh_ci_status=lambda: ci_status,
    )

    assert result.diagnostic_category == "validation_snapshot_stale"
    assert result.reviewer_session_checkpoint is None
    assert mock_dynamic.call_count == 1
