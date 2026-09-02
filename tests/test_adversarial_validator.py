"""Tests for the adversarial validation module."""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    ADVERSARIAL_RESPONSE_PREVIEW_LIMIT,
    AdversarialValidationContext,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    ChangeProvenanceItem,
    EvidenceRecoveryEntry,
    IssueRequirement,
    RequirementCoverageEntry,
    ReviewThreadDisposition,
    _apply_coverage_and_verdict_precedence,
    build_adversarial_validation_context,
    build_file_aware_diff,
    build_issue_requirement_manifest,
    extract_all_changed_files,
    extract_changed_test_files,
    extract_issue_requirements,
    format_adversarial_review_summary,
    format_change_provenance_clarification,
    is_test_file,
    parse_adversarial_validation_response,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.issue_context import IssueOracleResolution, VerifiedIssueOracle
from auto_coder.prompt_loader import render_prompt
from auto_coder.reviewer_session_registry import ReviewerSession
from auto_coder.trace_logger import get_trace_logger


def _demonstrated_finding_with_anchor(anchor_line: object) -> dict[str, object]:
    return {
        "requirement_id": "REQ-001",
        "finding_identity": "state-update-discard",
        "correction_identity": "test-correction",
        "violated_requirement": "Preserve state",
        "evidence_classification": "DEMONSTRATED",
        "reachability": "The public update entry point reaches this branch",
        "required_behavior": "State must be preserved",
        "actual_behavior": "State is discarded",
        "evidence": "src/state.py executes the destructive branch",
        "counterexample": "Given saved state, when update runs, state is discarded while existing tests only cover empty state",
        "anchor_path": "src/state.py",
        "anchor_line": anchor_line,
        "anchor_side": "RIGHT",
        "anchor_start_line": None,
    }


@pytest.mark.parametrize("anchor_line", [1817, "1817"])
def test_review_anchor_accepts_integer_and_normalizes_decimal_string(anchor_line: object) -> None:
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "summary": "State loss found",
            "findings": [_demonstrated_finding_with_anchor(anchor_line)],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "NEEDS_FIX"
    assert len(result.findings) == 1
    assert result.findings[0].anchor_line == 1817
    assert isinstance(result.findings[0].anchor_line, int)


def test_recovered_changed_file_allows_independently_verified_pass() -> None:
    context = AdversarialValidationContext(
        all_changed_files=["src/state.py"],
        unverified_files=["src/state.py"],
        issue_requirements=[IssueRequirement(requirement_id="REQ-001", text="Preserve state")],
    )
    result = AdversarialValidationResult(
        result="PASS",
        requirement_coverage=[RequirementCoverageEntry(requirement_id="REQ-001", status="VERIFIED", evidence="Current-head focused execution")],
        evidence_recovery=[
            EvidenceRecoveryEntry(
                path="src/state.py",
                source="current-PR retrieval",
                status="RECOVERED",
                evidence="Retrieved and inspected the complete current-head file",
                requirement_ids=["REQ-001"],
            )
        ],
    )

    checked = _apply_coverage_and_verdict_precedence(result, context)

    assert checked.result == "PASS"


def test_evidence_recovery_parser_enforces_deterministic_budget() -> None:
    attempts = [{"path": f"src/{index}.py", "source": "repository inspection", "status": "UNAVAILABLE", "evidence": "not present", "requirement_ids": ["REQ-001"]} for index in range(9)]

    result = parse_adversarial_validation_response(json.dumps({"result": "INCONCLUSIVE", "findings": [], "evidence_recovery": attempts}))

    assert result.result == "ERROR"
    assert result.diagnostic_category == "schema_error"


@pytest.mark.parametrize("anchor_line", ["abc", "0", "-1", 0, -1, 1.5, True])
def test_invalid_review_anchor_returns_structured_schema_error(anchor_line: object) -> None:
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "summary": "State loss found",
            "findings": [_demonstrated_finding_with_anchor(anchor_line)],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "ERROR"
    assert result.diagnostic_category == "schema_error"
    assert result.diagnostic_reason == "review anchor lines must be positive integers"
    assert result.findings == []


def test_parses_aggregated_change_provenance_clarification() -> None:
    response = json.dumps(
        {
            "result": "INCONCLUSIVE",
            "summary": "Requirements pass, but lockfile provenance is unknown",
            "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Implementation and test evidence"}],
            "findings": [],
            "unexplained_changes": [
                {
                    "paths": ["uv.lock", "pyproject.toml"],
                    "change_group": "Dependency metadata",
                    "why_unexplained": "No dependency source change establishes why both files changed",
                }
            ],
            "thread_dispositions": [],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "INCONCLUSIVE"
    assert result.findings == []
    assert result.requirement_coverage[0].status == "VERIFIED"
    assert result.unexplained_changes == [
        ChangeProvenanceItem(
            paths=["uv.lock", "pyproject.toml"],
            change_group="Dependency metadata",
            why_unexplained="No dependency source change establishes why both files changed",
        )
    ]


def test_change_provenance_thread_requests_classification_without_code_change() -> None:
    body = format_change_provenance_clarification([ChangeProvenanceItem(paths=["uv.lock"], change_group="Lockfile", why_unexplained="Generator input is not evident")])

    assert body.count("Auto-Coder change-provenance clarification") == 1
    assert "intentional and directly required" in body
    assert "generated or mechanically derived" in body
    assert "unrelated or accidental" in body
    assert "not an instruction to change code or create a commit" in body
    assert "auto-coder-review-addressed:v1" in body
    assert "unverified claim" in body


def test_complete_requirements_with_unexplained_changes_becomes_clarification_blocker() -> None:
    parsed = parse_adversarial_validation_response(
        json.dumps(
            {
                "result": "PASS",
                "summary": "The Issue contract is satisfied",
                "requirement_coverage": [{"requirement_id": "REQ-001", "status": "VERIFIED", "evidence": "Focused test and implementation path"}],
                "findings": [],
                "unexplained_changes": [{"paths": ["uv.lock"], "change_group": "Lockfile", "why_unexplained": "Its generating change is unclear"}],
            }
        )
    )
    context = AdversarialValidationContext(
        issue_requirements=[IssueRequirement(requirement_id="REQ-001", text="Required behavior")],
        all_changed_files=["uv.lock"],
    )

    result = _apply_coverage_and_verdict_precedence(parsed, context)

    assert result.result == "INCONCLUSIVE"
    assert result.diagnostic_category == "change_provenance_clarification"
    assert result.requirement_coverage[0].status == "VERIFIED"
    assert len(result.unexplained_changes) == 1


@pytest.mark.parametrize(
    ("rationale", "evidence"),
    [
        ("scripts/example.sh was unrelated work accidentally left in the branch", "The implementer identifies the script change as accidental"),
        ("The generated-file claim is contradicted by manual edits", "generated.json changes keys that are absent from generator input and output"),
    ],
)
def test_review_summary_publishes_concrete_still_valid_provenance(rationale: str, evidence: str) -> None:
    result = AdversarialValidationResult(
        result="INCONCLUSIVE",
        summary="Issue requirements remain verified",
        thread_dispositions=[ReviewThreadDisposition(thread_id="provenance-1", status="STILL_VALID", rationale=rationale, evidence=evidence)],
    )

    body = format_adversarial_review_summary(result, "abc123")

    assert "Issue requirements remain verified" in body
    assert "`provenance-1`: STILL_VALID" in body
    assert rationale in body
    assert evidence in body


def test_one_counterexample_compacts_multiple_requirement_perspectives() -> None:
    shared = {
        "finding_identity": "status-handler-lookup-failure",
        "correction_identity": "test-correction",
        "evidence_classification": "DEMONSTRATED",
        "reachability": "The status handler reaches the exception branch",
        "evidence": "handler.py:42 catches and returns",
        "anchor_path": "handler.py",
        "suggested_regression_scenario": "Raise from lookup and assert failed status",
    }
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "findings": [
                {
                    **shared,
                    "requirement_id": "REQ-001",
                    "violated_requirement": "Failures propagate",
                    "required_behavior": "Propagate the fatal lookup failure",
                    "actual_behavior": "Returns success",
                    "counterexample": "Given a lookup error, status must propagate failure but returns success",
                },
                {
                    **shared,
                    "requirement_id": "REQ-002",
                    "reachability": "The public status entry point follows the lookup-error handler",
                    "evidence": "The exception branch at handler.py:42 returns before marking failure",
                    "suggested_regression_scenario": "Make lookup raise and verify failure propagation plus FAILED status",
                    "violated_requirement": "Status distinguishes failure",
                    "required_behavior": "Record FAILED structured status",
                    "actual_behavior": "Leaves the previous SUCCESS status",
                    "counterexample": "Given the same lookup error, status must become FAILED but remains SUCCESS",
                },
            ],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert len(result.findings) == 1
    assert result.findings[0].all_requirement_ids == ["REQ-001", "REQ-002"]
    assert "Failures propagate" in result.findings[0].violated_requirement
    assert "Status distinguishes failure" in result.findings[0].violated_requirement
    assert "Propagate the fatal lookup failure" in result.findings[0].required_behavior
    assert "Record FAILED structured status" in result.findings[0].required_behavior
    assert "Returns success" in result.findings[0].actual_behavior
    assert "Leaves the previous SUCCESS status" in result.findings[0].actual_behavior
    assert "must propagate failure" in result.findings[0].counterexample
    assert "must become FAILED" in result.findings[0].counterexample
    assert "status handler reaches" in result.findings[0].reachability
    assert "public status entry point" in result.findings[0].reachability
    assert "handler.py:42 catches" in result.findings[0].evidence
    assert "exception branch at handler.py:42" in result.findings[0].evidence
    assert "Raise from lookup" in result.findings[0].suggested_regression_scenario
    assert "Make lookup raise" in result.findings[0].suggested_regression_scenario


