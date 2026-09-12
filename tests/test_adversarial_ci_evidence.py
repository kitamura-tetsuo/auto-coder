from pathlib import Path

from auto_coder.adversarial_validator import canonical_pr_tests_succeeded, validate_dynamic_check_target
from auto_coder.ci_observation import CIConclusion, CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject, WorkflowExecutionIdentity, WorkflowObservation
from auto_coder.util.github_action import GitHubActionsStatusResult


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
