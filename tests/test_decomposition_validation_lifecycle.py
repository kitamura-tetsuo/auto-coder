"""Generation-bound parent submission lifecycle and production-path tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import MagicMock, Mock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult, DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.implementation_slots import ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

PARENT_BODY = "## Requirements\n- REQ-001: Deliver both child behaviors."
CHILD_BODY = "## Requirements\n- REQ-001: Deliver the first behavior."


def issue(number, title, body, *, ready=False, state="open", issue_id=None):
    return {
        "id": issue_id or number * 10,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": "implementation-ready"}] if ready else [],
        "sub_issues_summary": {"total": 0},
    }


def decomposition_issues(parent, children):
    def adapt(item):
        return DecompositionIssue(build_normative_issue_manifest(item["number"], item["title"], item["body"]), item["body"])

    return adapt(parent), [adapt(child) for child in children]


def test_identity_is_order_independent_and_only_specification_inputs_invalidate(tmp_path):
    gate = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json")
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    first = issue(11, "First", CHILD_BODY)
    second = issue(12, "Second", CHILD_BODY, state="closed")

    baseline = gate.identity(parent, [first, second])
    assert baseline == gate.identity({**parent, "labels": [], "state": "closed"}, [{**second, "state": "open"}, first])
    assert baseline != gate.identity({**parent, "body": PARENT_BODY + "\nEdited"}, [first, second])
    assert baseline != gate.identity(parent, [{**first, "title": "Edited"}, second])
    assert baseline != gate.identity(parent, [first])


def test_ready_survives_restart_error_retries_and_concurrent_analysis_coalesces(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    children = [issue(11, "First", CHILD_BODY)]
    parent_input, child_inputs = decomposition_issues(parent, children)
    calls = Mock(side_effect=[DecompositionAnalysisResult("ERROR", error="outage"), DecompositionAnalysisResult("READY")])
    gate = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", calls)
    identity = gate.identity(parent, children)
    assert gate.decide(identity, parent_input, child_inputs).verdict == "ERROR"
    assert gate.decide(identity, parent_input, child_inputs).verdict == "READY"
    restarted = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", Mock(side_effect=AssertionError("must reuse")))
    assert restarted.decide(identity, parent_input, child_inputs).verdict == "READY"

    concurrent_path = tmp_path / "concurrent.json"
    barrier = Barrier(2)
    count = 0
    lock = Lock()

    def analyze(_parent, _children):
        nonlocal count
        with lock:
            count += 1
        return DecompositionAnalysisResult("READY")

    shared = DecompositionValidationLifecycle("owner/repo", "provider/model", concurrent_path, analyze)

    def run(_value):
        barrier.wait()
        return shared.decide(identity, parent_input, child_inputs).verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, range(2))) == ["READY", "READY"]
    assert count == 1


def test_parent_submission_reaches_set_then_child_validation_before_dispatch(tmp_path):
    """Production candidate processing inherits readiness without labeling the child."""
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    parent["sub_issues_summary"] = {"total": 1}
    github = MagicMock()
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if number == 11 else None
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(parent if number == 10 else child)
    github.has_linked_pr.return_value = False
    github.get_issue_timeline.return_value = []

    order = []

    def analyze_set(_parent, _children):
        order.append("set")
        return DecompositionAnalysisResult("READY")

    def analyze_child(_manifest, _body):
        order.append("child")
        return SpecificationAnalysisResult("READY")

    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", analyze_set)
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", analyze_child)
    dispatched = CandidateProcessingResult("issue", 11, "Child", True, ["dispatched"], None)

    with patch.object(engine, "_process_single_candidate_reserved", side_effect=lambda *_args, **_kwargs: order.append("dispatch") or dispatched):
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", parent, 0, issue_number=10), engine.config)

    assert result.success is True
    assert order == ["set", "child", "dispatch"]
    github.remove_labels.assert_not_called()
    assert child["labels"] == []


def test_parent_readiness_removed_during_final_check_prevents_child_dispatch(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    github = MagicMock()
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_parent_issue_details_strict.return_value = dict(parent)

    parent_reads = 0

    def snapshot(_repo, number):
        nonlocal parent_reads
        if number == 10:
            parent_reads += 1
            current = dict(parent)
            if parent_reads >= 2:
                current["labels"] = []
            return current
        return dict(child)

    github.get_issue_dispatch_snapshot_strict.side_effect = snapshot
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", lambda *_args: DecompositionAnalysisResult("READY"))
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", lambda *_args: SpecificationAnalysisResult("READY"))
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", child, 0, issue_number=11), engine.config)

    assert result.success is False
    assert "stale" in result.actions[0]
    dispatch.assert_not_called()