def test_missing_finding_identity_fails_closed_instead_of_under_compacting() -> None:
    shared = {
        "correction_identity": "lookup-failure-propagation",
        "evidence_classification": "DEMONSTRATED",
        "reachability": "The status handler reaches the exception branch",
        "evidence": "handler.py:42 catches and returns",
        "anchor_path": "handler.py",
        "suggested_regression_scenario": "Raise from lookup and assert failed status",
    }
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "findings": [
                {
                    **shared,
                    "requirement_id": "REQ-001",
                    "violated_requirement": "Failures propagate",
                    "required_behavior": "Propagate the fatal lookup failure",
                    "actual_behavior": "Returns success",
                    "counterexample": "Given a lookup error, status must propagate failure but returns success",
                },
                {
                    **shared,
                    "requirement_id": "REQ-002",
                    "violated_requirement": "Status distinguishes failure",
                    "required_behavior": "Record FAILED structured status",
                    "actual_behavior": "Leaves the previous SUCCESS status",
                    "counterexample": "Given the same lookup error, status must become FAILED but remains SUCCESS",
                },
            ],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "ERROR"
    assert result.diagnostic_category == "schema_error"
    assert result.diagnostic_reason == "DEMONSTRATED finding is missing: finding_identity"
    assert result.findings == []


def test_missing_correction_identity_fails_closed() -> None:
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "findings": [
                {
                    "finding_identity": "status-handler-lookup-failure",
                    "requirement_id": "REQ-001",
                    "violated_requirement": "Failures propagate",
                    "evidence_classification": "DEMONSTRATED",
                    "reachability": "The status handler reaches the exception branch",
                    "required_behavior": "Propagate fatal lookup failure",
                    "actual_behavior": "Returns success",
                    "evidence": "handler.py:42 catches and returns",
                    "counterexample": "Given a lookup error, status returns success",
                    "anchor_path": "handler.py",
                }
            ],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert result.result == "ERROR"
    assert result.diagnostic_category == "schema_error"
    assert result.diagnostic_reason == "DEMONSTRATED finding is missing: correction_identity"


def test_materially_different_corrections_are_not_compacted() -> None:
    common = {
        "requirement_ids": ["REQ-001"],
        "finding_identity": "test-finding",
        "correction_identity": "test-correction",
        "violated_requirement": "Failures propagate",
        "evidence_classification": "DEMONSTRATED",
        "reachability": "The handler reaches an exception branch",
        "required_behavior": "Propagate fatal failures",
        "actual_behavior": "Returns success",
        "evidence": "handler.py catches and returns",
        "counterexample": "Given an operation error, the handler returns success and tests omit the error",
        "anchor_path": "handler.py",
    }
    response = json.dumps(
        {
            "result": "NEEDS_FIX",
            "findings": [
                {
                    **common,
                    "correction_identity": "status-failure",
                    "suggested_regression_scenario": "Raise from status lookup and assert FAILED",
                },
                {
                    **common,
                    "correction_identity": "review-retry",
                    "suggested_regression_scenario": "Raise from review lookup and assert retry",
                },
            ],
        }
    )

    result = parse_adversarial_validation_response(response)

    assert len(result.findings) == 2


@pytest.mark.parametrize("contract_field", ["required_behavior", "actual_behavior", "counterexample"])
def test_different_observable_failure_contracts_are_not_compacted(contract_field: str) -> None:
    common = {
        "requirement_ids": ["REQ-001"],
        "finding_identity": "test-finding",
        "correction_identity": "test-correction",
        "violated_requirement": "Operation failures must be reported",
        "evidence_classification": "DEMONSTRATED",
        "reachability": "The same public handler reaches this return",
        "required_behavior": "Return FAILED",
        "actual_behavior": "Returns SUCCESS",
        "evidence": "handler.py:42 returns without propagating",
        "counterexample": "Given a lookup error, the handler returns SUCCESS",
        "anchor_path": "handler.py",
        # Omit suggested_regression_scenario so both receive the same default,
        # matching the dangerous case from AC-005.
    }
    different_contract = dict(common)
    different_contract["correction_identity"] = f"different-{contract_field}"
    different_contract[contract_field] = f"Materially different {contract_field}"
    response = json.dumps({"result": "NEEDS_FIX", "findings": [common, different_contract]})

    result = parse_adversarial_validation_response(response)

    assert len(result.findings) == 2


def test_requirement_coverage_stays_violated_for_grouped_current_finding() -> None:
    context = AdversarialValidationContext(issue_requirements=[IssueRequirement("REQ-001", "Propagate"), IssueRequirement("REQ-002", "Distinguish")])
    result = parse_adversarial_validation_response(
        json.dumps(
            {
                "result": "NEEDS_FIX",
                "requirement_coverage": [
                    {"requirement_id": "REQ-001", "status": "VIOLATED", "evidence": "path B"},
                    {"requirement_id": "REQ-002", "status": "VIOLATED", "evidence": "path B"},
                ],
                "findings": [
                    {
                        "requirement_ids": ["REQ-001", "REQ-002"],
                        "finding_identity": "test-finding",
                        "correction_identity": "test-correction",
                        "violated_requirement": "Failure path B violates both contracts",
                        "evidence_classification": "DEMONSTRATED",
                        "reachability": "entry -> path B",
                        "required_behavior": "report failure distinctly",
                        "actual_behavior": "reports success",
                        "evidence": "path_b.py:10",
                        "counterexample": "Given failure B, when invoked, failure is required, but success occurs, and tests omit B",
                        "anchor_path": "path_b.py",
                    }
                ],
                "thread_dispositions": [{"thread_id": "path-a", "status": "ADDRESSED", "rationale": "A is fixed", "evidence": "path_a.py now propagates"}],
            }
        )
    )

    checked = _apply_coverage_and_verdict_precedence(result, context)

    assert checked.result == "NEEDS_FIX"
    assert [entry.status for entry in checked.requirement_coverage] == ["VIOLATED", "VIOLATED"]
    assert checked.thread_dispositions[0].status == "ADDRESSED"


