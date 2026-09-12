from pathlib import Path
from unittest.mock import patch

from auto_coder.adversarial_validator import canonical_pr_tests_succeeded, format_ci_execution_evidence, run_exact_head_dynamic_check, validate_dynamic_check_target
from auto_coder.automation_config import AutomationConfig
from auto_coder.ci_observation import CIConclusion, CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject, WorkflowExecutionIdentity, WorkflowObservation
from auto_coder.util.github_action import GitHubActionsStatusResult
from auto_coder.utils import CommandResult


def test_reusable_ci_requires_verified_canonical_workflow_path() -> None:
    fact = WorkflowObservation(WorkflowExecutionIdentity("1", "10", 1), CIConclusion.SUCCESS, "renamed", workflow_path=".github/workflows/pr-tests.yml")
    snapshot = CIObservationSnapshot(ObservationSubject("https://api.github.com", "owner/repo", 7, "a" * 40), ObservationRequest("github-actions", "checks+workflows"), "cycle", 1, ObservationAvailability.KNOWN, (fact,))
    assert canonical_pr_tests_succeeded(GitHubActionsStatusResult(success=True, ids=[10], observation=snapshot)) is True
    assert canonical_pr_tests_succeeded(GitHubActionsStatusResult(success=True, ids=[10], observation=CIObservationSnapshot(snapshot.subject, snapshot.request, "other", 1, ObservationAvailability.KNOWN, (WorkflowObservation(fact.execution, fact.conclusion, "PR Tests"),)))) is False


def test_dynamic_target_protocol_rejects_prose_before_execution(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests" / "test_feature.py"
    test_file.write_text("def test_case(): pass\n")
    assert validate_dynamic_check_target("run the dashboard tests", tmp_path) == "target contains prose, whitespace, or shell/control syntax"
    assert validate_dynamic_check_target("tests/test_feature.py::test_case", tmp_path) is None
    assert validate_dynamic_check_target("tests/../outside.py", tmp_path) == "target must resolve inside the repository tests/ tree"


def test_pytest_unknown_node_is_target_selection_error() -> None:
    sha = "a" * 40
    with patch("auto_coder.adversarial_validator.CommandExecutor") as executor_type:
        executor = executor_type.return_value
        executor.DEFAULT_TIMEOUTS = {"test": 60}
        executor.run_command.side_effect = [
            CommandResult(True, sha, "", 0),
            CommandResult(False, "collected 0 items", "ERROR: not found: tests/test_feature.py::missing", 4),
            CommandResult(True, sha, "", 0),
        ]
        result = run_exact_head_dynamic_check(AutomationConfig(), "tests/test_feature.py::missing", sha)

    assert result.target_selection_error == "pytest could not resolve the requested file/node or selected zero tests"
    assert result.verification_error is None


def test_collection_import_failure_is_not_target_selection_error() -> None:
    sha = "b" * 40
    with patch("auto_coder.adversarial_validator.CommandExecutor") as executor_type:
        executor = executor_type.return_value
        executor.DEFAULT_TIMEOUTS = {"test": 60}
        executor.run_command.side_effect = [
            CommandResult(True, sha, "", 0),
            CommandResult(False, "collected 0 items / 1 error", "ERROR collecting tests/test_feature.py\nImportError while importing", 4),
            CommandResult(True, sha, "", 0),
        ]
        result = run_exact_head_dynamic_check(AutomationConfig(), "tests/test_feature.py::missing", sha)

    assert result.target_selection_error is None
    assert result.success is False


def test_production_status_preserves_subject_and_distinct_execution_facts() -> None:
    subject = ObservationSubject("https://api.github.com", "owner/repo", 7, "a" * 40)
    request = ObservationRequest("github-actions", "checks+workflows")
    facts = (
        WorkflowObservation(WorkflowExecutionIdentity("1", "10", 1), CIConclusion.SUCCESS, workflow_path=".github/workflows/pr-tests.yml"),
        WorkflowObservation(WorkflowExecutionIdentity("1", "11", 1), CIConclusion.PENDING, workflow_path=".github/workflows/other.yml"),
    )
    snapshot = CIObservationSnapshot(subject, request, "cycle-current", 4, ObservationAvailability.KNOWN, facts)
    status = GitHubActionsStatusResult(success=False, ids=[10, 11], in_progress=True, observation=snapshot)

    evidence = format_ci_execution_evidence(status)

    assert '"repository": "owner/repo"' in evidence
    assert '"cycle_id": "cycle-current"' in evidence
    assert '"run_id": "10"' in evidence
    assert '"run_id": "11"' in evidence
    assert '"conclusion": "pending"' in evidence
    assert evidence.count('"actionable": true') == 2
