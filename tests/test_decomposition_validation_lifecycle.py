"""Generation-bound parent submission lifecycle and production-path tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import MagicMock, Mock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import AffectedIssue, DecompositionAnalysisResult, DecompositionFinding, DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.implementation_slots import ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.util.gh_cache import GitHubClient

PARENT_BODY = "## Requirements\n- REQ-001: Deliver both child behaviors."
CHILD_BODY = "## Requirements\n- REQ-001: Deliver the first behavior."
SET_FINDING = DecompositionFinding(
    "missing_requirement_ownership",
    (AffectedIssue(10, ("REQ-001",)),),
    "No child owns the behavior.",
    "Assign the behavior to a child.",
)
CHILD_FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "Behavior is ambiguous.", "Define it.", "", "")


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


def configured_engine(tmp_path, github, set_result, child_result):
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", set_result)
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", child_result)
    return engine


def relationship_github(parent, children):
    github = MagicMock()
    snapshots = {parent["number"]: parent, **{child["number"]: child for child in children}}
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child) for child in children] if number == parent["number"] else []
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if number in snapshots and number != parent["number"] else None
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_comments_strict.return_value = []
    github.has_linked_pr.return_value = False
    github.get_issue_timeline.return_value = []
    return github


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


def test_explicit_parent_adapter_preserves_membership_and_never_dispatches_parent(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    children = [issue(11, "First", CHILD_BODY), issue(12, "Second", CHILD_BODY)]
    parent["sub_issues_summary"] = {"total": 2}
    github = relationship_github(parent, children)
    order = []
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: order.append("set") or DecompositionAnalysisResult("READY"),
        lambda manifest, _body: order.append(f"individual:{manifest.issue_number}") or SpecificationAnalysisResult("READY"),
    )
    normalized = GitHubClient.get_issue_details(github, parent)
    assert normalized["sub_issues_summary"] == {"total": 2}
    with patch.object(
        engine,
        "_process_single_candidate_reserved",
        side_effect=lambda _repo, candidate, *_args, **_kwargs: order.append(f"dispatch:{candidate.data['number']}") or CandidateProcessingResult("issue", candidate.data["number"], candidate.data["title"], True, ["dispatched"], None),
    ):
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", normalized, 0, issue_number=10), engine.config)
    assert result.success
    assert order == ["set", "individual:11", "dispatch:11"]


def test_labeled_child_cannot_bypass_current_set_block(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY, ready=True)
    child["parent_issue_number"] = 10
    github = relationship_github(parent, [child])
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = configured_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), individual)
    normalized = GitHubClient.get_issue_details(github, child)
    github.remove_labels.side_effect = RuntimeError("label unavailable")
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", normalized, 0, issue_number=11), engine.config)
    assert "blocked parent/child decomposition" in result.actions[0]
    individual.assert_not_called()
    dispatch.assert_not_called()


def test_explicit_later_sibling_waits_after_set_validation(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    first = issue(11, "First", CHILD_BODY)
    later = issue(12, "Later", CHILD_BODY)
    later["parent_issue_number"] = 10
    github = relationship_github(parent, [first, later])
    order = []
    child_analysis = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = configured_engine(tmp_path, github, lambda *_args: order.append("set") or DecompositionAnalysisResult("READY"), child_analysis)
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, later), 0, issue_number=12), engine.config)
    assert order == ["set"]
    assert result.actions == ["Deferred - earlier sibling(s) remain open: [11]"]
    child_analysis.assert_not_called()
    dispatch.assert_not_called()


def test_set_blocked_comment_failure_still_withdraws_parent(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    child["parent_issue_number"] = 10
    github = relationship_github(parent, [child])
    github.add_comment_to_issue.side_effect = RuntimeError("comment unavailable")
    engine = configured_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), lambda *_args: SpecificationAnalysisResult("READY"))
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, child), 0, issue_number=11), engine.config)
    github.remove_labels.assert_called_once_with("owner/repo", 10, ["implementation-ready"], item_type="issue")
    assert "findings publication failed" in str(result.error)
    dispatch.assert_not_called()


def test_inherited_child_blocked_comment_failure_still_withdraws_parent(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    child["parent_issue_number"] = 10
    github = relationship_github(parent, [child])
    github.add_comment_to_issue.side_effect = RuntimeError("comment unavailable")
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: SpecificationAnalysisResult("BLOCKED", (CHILD_FINDING,)),
    )
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, child), 0, issue_number=11), engine.config)
    github.remove_labels.assert_called_once_with("owner/repo", 10, ["implementation-ready"], item_type="issue")
    assert "findings publication failed" in str(result.error)
    dispatch.assert_not_called()


def test_stale_set_block_during_comment_lookup_has_no_effect_and_revalidates(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    sibling = issue(12, "Sibling", CHILD_BODY, state="closed")
    child["parent_issue_number"] = 10
    github = relationship_github(parent, [child, sibling])
    calls = Mock(side_effect=[DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), DecompositionAnalysisResult("READY")])

    def edit_sibling(*_args):
        sibling["body"] += "\nEdited"
        return []

    github.get_issue_comments_strict.side_effect = edit_sibling
    engine = configured_engine(tmp_path, github, calls, lambda *_args: SpecificationAnalysisResult("READY"))
    candidate = Candidate("issue", GitHubClient.get_issue_details(github, child), 0, issue_number=11)
    first = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert "blocked" in first.actions[0]
    github.add_comment_to_issue.assert_not_called()
    github.remove_labels.assert_not_called()
    with patch.object(
        engine,
        "_process_single_candidate_reserved",
        return_value=CandidateProcessingResult("issue", 11, "Child", True, ["dispatched"], None),
    ):
        second = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert second.success
    assert calls.call_count == 2


def test_stale_child_block_during_comment_lookup_cannot_withdraw_revised_set(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    sibling = issue(12, "Sibling", CHILD_BODY, state="closed")
    child["parent_issue_number"] = 10
    github = relationship_github(parent, [child, sibling])

    def edit_sibling(*_args):
        sibling["body"] += "\nEdited"
        return []

    github.get_issue_comments_strict.side_effect = edit_sibling
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: SpecificationAnalysisResult("BLOCKED", (CHILD_FINDING,)),
    )
    result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, child), 0, issue_number=11), engine.config)
    assert "blocked specification" in result.actions[0]
    github.add_comment_to_issue.assert_not_called()
    github.remove_labels.assert_not_called()


def test_strict_membership_keeps_closed_child_and_reuses_ready_after_restart(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    first = issue(11, "First", CHILD_BODY, state="open")
    second = issue(12, "Second", CHILD_BODY)
    members = [first, second]

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [dict(member) for member in members]

    client = MagicMock()
    client.__enter__.return_value.get.return_value = Response()
    github = MagicMock()
    strict_client = object.__new__(GitHubClient)
    strict_client.token = "token"
    github.get_direct_sub_issues_strict.side_effect = lambda repo, number: GitHubClient.get_direct_sub_issues_strict(strict_client, repo, number)
    snapshots = {10: parent, 11: first, 12: second}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    calls = Mock(return_value=DecompositionAnalysisResult("READY"))
    with patch("auto_coder.util.gh_cache.httpx.Client", return_value=client):
        engine = AutomationEngine(github, AutomationConfig())
        initial = engine._fetch_authoritative_decomposition_set("owner/repo", 10)
        assert initial is not None and [item["number"] for item in initial[1]] == [11, 12]
        gate = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", calls)
        parent_input, child_inputs = decomposition_issues(*initial)
        identity = gate.identity(*initial)
        assert gate.decide(identity, parent_input, child_inputs).verdict == "READY"
        first["state"] = "closed"
        restarted_engine = AutomationEngine(github, AutomationConfig())
        after_close = restarted_engine._fetch_authoritative_decomposition_set("owner/repo", 10)
        assert after_close is not None and [item["number"] for item in after_close[1]] == [11, 12]
        restarted = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", Mock(side_effect=AssertionError("must reuse")))
        assert restarted.identity(*after_close) == identity
        assert restarted.decide(identity, parent_input, child_inputs).verdict == "READY"
    assert calls.call_count == 1


def test_engine_validator_model_change_invalidates_persisted_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", str(tmp_path))
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    children = [issue(11, "Child", CHILD_BODY)]
    parent_input, child_inputs = decomposition_issues(parent, children)
    with patch("auto_coder.automation_engine.configured_provider_identity", return_value="provider/model-a"):
        first_engine = AutomationEngine(MagicMock(), AutomationConfig())
        first_gate = first_engine._get_decomposition_validator("owner/repo")
    first_gate.analyzer = lambda *_args: DecompositionAnalysisResult("READY")
    first_identity = first_gate.identity(parent, children)
    assert first_gate.decide(first_identity, parent_input, child_inputs).verdict == "READY"

    calls = Mock(return_value=DecompositionAnalysisResult("READY"))
    with patch("auto_coder.automation_engine.configured_provider_identity", return_value="provider/model-b"):
        restarted_engine = AutomationEngine(MagicMock(), AutomationConfig())
        restarted_gate = restarted_engine._get_decomposition_validator("owner/repo")
    restarted_gate.analyzer = calls
    changed_identity = restarted_gate.identity(parent, children)
    assert changed_identity != first_identity
    assert restarted_gate.decide(changed_identity, parent_input, child_inputs).verdict == "READY"
    calls.assert_called_once()


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