class TestExtractChangedTestFiles:
    """Test extraction of test files from unified git diffs."""

    def test_extract_python_test_files(self):
        diff = """diff --git a/src/auto_coder/main.py b/src/auto_coder/main.py
--- a/src/auto_coder/main.py
+++ b/src/auto_coder/main.py
@@ -1,3 +1,4 @@
+print('hello')
diff --git a/tests/test_main.py b/tests/test_main.py
new file mode 100644
--- /dev/null
+++ b/tests/test_main.py
@@ -0,0 +1,5 @@
+def test_something():
+    assert True
diff --git a/src/service_test.py b/src/service_test.py
--- a/src/service_test.py
+++ b/src/service_test.py
@@ -10,3 +10,4 @@
"""
        test_files = extract_changed_test_files(diff)
        assert "tests/test_main.py" in test_files
        assert "src/service_test.py" in test_files
        assert "src/auto_coder/main.py" not in test_files

    def test_empty_diff(self):
        assert extract_changed_test_files("") == []
        assert extract_all_changed_files("") == []

    def test_git_quoted_utf8_path_is_decoded_into_complete_manifest(self):
        quoted_diff = r'diff --git "a/src/\346\227\245\346\234\254.py" "b/src/\346\227\245\346\234\254.py"' "\n" r'--- "a/src/\346\227\245\346\234\254.py"' "\n" r'+++ "b/src/\346\227\245\346\234\254.py"' "\n" "+changed = True\n"

        assert extract_all_changed_files(quoted_diff) == ["src/日本.py"]

    def test_issue_requirements_receive_stable_machine_checkable_ids(self):
        requirements = extract_issue_requirements("Issue Description:\nR1: Persist state.\nR2: Emit an audit event.")

        assert [requirement.text for requirement in requirements] == ["R1: Persist state.", "R2: Emit an audit event."]
        assert requirements[0].requirement_id.startswith("REQ-001-")
        assert requirements[1].requirement_id.startswith("REQ-002-")

    def test_explicit_contract_uses_only_requirements_section(self):
        issue = VerifiedIssueOracle(
            number=1591,
            body="## Context\nBackground only.\n### Requirements\n- REQ-001: Preserve IDs.\nREQ-002: Preserve Origin: #12/REQ-007\n## Acceptance Scenarios\nREQ-999: Not normative.",
        )

        manifest = build_issue_requirement_manifest(IssueOracleResolution(issues=(issue,)))

        assert manifest.mode == "explicit-contract"
        assert manifest.error is None
        assert [(item.requirement_id, item.text) for item in manifest.requirements] == [
            ("REQ-001", "Preserve IDs."),
            ("REQ-002", "Preserve Origin: #12/REQ-007"),
        ]

    def test_explicit_contract_accepts_backticked_requirement_labels(self):
        issue = VerifiedIssueOracle(
            number=1588,
            body=("## Requirements\n" "- `REQ-001:` Classify only materially relevant unspecified policy choices.\n" "* `REQ-002`: Do not invent policy.\n" "1. `REQ-003:` Keep gaps separate from findings."),
        )

        manifest = build_issue_requirement_manifest(IssueOracleResolution(issues=(issue,)))

        assert manifest.error is None
        assert [(item.requirement_id, item.text) for item in manifest.requirements] == [
            ("REQ-001", "Classify only materially relevant unspecified policy choices."),
            ("REQ-002", "Do not invent policy."),
            ("REQ-003", "Keep gaps separate from findings."),
        ]

    def test_duplicate_explicit_ids_across_issues_are_issue_qualified(self):
        issues = (
            VerifiedIssueOracle(number=101, body="## Requirements\nREQ-001: First contract."),
            VerifiedIssueOracle(number=102, body="###### Requirements\n* REQ-001: Second contract."),
        )

        manifest = build_issue_requirement_manifest(IssueOracleResolution(issues=issues))

        assert [item.requirement_id for item in manifest.requirements] == ["#101/REQ-001", "#102/REQ-001"]

    @pytest.mark.parametrize(
        "body, diagnostic",
        [
            ("## Requirements\nREQ-001: One.\nREQ-001: Again.", "duplicate IDs"),
            ("## Requirements\nMust do something.", "malformed entries"),
            ("## Requirements\n\n## Context\nNothing", "no valid REQ-NNN entries"),
        ],
    )
    def test_invalid_explicit_contract_fails_closed_without_legacy_fallback(self, body, diagnostic):
        manifest = build_issue_requirement_manifest(IssueOracleResolution(issues=(VerifiedIssueOracle(number=7, body=body),)))

        assert manifest.requirements == []
        assert manifest.mode == "explicit-contract"
        assert diagnostic in (manifest.error or "")

    def test_legacy_manifest_extracts_direct_issue_body_only(self):
        issue = VerifiedIssueOracle(number=8, body="Context line.\nRequired behavior.")

        manifest = build_issue_requirement_manifest(IssueOracleResolution(issues=(issue,)))

        assert manifest.mode == "legacy-extraction"
        assert [item.text for item in manifest.requirements] == ["Context line.", "Required behavior."]


class TestParseAdversarialValidationResponse:
    """Test parsing of strong-model validation output."""

    def test_parse_json_pass(self):
        json_resp = """```json
{
  "result": "PASS",
  "summary": "All acceptance criteria verified against implementation.",
  "dynamic_check_requested": null,
  "findings": []
}
```"""
        result = parse_adversarial_validation_response(json_resp)
        assert result.is_pass
        assert not result.needs_fix
        assert result.result == "PASS"
        assert "All acceptance criteria" in result.summary
        assert len(result.findings) == 0

    def test_parse_json_needs_fix(self):
        json_resp = """```json
{
  "result": "NEEDS_FIX",
  "summary": "Found 1 subtle specification violation in edge case handling.",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "State must be persisted before event dispatch",
      "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
      "reachability": "The public save endpoint calls persist_state and then dispatch_event on this branch",
      "required_behavior": "Persist state before dispatching the event",
      "actual_behavior": "dispatch_event runs before persist_state",
      "evidence": "The supplied service patch shows the calls in the conflicting order",
      "counterexample": "Given state S, when action A occurs, then specification requires R, but implementation produces X, and tests pass because mock ignores order",
      "test_gap": "Current unit tests assert both calls happen but not the order",
      "suggested_regression_scenario": "Test state persistence timestamp is strictly before dispatch timestamp",
      "anchor_path": "src/state.py"
    }
  ]
}
```"""
        result = parse_adversarial_validation_response(json_resp)
        assert result.needs_fix
        assert not result.is_pass
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.violated_requirement == "State must be persisted before event dispatch"
        assert "Given state S" in finding.counterexample
        assert "assert both calls happen" in finding.test_gap
        assert "strictly before" in finding.suggested_regression_scenario
        assert finding.anchor_path == "src/state.py"
        assert finding.anchor_line is None
        assert finding.anchor_side == "RIGHT"

    def test_unverified_finding_cannot_be_repromoted_by_needs_fix_label(self):
        response = """{
  "result": "NEEDS_FIX",
  "summary": "Caching may expose stale state if an assumed call path exists",
  "dynamic_check_requested": "tests/test_cache.py::test_production_entry_point",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "REQ-001 requires fresh state",
      "evidence_classification": "UNVERIFIED",
      "counterexample": "A theoretical stale-cache path"
    }
  ]
}"""

        result = parse_adversarial_validation_response(response)

        assert result.result == "INCONCLUSIVE"
        assert result.is_blocked
        assert not result.needs_fix
        assert result.findings == []
        assert result.dynamic_check_requested == "tests/test_cache.py::test_production_entry_point"

    def test_unverified_finding_prevents_contradictory_pass(self):
        response = """{
  "result": "PASS",
  "summary": "The implementation appears correct, but one material path is unverified",
  "findings": [{
    "requirement_id": "REQ-001-test",
    "finding_identity": "test-finding",
    "correction_identity": "test-correction",
      "violated_requirement": "REQ-001 requires fresh state",
    "evidence_classification": "UNVERIFIED",
    "counterexample": "A suspected stale-cache path"
  }]
}"""

        result = parse_adversarial_validation_response(response)

        assert result.result == "INCONCLUSIVE"
        assert result.findings == []
        assert result.is_blocked

    @pytest.mark.parametrize(
        "missing_field",
        ["reachability", "required_behavior", "actual_behavior", "evidence"],
    )
    def test_demonstrated_finding_requires_complete_reachability_evidence(self, missing_field):
        finding = {
            "finding_identity": "test-finding",
            "correction_identity": "test-correction",
            "violated_requirement": "REQ-001 requires rejection",
            "requirement_id": "REQ-001-test",
            "evidence_classification": "DEMONSTRATED",
            "reachability": "POST /items reaches validate on input S",
            "required_behavior": "Reject S",
            "actual_behavior": "Return success for S",
            "evidence": "The supplied branch returns True",
            "counterexample": "Given S, POST /items must reject but returns success",
        }
        del finding[missing_field]

        result = parse_adversarial_validation_response(json.dumps({"result": "NEEDS_FIX", "findings": [finding]}))

        assert result.result == "ERROR"
        assert result.diagnostic_category == "schema_error"
        assert missing_field in (result.diagnostic_reason or "")

    def test_inconclusive_test_gap_is_not_normalized_to_needs_fix(self):
        response = """{
  "result": "INCONCLUSIVE",
  "summary": "One file was unavailable, but a required test category is absent",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "The Issue requires an HTTP-level regression test",
      "evidence_classification": "UNVERIFIED",
      "counterexample": "Given the complete test manifest, when coverage is inspected, then the specification requires an HTTP test, but only a service test exists, and current tests pass because they never cross the HTTP boundary",
      "test_gap": "No changed HTTP or E2E test is present",
      "suggested_regression_scenario": "Exercise diagnosis and apply through the HTTP connector"
    }
  ]
}"""

        result = parse_adversarial_validation_response(response)

        assert result.result == "INCONCLUSIVE"
        assert not result.needs_fix
        assert result.findings == []

    def test_parse_bare_json(self):
        json_resp = '{"result": "PASS", "summary": "Looks good", "findings": []}'
        result = parse_adversarial_validation_response(json_resp)
        assert result.is_pass
        assert result.summary == "Looks good"

    def test_parse_concrete_findings_override_contradictory_pass_label(self):
        """A valid counterexample must survive a contradictory top-level PASS."""
        json_resp = """{
  "result": "PASS",
  "summary": "Says pass but listed a bug",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "Spec requirement R",
          "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
          "reachability": "The public handler reaches branch B for state S",
          "required_behavior": "Produce R",
          "actual_behavior": "Produce X",
          "evidence": "The supplied handler patch returns X from branch B",
      "counterexample": "Given state S, produces X",
      "test_gap": "Gap G",
      "suggested_regression_scenario": "Scenario T",
      "anchor_path": "src/feature.py"
    }
  ]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1
        assert result.raw_response == json_resp

    def test_parse_malformed_findings_with_empty_dict_fails_closed_to_error(self):
        """Malformed findings containing empty dict must fail closed to ERROR."""
        json_resp = """{
  "result": "PASS",
  "findings": [{}]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_malformed_findings_with_numbers_fails_closed_to_error(self):
        """Malformed findings containing non-dict items must fail closed to ERROR."""
        json_resp = """{
  "result": "PASS",
  "findings": [123]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_needs_fix_missing_counterexample_fails_closed_to_error(self):
        """NEEDS_FIX without required concrete counterexample must NOT synthesize fake counterexample, fails closed to ERROR."""
        json_resp = """{
  "result": "NEEDS_FIX",
  "findings": [
    {"finding_identity": "test-finding",
    "correction_identity": "test-correction",
      "violated_requirement": "Maybe caching is wrong"}
  ]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_text_fallback_needs_fix_fails_closed_without_evidence_schema(self):
        text_resp = """RESULT: NEEDS_FIX
VIOLATED_REQUIREMENT: User session must expire after 2 hours
COUNTEREXAMPLE: Given state S, when token is 3 hours old, then specification requires logout, but current implementation accepts it because timestamp is checked against local time instead of UTC
TEST_GAP: Tests mock datetime.now() with UTC timezone directly
SUGGESTED_REGRESSION_SCENARIO: Assert token expiration with timezone offset differences
"""
        result = parse_adversarial_validation_response(text_resp)
        assert result.result == "ERROR"
        assert not result.needs_fix
        assert result.findings == []

    def test_parse_text_concrete_finding_overrides_pass(self):
        """Legacy text cannot assert the demonstrated fields required to block."""
        text_resp = """RESULT: PASS
VIOLATED_REQUIREMENT: User session must expire after 2 hours
COUNTEREXAMPLE: Given state S, produces invalid token
"""
        result = parse_adversarial_validation_response(text_resp)
        assert not result.is_pass
        assert not result.needs_fix
        assert result.result == "ERROR"

    def test_parse_text_concrete_finding_overrides_blocked(self):
        text_resp = """RESULT: BLOCKED
VIOLATED_REQUIREMENT: Audit events must be persisted
COUNTEREXAMPLE: Given state S, when save succeeds but audit delivery is unavailable, then the specification requires a durable event, but the implementation drops it, and tests pass because delivery is mocked
TEST_GAP: No test covers unavailable audit delivery
SUGGESTED_REGRESSION_SCENARIO: Persist an event while delivery is offline
"""

        result = parse_adversarial_validation_response(text_resp)

        assert result.result == "BLOCKED"
        assert not result.needs_fix
        assert result.findings == []

    def test_parse_empty_response_fails_closed_to_error(self):
        """Empty response must fail closed to ERROR and block merge."""
        result = parse_adversarial_validation_response("")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"
        assert result.diagnostic_category == "empty_response"
        assert "no non-whitespace content" in (result.diagnostic_reason or "")
        assert result.raw_response == ""

    def test_parse_malformed_response_fails_closed_to_error(self):
        """Unparseable/corrupted response must fail closed to ERROR."""
        result = parse_adversarial_validation_response("Just random chatter with no valid JSON or RESULT header.")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"
        assert result.diagnostic_category == "unrecognized_format"
        assert result.raw_response == "Just random chatter with no valid JSON or RESULT header."

    def test_parse_invalid_json_reports_syntax_location_and_fails_closed(self):
        response = '{"result": "PASS", "findings": [}'
        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "json_parse_error"
        assert "line 1" in (result.diagnostic_reason or "")
        assert result.raw_response == response

    def test_parse_valid_json_with_invalid_schema_reports_schema_reason(self):
        response = '{"result": "PASS", "findings": "none"}'
        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "schema_error"
        assert result.diagnostic_reason == "findings must be a list, got str"
        assert result.raw_response == response

    def test_parse_codex_jsonl_uses_final_agent_message_and_preserves_event_stream(self):
        final_message = '{"result":"PASS","summary":"All requirements verified","findings":[]}'
        response = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Reviewing the change."}}',
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_1", "type": "agent_message", "text": final_message},
                    }
                ),
                '{"type":"turn.completed"}',
            ]
        )

        result = parse_adversarial_validation_response(response)

        assert result.is_pass
        assert result.summary == "All requirements verified"
        assert result.raw_response == response

    def test_parse_codex_jsonl_with_non_json_contamination_fails_closed(self):
        response = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "agent_message",
                            "text": '{"result":"PASS","findings":[]}',
                        },
                    }
                ),
                "unexpected stderr warning",
            ]
        )

        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "cli_event_stream_error"
        assert "non-JSON content" in (result.diagnostic_reason or "")
        assert result.raw_response == response

    @pytest.mark.parametrize(
        ("event_line", "expected_reason"),
        [
            ('{"type":"turn.completed"}', "no completed agent message"),
            ('{"type":"turn.failed","error":{"message":"sandbox unavailable"}}', "turn.failed"),
        ],
    )
    def test_parse_incomplete_or_failed_codex_jsonl_fails_closed(self, event_line, expected_reason):
        response = "\n".join(['{"type":"thread.started","thread_id":"thread-1"}', event_line])

        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "cli_event_stream_error"
        assert expected_reason in (result.diagnostic_reason or "")
        assert result.raw_response == response


