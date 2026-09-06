"""Generation-bound decomposition authorization regressions."""

from unittest.mock import Mock

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult, DecompositionIssue
from auto_coder.implementation_slots import ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import DecompositionValidationLifecycle, SpecificationValidationLifecycle


def issue(number: int, title: str, requirement: str) -> DecompositionIssue:
    body = f"## Requirements\n- REQ-001: {requirement}"
    return DecompositionIssue(build_normative_issue_manifest(number, title, body), body)


def test_ready_is_reused_only_for_exact_parent_child_generation(tmp_path):
    analyze = Mock(return_value=DecompositionAnalysisResult("READY"))
    path = tmp_path / "decompositions.json"
    parent = issue(10, "Parent", "Children collectively deliver the feature.")
    child = issue(11, "Child", "Deliver the behavior.")
    lifecycle = DecompositionValidationLifecycle("owner/repo", "provider/model", path, analyze)

    assert lifecycle.decide(parent, (child,)).verdict == "READY"
    assert lifecycle.decide(parent, (child,)).verdict == "READY"
    changed_child = issue(11, "Edited child", "Deliver the behavior.")
    assert lifecycle.decide(parent, (changed_child,)).verdict == "READY"
    assert analyze.call_count == 2

    restarted = DecompositionValidationLifecycle("owner/repo", "provider/model", path, Mock(side_effect=AssertionError("must reuse")))
    assert restarted.decide(parent, (child,)).verdict == "READY"


def test_error_is_not_persisted_and_membership_changes_generation(tmp_path):
    analyze = Mock(side_effect=[DecompositionAnalysisResult("ERROR", error="outage"), DecompositionAnalysisResult("READY"), DecompositionAnalysisResult("READY")])
    lifecycle = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "decompositions.json", analyze)
    parent = issue(10, "Parent", "Children collectively deliver the feature.")
    children = (issue(11, "First", "Deliver the first behavior."),)

    assert lifecycle.decide(parent, children).verdict == "ERROR"
    assert lifecycle.decide(parent, children).verdict == "READY"
    assert lifecycle.decide(parent, (*children, issue(12, "Second", "Deliver the second behavior."))).verdict == "READY"
    assert analyze.call_count == 3


def test_parent_submission_reaches_child_dispatch_without_child_label(tmp_path):
    """Exercise the common production dispatch boundary, not only the store."""

    class GitHub:
        snapshots = {
            10: {"number": 10, "title": "Parent", "body": "## Requirements\n- REQ-001: Child delivers behavior.", "state": "open", "labels": ["implementation-ready"]},
            11: {"number": 11, "title": "Child", "body": "## Requirements\n- REQ-001: Deliver behavior.", "state": "open", "labels": []},
        }

        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return dict(self.snapshots[number])

        def get_parent_issue_number_strict(self, _repo, number):
            return 10 if number == 11 else None

        def get_all_sub_issues_strict(self, _repo, _number):
            return [11]

        def get_open_sub_issues(self, _repo, number):
            return [11] if number == 10 else []

    github = GitHub()
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "individual.json", lambda _manifest, _body: SpecificationAnalysisResult("READY"))
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "set.json", lambda _parent, _children: DecompositionAnalysisResult("READY"))
    dispatched = Mock(return_value=CandidateProcessingResult("issue", 11, "Child", True, ["dispatched"]))
    engine._process_single_candidate_reserved = dispatched

    result = engine._process_single_candidate_unified(
        "owner/repo",
        Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11),
        engine.config,
    )

    assert result.actions == ["dispatched"]
    dispatched.assert_called_once()