class TestBuildAdversarialValidationContext:
    """Test gathering of context for adversarial validation."""

    def test_build_context(self):
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/tests/test_x.py b/tests/test_x.py\n+++ b/tests/test_x.py"
        mock_client.get_pr_changed_file_count.return_value = 1
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Add rate limiting"
        mock_issue.body = "Specification: Limit to 100 req/min. Acceptance Criteria: Return 429 when exceeded."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        pr_data = {
            "number": 42,
            "title": "Implement rate limiting",
            "body": "Fixes #10",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert context.pr_number == 42
        assert context.pr_title == "Implement rate limiting"
        assert "tests/test_x.py" in context.changed_tests
        assert "Linked Issue #10" in context.issue_context
        assert not context.is_diff_truncated

    def test_build_context_uses_file_aware_evidence_for_large_diff(self):
        mock_client = MagicMock()
        huge_diff = "diff --git a/file1.py b/file1.py\n+++ b/file1.py\n" + ("+" + "a" * 100 + "\n") * 500 + "diff --git a/file_late.py b/file_late.py\n+++ b/file_late.py\n"
        mock_client.get_pr_diff.return_value = huge_diff
        mock_client.get_pr_changed_file_count.return_value = 2
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Big change"
        mock_issue.body = "Spec details"
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        config.MAX_PR_DIFF_SIZE = 100  # small threshold to trigger truncation
        pr_data = {"number": 99, "title": "Big PR", "body": "Fixes #10"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert context.is_diff_truncated
        assert "COVERAGE INCOMPLETE" in context.pr_diff
        assert "### Changed file: file_late.py" in context.pr_diff
        assert "file_late.py" in context.all_changed_files
        assert "file1.py" in context.unverified_files

    def test_rendered_prompt_preserves_late_file_patch_and_complete_manifests(self):
        """A large early patch must not hide a later material file from the prompt."""
        mock_client = MagicMock()
        diff_prefix = "diff --git a/src/early.py b/src/early.py\n+++ b/src/early.py\n" + ("+early_line\n" * 200)
        diff_suffix = "diff --git a/src/late_secret_feature.py b/src/late_secret_feature.py\n+++ b/src/late_secret_feature.py\n+late_line\n"
        huge_diff = diff_prefix + diff_suffix

        mock_client.get_pr_diff.return_value = huge_diff
        mock_client.get_pr_changed_file_count.return_value = 2
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Complex feature"
        mock_issue.body = "Spec: Must handle late secret feature."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        config.MAX_PR_DIFF_SIZE = 200
        pr_data = {"number": 100, "title": "Complex PR", "body": "Fixes #10"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)

        changed_tests_str = "\n".join(f"- {t}" for t in context.changed_tests) if context.changed_tests else "(No test files detected in diff)"
        rendered_prompt = render_prompt(
            "pr.adversarial_validation",
            review_policy=render_prompt("pr.adversarial_validation_initial_review"),
            repo_name="owner/repo",
            pr_number=100,
            pr_title=context.pr_title,
            pr_body=context.pr_body,
            pr_diff=context.pr_diff,
            linked_issues_context=context.issue_context,
            changed_tests=changed_tests_str,
            changed_files="\n".join(f"- {path}" for path in context.all_changed_files),
            coverage_status="INCOMPLETE",
            requirement_manifest="\n".join(f"- {item.requirement_id}: {item.text}" for item in context.issue_requirements),
        )

        assert "Complete Changed-File Manifest" in rendered_prompt
        assert "src/late_secret_feature.py" in rendered_prompt
        assert "+late_line" in rendered_prompt
        assert "Coverage: COMPLETE" in rendered_prompt

    def test_oversized_first_file_does_not_starve_later_security_and_test_files(self):
        huge_patch = "diff --git a/generated.txt b/generated.txt\n+++ b/generated.txt\n" + "+generated\n" * 1000
        security_patch = "diff --git a/src/security.py b/src/security.py\n+++ b/src/security.py\n+reject_unsafe_input()\n"
        test_patch = "diff --git a/tests/test_security.py b/tests/test_security.py\n+++ b/tests/test_security.py\n+assert_rejected()\n"

        evidence, unverified = build_file_aware_diff(huge_patch + security_patch + test_patch, 700)

        assert "generated.txt" in unverified
        assert "### Changed file: src/security.py" in evidence
        assert "+reject_unsafe_input()" in evidence
        assert "### Changed file: tests/test_security.py" in evidence
        assert "+assert_rejected()" in evidence

    def test_quoted_path_before_normal_file_is_never_omitted_or_reported_complete(self):
        quoted_patch = r'diff --git "a/src/\346\227\245\346\234\254.py" "b/src/\346\227\245\346\234\254.py"' "\n" r'--- "a/src/\346\227\245\346\234\254.py"' "\n" r'+++ "b/src/\346\227\245\346\234\254.py"' "\n" + "+quoted_change\n" * 100
        normal_patch = "diff --git a/src/normal.py b/src/normal.py\n+++ b/src/normal.py\n+normal_change\n"

        evidence, unverified = build_file_aware_diff(quoted_patch + normal_patch, 300)

        assert "### Changed file: src/normal.py" in evidence
        assert "src/日本.py" in unverified
        assert "COVERAGE INCOMPLETE" in evidence

    def test_many_file_evidence_obeys_hard_size_bound(self):
        patches = []
        for index in range(100):
            path = f"src/generated/component_{index:03d}_with_a_descriptive_name.py"
            patches.append(f"diff --git a/{path} b/{path}\n+++ b/{path}\n+value_{index} = True\n")

        evidence, unverified = build_file_aware_diff("".join(patches), 6000)

        assert len(evidence) <= 6000
        assert unverified
        assert "COVERAGE INCOMPLETE" in evidence
        assert "Unverified manifest SHA-256:" in evidence

    def test_authoritative_301_file_count_requires_human_review(self):
        mock_client = MagicMock()
        visible_files = [f"src/file_{index:03d}.py" for index in range(300)]
        raw_diff = "".join(f"diff --git a/{path} b/{path}\n+++ b/{path}\n+changed = True\n" for path in visible_files)
        mock_client.get_pr_diff.return_value = raw_diff
        mock_client.get_pr_changed_file_count.return_value = 301
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Large PR requirement"
        mock_issue.body = "The hidden test file is material."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        context = build_adversarial_validation_context(
            "owner/repo",
            {"number": 301, "title": "Large PR", "body": "Fixes #10"},
            AutomationConfig(),
            github_client=mock_client,
        )

        assert len(context.all_changed_files) == 300
        assert context.requires_human_review

    def test_authoritative_changed_file_count_failure_is_recorded(self):
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py\n+changed = True\n"
        mock_client.get_pr_changed_file_count.side_effect = RuntimeError("count unavailable")
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Count requirement"
        mock_issue.body = "The changed-file count must be authoritative."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        context = build_adversarial_validation_context(
            "owner/repo",
            {"number": 302, "title": "Count failure", "body": "Fixes #10"},
            AutomationConfig(),
            github_client=mock_client,
        )

        assert context.evidence_retrieval_error == "Authoritative changed-file count retrieval failed: count unavailable"
        assert not context.requires_human_review

    @pytest.mark.parametrize("late_file_first", [False, True])
    def test_violating_file_evidence_is_order_independent(self, late_file_first):
        huge_patch = "diff --git a/generated.txt b/generated.txt\n+++ b/generated.txt\n" + "+generated\n" * 1000
        violation_patch = "diff --git a/src/violation.py b/src/violation.py\n+++ b/src/violation.py\n+allow_forbidden_state()\n"
        raw_diff = violation_patch + huge_patch if late_file_first else huge_patch + violation_patch

        evidence, _ = build_file_aware_diff(raw_diff, 500)

        assert "+allow_forbidden_state()" in evidence

    def test_hierarchical_oracle_selection_prefers_explicit_linking_keyword(self):
        """When explicit linking keywords exist in body, other reference issues are NOT included."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"
        mock_client.get_pr_changed_file_count.return_value = 1

        def get_issue_side_effect(repo, issue_num):
            m = MagicMock(spec=["title", "body"])
            if issue_num == 100:
                m.title = "Core Feature Specification"
                m.body = "Specification: Requirement A and B."
                return m
            elif issue_num == 200:
                m.title = "Background Discussion Only"
                m.body = "Discussion: Not the PR requirement."
                return m
            return None

        mock_client.get_issue.side_effect = get_issue_side_effect
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        # PR body explicitly links #100, while merely referencing #200
        pr_data = {
            "number": 50,
            "title": "feat: implementation",
            "body": "Fixes #100\n\nThis implementation follows the approach discussed in issue #200.",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #100" in context.issue_context
        assert "Linked Issue #200" not in context.issue_context

    def test_unaggregated_inconclusive_result_remains_blocked_until_precedence_is_applied(self):
        """Direct result construction remains fail-closed; parsed results apply precedence."""
        finding = AdversarialValidationFinding(
            violated_requirement="Spec invariant",
            counterexample="Given state S, action A produces X",
        )
        res = AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Need dynamic verification",
            findings=[finding],
        )
        assert not res.needs_fix
        assert not res.is_pass
        assert res.is_blocked

    def test_parent_issue_scope_boundary_notice(self):
        """Parent issue context includes explicit SCOPE BOUNDARY NOTICE ensuring sub-issue PR scope is preserved."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"
        mock_client.get_pr_changed_file_count.return_value = 1
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Sub-issue A"
        mock_issue.body = "Specification: Implement feature A only."
        mock_client.get_issue.return_value = mock_issue

        mock_client.get_parent_issue_details.return_value = {"number": 100, "title": "Epic Feature A, B and C"}
        mock_client.get_parent_issue_body.return_value = "Parent Specification: Must implement A, B, and C."

        config = AutomationConfig()
        pr_data = {"number": 10, "title": "feat: feature A", "body": "Fixes #101"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #101: Sub-issue A" in context.issue_context
        assert "SCOPE BOUNDARY NOTICE" in context.issue_context
        assert "Do NOT require parent requirements outside the child issue scope" in context.issue_context
        assert "Parent Issue #100 (CONTEXT ONLY" in context.issue_context

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_enforces_permission_mode_plan_on_noedit(self, mock_sub_run, mock_get_config):
        """ClaudeClient must inject --permission-mode plan when is_noedit=True even if no options_for_noedit configured."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--max-thinking-tokens", "1000"]
        mock_backend.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--permission-mode" in called_cmd
            assert "plan" in called_cmd

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_overrides_configured_and_extra_args_write_modes_in_noedit(self, mock_sub_run, mock_get_config):
        """ClaudeClient must override configured bypassPermissions and strip extra args --dangerously-skip-permissions."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--permission-mode", "bypassPermissions"]
        mock_backend.options_for_noedit = ["--permission-mode", "bypassPermissions"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--permission-mode", "bypassPermissions"],
            "options_for_noedit": ["--permission-mode", "bypassPermissions"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._extra_args = ["--dangerously-skip-permissions", "--permission-mode", "default"]
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in called_cmd
            assert "bypassPermissions" not in called_cmd
            assert "default" not in called_cmd
            assert "--permission-mode" in called_cmd
            assert "plan" in called_cmd

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_preserves_writable_mode_when_not_noedit(self, mock_sub_run, mock_get_config):
        """Normal implementation runs (is_noedit=False) retain writable configurations."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--dangerously-skip-permissions"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["--dangerously-skip-permissions"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._run_llm_cli("prompt", is_noedit=False)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" in called_cmd
            assert "--permission-mode" not in called_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_enforces_sandbox_readonly_on_noedit(self, mock_sub_run, mock_get_config):
        """CodexClient must inject --sandbox read-only when is_noedit=True even if no options_for_noedit configured."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["exec", "--json"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["exec", "--json"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "--json",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_overrides_configured_and_extra_args_write_modes_in_noedit(self, mock_sub_run, mock_get_config):
        """CodexClient must override configured workspace-write and strip extra args danger-full-access/full-auto."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--sandbox", "workspace-write", "exec", "--full-auto"]
        mock_backend.options_for_noedit = ["--sandbox", "workspace-write", "exec"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--sandbox", "workspace-write", "exec", "--full-auto"],
            "options_for_noedit": ["--sandbox", "workspace-write", "exec"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._extra_args = ["--sandbox", "danger-full-access", "-y", "--ask-for-approval", "always"]
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "workspace-write" not in called_cmd
            assert "danger-full-access" not in called_cmd
            assert "--full-auto" not in called_cmd
            assert "-y" not in called_cmd
            assert "always" not in called_cmd
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_strips_yolo_and_dangerously_bypass_approvals_and_sandbox(self, mock_sub_run, mock_get_config):
        """CodexClient must strip real YOLO/bypass flags and -s alias and enforce -c approvals_reviewer="user"."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--dangerously-bypass-approvals-and-sandbox", "exec", "-s", "workspace-write"]
        mock_backend.options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox", "exec"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--dangerously-bypass-approvals-and-sandbox", "exec", "-s", "workspace-write"],
            "options_for_noedit": ["--dangerously-bypass-approvals-and-sandbox", "exec"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._extra_args = ["--yolo", "--approve-for-me", "--not-so-yolo", "-c", 'approvals_reviewer="auto_review"']
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-bypass-approvals-and-sandbox" not in called_cmd
            assert "--yolo" not in called_cmd
            assert "--approve-for-me" not in called_cmd
            assert "--not-so-yolo" not in called_cmd
            assert "workspace-write" not in called_cmd
            assert 'approvals_reviewer="auto_review"' not in called_cmd
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_preserves_writable_mode_when_not_noedit(self, mock_sub_run, mock_get_config):
        """Normal implementation runs (is_noedit=False) retain writable configurations."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--sandbox", "workspace-write", "--full-auto"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["--sandbox", "workspace-write", "--full-auto"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._run_llm_cli("prompt", is_noedit=False)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--sandbox" in called_cmd
            assert "workspace-write" in called_cmd
            assert "--full-auto" in called_cmd

    def test_oracle_recovery_falls_back_to_title_when_no_body_link(self):
        """Issue specification is recovered from PR title when body omits linking phrase."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"
        mock_client.get_pr_changed_file_count.return_value = 1
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Implement rate limiting"
        mock_issue.body = "Spec: Limit to 100 req/min"
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        # PR body has no linking keyword
        pr_data = {
            "number": 200,
            "title": "feat: rate limiting implementation for issue #1567",
            "body": "This PR updates the rate limiting algorithm.",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #1567" in context.issue_context
        assert "Limit to 100 req/min" in context.issue_context


class TestRunAdversarialValidation:
    """Test executing adversarial validation."""

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    def test_rereview_prompt_composition_excludes_initial_broad_policy(self, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=1594,
            pr_title="Convergent rereview",
            pr_body="Closes #1594",
            pr_diff="diff --git a/fix.py b/fix.py\n+corrective_change = True",
            all_changed_files=["fix.py"],
            changed_tests=["tests/test_fix.py"],
            issue_context="Issue requires convergence.",
            issue_requirements=[IssueRequirement(requirement_id="REQ-003", text="Rereview must converge.")],
        )
        registry = MagicMock()
        registry.get.return_value = ReviewerSession(
            repository="owner/repo",
            pr_number=1594,
            backend_name="reviewer",
            backend_type="codex",
            model_name="strong",
            session_id="persisted-session",
            last_head_sha="previous-head",
        )
        manager = MagicMock()
        manager.get_current_backend_identity.return_value = ("reviewer", "codex", "strong")
        manager.continue_session.return_value = """{
          "result": "PASS",
          "summary": "Previous blockers are fixed",
          "requirement_coverage": [{"requirement_id": "REQ-003", "status": "VERIFIED", "evidence": "Corrective change and regression test"}],
          "findings": []
        }"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1594, "title": "Convergent rereview", "body": "Closes #1594", "head": {"sha": "current-head"}},
            AutomationConfig(),
            backend_manager=manager,
            session_registry=registry,
        )

        assert result.is_pass
        prompt = manager.continue_session.call_args.args[1]
        normalized_prompt = " ".join(prompt.split())
        assert "Do NOT restart unrestricted broad adversarial exploration" in prompt
        assert "FIXED, STILL_VIOLATED, or REGRESSED" in prompt
        assert "incremental requirement-coverage validation" in normalized_prompt
        assert "carry that status forward only after checking" in normalized_prompt
        assert "Never carry forward a status merely because the previous reviewer reported it" in normalized_prompt
        assert "exactly one coverage entry for every stable requirement ID" in normalized_prompt
        assert "initial validation remains a full-coverage, fail-closed review" in normalized_prompt
        assert "Stable Issue Requirement Manifest:\nManifest mode: legacy-extraction\n- REQ-003: Rereview must converge." in prompt
        assert "Changed/Added Tests:\n- tests/test_fix.py" in prompt
        assert "corrective_change = True" in prompt
        assert "Your mission: Falsify the implementation" not in prompt
        assert "Assume the implementation may contain subtle requirement misunderstandings, unhandled edge cases" not in prompt

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_pass(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
            issue_requirements=[IssueRequirement(requirement_id="REQ-001-x", text="Must do X")],
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "Valid implementation",
  "requirement_coverage": [{"requirement_id": "REQ-001-x", "status": "VERIFIED", "evidence": "Patch implements X and its test asserts X"}],
  "findings": []
}"""

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.is_pass
        assert result.result == "PASS"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_needs_fix(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
            issue_requirements=[IssueRequirement(requirement_id="REQ-001-test", text="Must do X")],
        )
        mock_run_prompt.return_value = """{
  "result": "NEEDS_FIX",
  "summary": "Found violation",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "Spec X",
          "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
          "reachability": "The public entry point reaches the shown branch for S",
          "required_behavior": "Return R",
          "actual_behavior": "Return X",
          "evidence": "The supplied diff returns X on that branch",
      "counterexample": "Given state S, when A occurs, then R, but produces X, tests pass because Y",
      "test_gap": "Gap",
      "suggested_regression_scenario": "Scenario",
      "anchor_path": "src/feature.py"
    }
  ]
}"""

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert len(result.findings) == 1

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_parse_failure_trace_is_correlated_bounded_and_references_full_log(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=1582,
            pr_title="Diagnose malformed response",
            pr_body="Fixes #1582",
            pr_diff="diff content",
            issue_context="Malformed validation responses must remain blocked.",
        )
        response = "unexpected validator prose " + ("x" * (ADVERSARIAL_RESPONSE_PREVIEW_LIMIT + 500))
        mock_run_prompt.return_value = response
        backend_manager = MagicMock()
        backend_manager.get_last_backend_and_model.return_value = ("codex", "gpt-5.6")
        backend_manager.get_last_interaction_log_path.return_value = "/tmp/llm_output.jsonl"
        trace_logger = get_trace_logger()
        trace_logger.clear()

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1582, "title": "Diagnose malformed response", "body": "Fixes #1582"},
            AutomationConfig(),
            backend_manager=backend_manager,
        )

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.raw_response == response
        parse_entries = [entry for entry in trace_logger.get_logs(item_type="pr", item_number=1582) if entry["category"] == "Adversarial Validation Parse Failure"]
        assert len(parse_entries) == 1
        details = parse_entries[0]["details"]
        assert details["attempt"] == "initial"
        assert details["backend"] == "codex"
        assert details["model"] == "gpt-5.6"
        assert details["response_state"] == "non-empty"
        assert details["response_length"] == len(response)
        assert details["diagnostic_category"] == "unrecognized_format"
        assert details["interaction_log"] == "/tmp/llm_output.jsonl"
        assert len(details["response_preview"]) <= ADVERSARIAL_RESPONSE_PREVIEW_LIMIT + 50
        assert "characters omitted" in details["response_preview"]

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    def test_run_adversarial_validation_no_issue_context_fails_closed_to_blocked(self, mock_build_ctx):
        """Oracle acquisition failure must block merge (BLOCKED), not pass."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="No linked issue",
            pr_diff="diff content",
            changed_tests=[],
            issue_context="",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": ""}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "Oracle acquisition failed" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    def test_run_adversarial_validation_missing_diff_fails_closed_to_blocked(self, mock_build_ctx):
        """Diff retrieval failure must block merge (BLOCKED) rather than validating against empty diff."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="",  # Diff unavailable
            changed_tests=[],
            issue_context="Issue specification: Must do X.",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "Diff retrieval failed" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_301_files_requires_human_review(self, mock_run_prompt, mock_build_ctx):
        """A model cannot authorize merge from a raw diff capped at 300 files."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=301,
            pr_title="Large PR",
            pr_body="Fixes #1",
            pr_diff="diff content for the first 300 files",
            issue_context="Issue specification: Must do X.",
            requires_human_review=True,
        )

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 301, "title": "Large PR", "body": "Fixes #1"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "BLOCKED"
        assert result.is_blocked
        assert result.diagnostic_category == "human_review_required"
        assert "human review is required" in result.summary
        mock_run_prompt.assert_not_called()

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_missing_authoritative_count_blocks(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=302,
            pr_title="Count unavailable",
            pr_body="Fixes #1",
            pr_diff="diff content",
            issue_context="Issue specification: Must do X.",
            evidence_retrieval_error="Authoritative changed-file count retrieval failed: unavailable",
        )

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 302, "title": "Count unavailable", "body": "Fixes #1"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "BLOCKED"
        assert result.diagnostic_category == "evidence_retrieval_failure"
        mock_run_prompt.assert_not_called()

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.cli_helpers.create_adversarial_validation_backend_manager", return_value=None)
    def test_run_adversarial_validation_no_backend_available_fails_closed(self, mock_mgr, mock_build_ctx):
        """No strong backend configured or available must fail closed to BLOCKED."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=[],
            issue_context="Spec content",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=None)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "No strong adversarial validation backend configured" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_concrete_finding_prevents_later_dynamic_uncertainty_from_erasing_it(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """A concrete finding wins immediately and is not downgraded by a later check."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
            issue_requirements=[IssueRequirement(requirement_id="REQ-001-test", text="Must do X")],
        )
        mock_run_prompt.side_effect = [
            """{
  "result": "INCONCLUSIVE",
  "summary": "Suspected defect in state reload",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "State reload invariant",
          "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
          "reachability": "The focused reload test executes the production reload path",
          "required_behavior": "Preserve reloaded state",
          "actual_behavior": "Reload produces X",
          "evidence": "The supplied path or focused dynamic check demonstrates X",
      "counterexample": "Given state S, when reload occurs, then persisted timestamp is lost",
      "test_gap": "Test does not check reload",
      "suggested_regression_scenario": "Test state reload",
      "anchor_path": "src/feature.py"
    }
  ]
}""",
            '{"result": "PASS", "summary": "Reviewer confirmed reload output satisfies spec", "requirement_coverage_complete": true, "unverified_requirements": [], "findings": []}',
        ]
        # run_local_tests returns dict with output and errors
        mock_run_tests.return_value = {
            "success": True,
            "output": "PASSED tests/test_feature.py::test_reload_scenario",
            "errors": "DeprecationWarning: something deprecated",
        }

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert mock_run_prompt.call_count == 1
        mock_run_tests.assert_not_called()

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_is_rejected_when_material_file_evidence_is_incomplete(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Large change",
            pr_diff="bounded evidence",
            all_changed_files=["src/huge.py", "tests/test_feature.py"],
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification",
            is_diff_truncated=True,
            unverified_files=["src/huge.py"],
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Looks correct", "findings": []}'

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Large change"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert result.is_blocked
        assert result.diagnostic_category == "incomplete_evidence_coverage"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_without_structured_requirement_coverage_is_rejected(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
            issue_requirements=[
                IssueRequirement(requirement_id="REQ-001-r1", text="R1: persist state"),
                IssueRequirement(requirement_id="REQ-002-r2", text="R2: emit an audit event"),
            ],
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Reviewed R1", "findings": []}'

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert result.diagnostic_category == "incomplete_requirement_coverage"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_false_complete_boolean_cannot_hide_omitted_requirement_id(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
            issue_requirements=[
                IssueRequirement(requirement_id="REQ-001-r1", text="R1: persist state"),
                IssueRequirement(requirement_id="REQ-002-r2", text="R2: emit an audit event"),
            ],
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "R1 is correct",
  "requirement_coverage_complete": true,
  "requirement_coverage": [
    {"requirement_id": "REQ-001-r1", "status": "VERIFIED", "evidence": "R1 patch inspected"}
  ],
  "findings": []
}"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert "REQ-002-r2" in (result.diagnostic_reason or "")

    @pytest.mark.parametrize("top_level_result", ["PASS", "INCONCLUSIVE"])
    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_violated_coverage_without_finding_fails_closed_to_error(self, mock_run_prompt, mock_build_ctx, top_level_result):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
            issue_requirements=[
                IssueRequirement(requirement_id="REQ-001-r1", text="R1: persist state"),
                IssueRequirement(requirement_id="REQ-002-r2", text="R2: emit an audit event"),
            ],
        )
        mock_run_prompt.return_value = json.dumps(
            {
                "result": top_level_result,
                "summary": "R2 is violated but no finding was emitted",
                "requirement_coverage": [
                    {"requirement_id": "REQ-001-r1", "status": "VERIFIED", "evidence": "R1 patch inspected"},
                    {"requirement_id": "REQ-002-r2", "status": "VIOLATED", "evidence": "Audit event is dropped"},
                ],
                "findings": [],
            }
        )

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "ERROR"
        assert result.diagnostic_category == "violated_requirement_without_finding"
        assert "REQ-002-r2" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_unknown_violated_requirement_id_cannot_disappear_into_pass(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
            issue_requirements=[
                IssueRequirement(requirement_id="REQ-001-r1", text="R1: persist state"),
                IssueRequirement(requirement_id="REQ-002-r2", text="R2: emit an audit event"),
            ],
        )
        mock_run_prompt.return_value = json.dumps(
            {
                "result": "PASS",
                "summary": "Expected IDs are covered but a typo ID reports a violation",
                "requirement_coverage": [
                    {"requirement_id": "REQ-001-r1", "status": "VERIFIED", "evidence": "R1 inspected"},
                    {"requirement_id": "REQ-002-r2", "status": "VERIFIED", "evidence": "R2 inspected"},
                    {"requirement_id": "REQ-002-typo", "status": "VIOLATED", "evidence": "Audit event is dropped"},
                ],
                "findings": [],
            }
        )

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "ERROR"
        assert result.diagnostic_category == "unknown_requirement_coverage_id"
        assert "REQ-002-typo" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_demonstrated_finding_for_unknown_requirement_cannot_block_as_needs_fix(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="One requirement",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="REQ-001: Persist state",
            issue_requirements=[IssueRequirement(requirement_id="REQ-001-r1", text="Persist state")],
        )
        mock_run_prompt.return_value = json.dumps(
            {
                "result": "NEEDS_FIX",
                "summary": "An invented hardening invariant is violated",
                "requirement_coverage": [{"requirement_id": "REQ-001-r1", "status": "VERIFIED", "evidence": "Persistence is correct"}],
                "findings": [
                    {
                        "requirement_id": "REQ-999-invented",
                        "finding_identity": "test-finding",
                        "correction_identity": "test-correction",
                        "violated_requirement": "All caches should have extra defensive guards",
                        "evidence_classification": "DEMONSTRATED",
                        "reachability": "The cache entry point accepts an empty key",
                        "required_behavior": "Reject empty keys",
                        "actual_behavior": "Accept an empty key",
                        "evidence": "The implementation has no empty-key guard",
                        "counterexample": "Given an empty key, the cache accepts it",
                        "anchor_path": "src/feature.py",
                    }
                ],
            }
        )

        result = run_adversarial_validation("owner/repo", {"number": 100, "title": "One requirement"}, AutomationConfig(), backend_manager=MagicMock())

        assert result.result == "ERROR"
        assert not result.needs_fix
        assert result.findings == []
        assert result.diagnostic_category == "unknown_finding_requirement_id"
        assert "REQ-999-invented" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_with_explicit_unverified_requirement_is_rejected(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
            issue_requirements=[
                IssueRequirement(requirement_id="REQ-001-r1", text="R1: persist state"),
                IssueRequirement(requirement_id="REQ-002-r2", text="R2: emit an audit event"),
            ],
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "R1 was verified but R2 was not inspected",
  "requirement_coverage": [
    {"requirement_id": "REQ-001-r1", "status": "VERIFIED", "evidence": "Persistence patch inspected"},
    {"requirement_id": "REQ-002-r2", "status": "UNVERIFIED", "evidence": "Audit behavior was not inspected"}
  ],
  "findings": []
}"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert "REQ-002-r2" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_finding_wins_while_incomplete_coverage_remains_diagnostic(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Large change",
            pr_diff="bounded evidence",
            all_changed_files=["src/huge.py", "tests/service_test.py"],
            changed_tests=["tests/service_test.py"],
            issue_context="Issue requires service and HTTP tests",
            unverified_files=["src/huge.py"],
        )
        mock_run_prompt.return_value = """{
  "result": "INCONCLUSIVE",
  "summary": "Implementation file is partial, but required HTTP coverage is absent",
  "findings": [{
    "finding_identity": "test-finding",
    "correction_identity": "test-correction",
      "violated_requirement": "HTTP-level tests are required",
    "evidence_classification": "UNVERIFIED",
    "counterexample": "Given the complete test manifest, when required categories are compared, then an HTTP test is required, but only a service test exists, and CI passes because no HTTP test runs",
    "test_gap": "The changed-test manifest has no HTTP test",
    "suggested_regression_scenario": "Add an HTTP connector regression test"
  }]
}"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Large change"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert not result.needs_fix
        assert result.findings == []

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_dynamic_followup_overturns_initial_addressed_disposition(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """The dynamic check disproves an initial ADDRESSED claim; the
        follow-up's fresh STILL_VALID must win, never the stale initial one."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "Need dynamic verification of claimed thread",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "Looks fixed", "evidence": "Code inspection"}
  ]
}"""
        mock_run_tests.return_value = {"success": False, "output": "FAILED tests/test_feature.py::test_reload", "errors": ""}

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        manager = MagicMock()
        manager._last_session_id = "review-session"
        manager.continue_session.return_value = """{
  "result": "PASS",
  "summary": "Dynamic check disproves the claimed fix",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "STILL_VALID", "rationale": "The dynamic test reproduces the original defect", "evidence": "FAILED tests/test_feature.py::test_reload"}
  ]
}"""

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=manager)

        assert len(result.thread_dispositions) == 1
        assert result.thread_dispositions[0].status == "STILL_VALID"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_dynamic_followup_confirms_initial_inconclusive_as_addressed(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """The dynamic check proves an initially INCONCLUSIVE thread is fixed."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "Need dynamic verification of claimed thread",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "INCONCLUSIVE", "rationale": "Cannot tell from the diff alone", "evidence": "No dynamic evidence yet"}
  ]
}"""
        mock_run_tests.return_value = {"success": True, "output": "PASSED tests/test_feature.py::test_reload", "errors": ""}

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        manager = MagicMock()
        manager._last_session_id = "review-session"
        manager.continue_session.return_value = """{
  "result": "PASS",
  "summary": "Dynamic check proves the fix",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "The dynamic test now exercises and passes the original failing path", "evidence": "PASSED tests/test_feature.py::test_reload"}
  ]
}"""

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=manager)

        assert len(result.thread_dispositions) == 1
        assert result.thread_dispositions[0].status == "ADDRESSED"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_dynamic_followup_omitting_dispositions_does_not_resurrect_stale_ones(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """If the follow-up response omits thread_dispositions entirely, the
        claimed thread must fail closed to unresolved rather than silently
        keep the initial (now unverified) disposition."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "Need dynamic verification of claimed thread",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "Looks fixed", "evidence": "Code inspection"}
  ]
}"""
        mock_run_tests.return_value = {"success": True, "output": "PASSED", "errors": ""}

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        manager = MagicMock()
        manager._last_session_id = "review-session"
        manager.continue_session.return_value = """{
  "result": "PASS",
  "summary": "Dynamic check passed",
  "findings": []
}"""

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=manager)

        assert result.thread_dispositions == []

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_run_adversarial_validation_dynamic_check_failure_routes_to_reviewer(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """Failing dynamic check is sent to the reviewer for semantic determination against the counterexample."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
            issue_requirements=[IssueRequirement(requirement_id="REQ-001-test", text="Must do X")],
        )
        mock_run_prompt.side_effect = [
            """{
  "result": "INCONCLUSIVE",
  "summary": "Need dynamic verification",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": []
}""",
            """{
  "result": "NEEDS_FIX",
  "summary": "Test failure confirmed the suspected specification violation",
  "findings": [
    {
      "finding_identity": "test-finding",
      "correction_identity": "test-correction",
      "violated_requirement": "State reload invariant",
          "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
          "reachability": "The focused reload test executes the production reload path",
          "required_behavior": "Preserve reloaded state",
          "actual_behavior": "Reload produces X",
          "evidence": "The supplied path or focused dynamic check demonstrates X",
      "counterexample": "Given state S, produces X",
      "test_gap": "Test failed on reload",
        "suggested_regression_scenario": "Fix reload logic",
        "anchor_path": "src/feature.py"
    }
  ]
}""",
        ]
        mock_run_tests.return_value = {
            "success": False,
            "output": "FAILED tests/test_feature.py::test_reload",
            "errors": "AssertionError: 1 != 2",
        }

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        manager = MagicMock()
        manager._last_session_id = "review-session"
        manager.continue_session.return_value = """{
  "result": "NEEDS_FIX",
  "summary": "Test failure confirmed the suspected specification violation",
  "findings": [{
    "finding_identity": "test-finding",
    "correction_identity": "test-correction",
      "violated_requirement": "State reload invariant",
          "requirement_id": "REQ-001-test",
      "evidence_classification": "DEMONSTRATED",
          "reachability": "The focused reload test executes the production reload path",
          "required_behavior": "Preserve reloaded state",
          "actual_behavior": "Reload produces X",
          "evidence": "The supplied path or focused dynamic check demonstrates X",
    "counterexample": "Given state S, produces X",
    "test_gap": "Test failed on reload",
    "suggested_regression_scenario": "Fix reload logic",
    "anchor_path": "src/feature.py"
  }]
}"""
        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=manager)
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1
        assert mock_run_prompt.call_count == 1
        manager.continue_session.assert_called_once()

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_run_adversarial_validation_dynamic_check_exception_fails_closed(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = '{"result": "INCONCLUSIVE", "summary": "Need dynamic test", "dynamic_check_requested": "tests/test_feature.py", "findings": []}'
        mock_run_tests.side_effect = RuntimeError("Test runner crashed")

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "could not be completed" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_dynamic_check_exception_discards_initial_thread_dispositions(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """If the dynamic check that would re-adjudicate a claimed thread
        fails before a final disposition is obtained, the provisional initial
        disposition must not survive: it must never be able to resolve a
        thread when the PR-level result itself is BLOCKED."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "Need dynamic verification of claimed thread",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [],
  "thread_dispositions": [
    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "Looks fixed", "evidence": "Code inspection"}
  ]
}"""
        mock_run_tests.side_effect = RuntimeError("Test runner crashed")

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())

        assert result.result == "BLOCKED"
        assert result.thread_dispositions == []


class TestParseThreadDispositions:
    """Tests for parsing the optional `thread_dispositions` field (issue #1619)."""

    def test_valid_thread_dispositions_are_parsed(self):
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {
                        "thread_id": "thread-1",
                        "status": "addressed",
                        "rationale": "The counter reset bug is fixed in the current head",
                        "evidence": "Traced the retry path in counter.py:42; it now resets before increment",
                    }
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert result.result == "PASS"
        assert len(result.thread_dispositions) == 1
        disposition = result.thread_dispositions[0]
        assert disposition.thread_id == "thread-1"
        assert disposition.status == "ADDRESSED"
        assert "counter reset" in disposition.rationale

    def test_missing_thread_dispositions_defaults_to_empty(self):
        json_resp = '{"result": "PASS", "summary": "Looks good", "findings": []}'
        result = parse_adversarial_validation_response(json_resp)
        assert result.thread_dispositions == []

    def test_invalid_status_entry_is_dropped_without_failing_pr_verdict(self):
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "FIXED", "rationale": "x", "evidence": "y"},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert result.result == "PASS"
        assert result.thread_dispositions == []

    def test_missing_rationale_or_evidence_entry_is_dropped(self):
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "", "evidence": "y"},
                    {"thread_id": "thread-2", "status": "ADDRESSED", "rationale": "x", "evidence": ""},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert result.thread_dispositions == []

    def test_thread_dispositions_not_a_list_is_ignored(self):
        json_resp = json.dumps({"result": "PASS", "summary": "Looks good", "findings": [], "thread_dispositions": "not-a-list"})
        result = parse_adversarial_validation_response(json_resp)
        assert result.result == "PASS"
        assert result.thread_dispositions == []

    def test_duplicate_thread_id_invalidates_the_thread(self):
        """A contradictory/duplicate thread_id (e.g. ADDRESSED then STILL_VALID
        for the same thread) must not resolve to whichever entry came first;
        the thread must receive no valid disposition at all (fail-closed)."""
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "first", "evidence": "first"},
                    {"thread_id": "thread-1", "status": "STILL_VALID", "rationale": "second", "evidence": "second"},
                    {"thread_id": "thread-2", "status": "ADDRESSED", "rationale": "ok", "evidence": "ok"},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert [d.thread_id for d in result.thread_dispositions] == ["thread-2"]

    def test_duplicate_thread_id_invalidates_thread_even_when_one_entry_is_malformed(self):
        """A duplicate thread_id must be detected from the raw list before
        per-entry validation: a valid ADDRESSED plus a malformed duplicate
        (e.g. empty evidence) for the same ID must still yield no disposition
        for that thread, not silently keep the one valid entry."""
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "valid", "evidence": "valid"},
                    {"thread_id": "thread-1", "status": "STILL_VALID", "rationale": "malformed dup", "evidence": ""},
                    {"thread_id": "thread-2", "status": "ADDRESSED", "rationale": "ok", "evidence": "ok"},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert [d.thread_id for d in result.thread_dispositions] == ["thread-2"]

    def test_still_valid_and_inconclusive_statuses_parse(self):
        json_resp = json.dumps(
            {
                "result": "PASS",
                "summary": "Looks good",
                "findings": [],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "STILL_VALID", "rationale": "r1", "evidence": "e1"},
                    {"thread_id": "thread-2", "status": "INCONCLUSIVE", "rationale": "r2", "evidence": "e2"},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        statuses = {d.thread_id: d.status for d in result.thread_dispositions}
        assert statuses == {"thread-1": "STILL_VALID", "thread-2": "INCONCLUSIVE"}

    def test_a_thread_addressed_is_independent_of_pr_needs_fix(self):
        """REQ-005 / AC-007: a thread may be ADDRESSED even when the PR itself
        needs fixes for an unrelated defect."""
        json_resp = json.dumps(
            {
                "result": "NEEDS_FIX",
                "summary": "Unrelated defect found",
                "findings": [
                    {
                        "requirement_id": "REQ-001",
                        "finding_identity": "test-finding",
                        "correction_identity": "test-correction",
                        "violated_requirement": "Must validate input",
                        "evidence_classification": "DEMONSTRATED",
                        "reachability": "call foo() with bad input",
                        "required_behavior": "reject bad input",
                        "actual_behavior": "accepts bad input",
                        "evidence": "foo.py:10",
                        "counterexample": "Given bad input, when foo() runs, then it should reject, but it accepts, and tests still pass because no test covers it",
                        "anchor_path": "foo.py",
                    }
                ],
                "thread_dispositions": [
                    {"thread_id": "thread-1", "status": "ADDRESSED", "rationale": "r1", "evidence": "e1"},
                ],
            }
        )
        result = parse_adversarial_validation_response(json_resp)
        assert result.result == "NEEDS_FIX"
        assert result.thread_dispositions[0].status == "ADDRESSED"
