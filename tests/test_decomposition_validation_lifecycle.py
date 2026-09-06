"""Generation-bound parent submission lifecycle and production-path tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import AffectedIssue, DecompositionAnalysisResult, DecompositionFinding, DecompositionIssue
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.implementation_slots import ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.util.gh_cache import GitHubClient, is_implementation_ready

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
    assert set(order[:2]) == {"set", "child"}
    assert order[-1] == "dispatch"
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
    assert set(order[:-1]) == {"set", "individual:11", "individual:12"}
    assert order[-1] == "dispatch:11"


def test_labeled_child_cannot_bypass_current_set_block(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY, ready=True)
    child["parent_issue_url"] = "https://api.github.com/repos/owner/repo/issues/10"
    github = relationship_github(parent, [child])
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = configured_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), individual)
    normalized = GitHubClient.get_issue_details(github, child)
    assert normalized["parent_issue_number"] == 10
    github.remove_labels.side_effect = RuntimeError("label unavailable")
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", normalized, 0, issue_number=11), engine.config)
    assert "blocked parent/child decomposition" in result.actions[0]
    individual.assert_called_once()
    dispatch.assert_not_called()


def test_stale_candidate_without_parent_hint_obeys_live_set_block(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY, ready=True)
    candidate = GitHubClient.get_issue_details(MagicMock(), child)
    assert candidate["parent_issue_number"] is None
    child["parent_issue_url"] = "https://api.github.com/repos/owner/repo/issues/10"
    github = relationship_github(parent, [child])
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = configured_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), individual)
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", candidate, 0, issue_number=11), engine.config)
    assert "blocked parent/child decomposition" in result.actions[0]
    individual.assert_called_once()
    dispatch.assert_not_called()


def test_parent_routing_labeled_child_cannot_bypass_current_set_block(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    parent["sub_issues_summary"] = {"total": 1}
    child = issue(11, "Child", CHILD_BODY, ready=True)
    github = relationship_github(parent, [child])
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = configured_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)), individual)
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, parent), 0, issue_number=10), engine.config)
    assert "blocked parent/child decomposition" in result.actions[0]
    individual.assert_called_once()
    dispatch.assert_not_called()


def test_stale_empty_parent_membership_hint_cannot_authorize_standalone_dispatch(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    parent["sub_issues_summary"] = {"total": 0}
    child = issue(11, "Child", CHILD_BODY)
    normalized_before_child_addition = GitHubClient.get_issue_details(MagicMock(), parent)
    github = relationship_github(parent, [child])
    events = []
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: events.append("set") or DecompositionAnalysisResult("READY"),
        lambda manifest, _body: events.append(f"individual:{manifest.issue_number}") or SpecificationAnalysisResult("READY"),
    )
    with patch.object(
        engine,
        "_process_single_candidate_reserved",
        side_effect=lambda _repo, candidate, *_args, **_kwargs: events.append(f"dispatch:{candidate.data['number']}") or CandidateProcessingResult("issue", candidate.data["number"], candidate.data["title"], True, ["dispatched"], None),
    ):
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", normalized_before_child_addition, 0, issue_number=10), engine.config)
    assert result.success
    assert set(events[:2]) == {"set", "individual:11"}
    assert events[-1] == "dispatch:11"


def test_child_added_during_individual_validation_invalidates_standalone_parent(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY)
    members = []
    github = relationship_github(parent, members)
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(item) for item in members] if number == 10 else []
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(parent if number == 10 else child)
    events = []

    def analyze_individual(manifest, _body):
        events.append(f"individual:{manifest.issue_number}")
        if manifest.issue_number == 10:
            child["parent_issue_url"] = "https://api.github.com/repos/owner/repo/issues/10"
            members.append(child)
        return SpecificationAnalysisResult("READY")

    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: events.append("set") or DecompositionAnalysisResult("READY"),
        analyze_individual,
    )
    candidate = Candidate("issue", GitHubClient.get_issue_details(github, parent), 0, issue_number=10)
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        first = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert first.actions == ["Skipped - validated Issue generation is stale or no longer submitted"]
    dispatch.assert_not_called()
    assert events == ["individual:10"]

    with patch.object(
        engine,
        "_process_single_candidate_reserved",
        return_value=CandidateProcessingResult("issue", 11, "Child", True, ["dispatched"], None),
    ):
        second = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert second.success
    assert events[0] == "individual:10"
    assert set(events[1:]) == {"set", "individual:11"}


def test_daemon_normalization_with_closed_children_routes_parent_submission(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    parent.update(
        {
            "assignees": [],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/issues/10",
            "user": {"login": "author", "id": 1},
            "comments": 0,
        }
    )
    child = issue(11, "Child", CHILD_BODY, state="closed")
    api = MagicMock()
    api.issues.list_for_repo.return_value = [parent]
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_linked_prs = Mock(return_value=[])
    github.get_open_sub_issues = Mock(return_value=[])
    with patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=api):
        normalized = github.get_open_issues_json("owner/repo")
    assert normalized[0]["has_open_sub_issues"] is False
    github.get_direct_sub_issues_strict = Mock(return_value=[child])
    snapshots = {10: parent, 11: child}
    github.get_issue_dispatch_snapshot_strict = Mock(side_effect=lambda _repo, number: dict(snapshots[number]))
    events = []
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: events.append("set") or DecompositionAnalysisResult("READY"),
        lambda *_args: events.append("individual") or SpecificationAnalysisResult("READY"),
    )
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", normalized[0], 0, issue_number=10), engine.config)
    assert result.actions == ["Skipped - submitted parent has no open child eligible for sequential implementation"]
    assert events == ["set"]
    dispatch.assert_not_called()
    GitHubClient.reset_singleton()


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
    # Eager validation is independent of implementation ordering. At least the
    # earlier child may already execute; queued sibling completion is covered by
    # the scheduler production-path regression.
    assert 11 in {call.args[0].issue_number for call in child_analysis.call_args_list}
    dispatch.assert_not_called()


def test_reopened_predecessor_is_rechecked_before_later_sibling_admission(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    first = issue(11, "First", CHILD_BODY, state="closed")
    later = issue(12, "Later", CHILD_BODY)
    later["parent_issue_number"] = 10
    github = relationship_github(parent, [first, later])
    set_analysis = Mock(return_value=DecompositionAnalysisResult("READY"))

    def analyze_child(*_args):
        first["state"] = "open"
        return SpecificationAnalysisResult("READY")

    engine = configured_engine(tmp_path, github, set_analysis, analyze_child)
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", GitHubClient.get_issue_details(github, later), 0, issue_number=12), engine.config)
    assert result.actions == ["Skipped - validated Issue generation is stale or no longer submitted"]
    dispatch.assert_not_called()
    identity = engine._decomposition_validators["owner/repo"].identity(parent, [first, later])
    persisted = engine._decomposition_validators["owner/repo"].store.get(identity)
    assert persisted is not None and persisted.verdict == "READY"
    set_analysis.assert_called_once()


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


def test_parent_relationship_added_during_individual_validation_blocks_standalone_admission(tmp_path):
    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY, ready=True)
    attached = False
    github = MagicMock()

    def members(_repo, number):
        return [dict(child)] if attached and number == 10 else []

    def snapshot(_repo, number):
        current = dict(parent if number == 10 else child)
        if attached and number == 11:
            current["parent_issue_url"] = "https://api.github.com/repos/owner/repo/issues/10"
        return current

    github.get_direct_sub_issues_strict.side_effect = members
    github.get_issue_dispatch_snapshot_strict.side_effect = snapshot
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if attached and number == 11 else None
    github.get_issue_comments_strict.return_value = []
    github.has_linked_pr.return_value = False
    github.get_issue_timeline.return_value = []

    def analyze_child(*_args):
        nonlocal attached
        attached = True
        return SpecificationAnalysisResult("READY")

    set_analysis = Mock(return_value=DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)))
    engine = configured_engine(tmp_path, github, set_analysis, analyze_child)
    set_gate = engine._decomposition_validators["owner/repo"]
    parent_input, child_inputs = decomposition_issues(parent, [child])
    blocked_identity = set_gate.identity(parent, [child])
    assert set_gate.decide(blocked_identity, parent_input, child_inputs).verdict == "BLOCKED"
    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate("issue", GitHubClient.get_issue_details(github, child), 0, issue_number=11),
            engine.config,
        )

    assert result.actions == ["Skipped - validated Issue generation is stale or no longer submitted"]
    github.get_parent_issue_details_strict.assert_called()
    github.remove_labels.assert_called_once_with("owner/repo", 10, ["implementation-ready"], item_type="issue")
    dispatch.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()
    set_analysis.assert_called_once()


def test_stale_jules_replacement_enforces_parent_set_and_sibling_transitions(tmp_path):
    """The production daemon callback cannot bypass any hierarchy authorization."""
    from auto_coder.implementation_slots import ImplementationOwner

    scenarios = ("blocked-child", "became-parent", "reopened-predecessor")
    for scenario in scenarios:
        parent = issue(10, "Parent", PARENT_BODY, ready=True)
        first = issue(11, "First", CHILD_BODY, state="open")
        target_number = 10 if scenario == "became-parent" else (12 if scenario == "reopened-predecessor" else 11)
        target = issue(target_number, "Target", CHILD_BODY if target_number != 10 else PARENT_BODY, ready=True)
        children = [first, target] if target_number == 12 else ([target] if target_number == 11 else [first])
        github = relationship_github(parent if target_number != 10 else target, children)
        github.get_item_type_strict.return_value = "issue"
        github.get_issue.return_value = dict(target)
        github.get_issue_details.return_value = dict(target)
        github.has_linked_pr.return_value = False
        github.remove_labels.return_value = True
        set_result = DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)) if scenario == "blocked-child" else DecompositionAnalysisResult("READY")
        child_analysis = Mock(return_value=SpecificationAnalysisResult("READY"))
        engine = configured_engine(tmp_path / scenario, github, lambda *_args, value=set_result: value, child_analysis)
        slots = engine.implementation_slots
        assert slots is not None
        owner = ImplementationOwner("issue", target_number)
        slots.record_provider_session(owner, "stale-session")
        jules = MagicMock()
        jules.get_session.return_value = {"state": "COMPLETED"}
        jules.list_sessions.return_value = [
            {
                "name": "sessions/stale-session",
                "state": "IN_PROGRESS",
                "createTime": "2000-01-01T00:00:00Z",
                "outputs": {},
            }
        ]
        cloud = MagicMock()
        cloud.get_issue_by_session.return_value = target_number
        with (
            patch("auto_coder.issue_processor.JulesClient", return_value=jules),
            patch("auto_coder.issue_processor.CloudManager", return_value=cloud),
            patch("auto_coder.issue_processor.is_session_stopped", return_value=False),
            patch("auto_coder.issue_processor.increment_attempt") as increment,
            patch("auto_coder.issue_processor._take_issue_actions") as replacement,
        ):
            engine.handle_stale_jules_issue_sessions("owner/repo")

        replacement.assert_not_called()
        increment.assert_not_called()
        assert slots.active_execution_ids(owner) == ()
        if scenario in {"became-parent", "reopened-predecessor"}:
            child_analysis.assert_not_called()


@pytest.mark.parametrize(
    "child_ready,child_verdict,replacement_expected",
    [(False, "READY", True), (True, "BLOCKED", False)],
)
def test_stale_jules_inherited_readiness_and_blocked_effects(tmp_path, child_ready, child_verdict, replacement_expected):
    """The real daemon inherits readiness and targets parent withdrawal."""
    from auto_coder.implementation_slots import ImplementationOwner

    parent = issue(10, "Parent", PARENT_BODY, ready=True)
    child = issue(11, "Child", CHILD_BODY, ready=child_ready)
    github = relationship_github(parent, [child])
    github.get_item_type_strict.return_value = "issue"
    github.get_issue.return_value = dict(child)
    github.get_issue_details.return_value = dict(child)
    github.get_issue_comments_strict.return_value = []
    github.has_linked_pr.return_value = False
    github.remove_labels.return_value = True
    child_result = SpecificationAnalysisResult("BLOCKED", (CHILD_FINDING,)) if child_verdict == "BLOCKED" else SpecificationAnalysisResult("READY")
    engine = configured_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: child_result,
    )
    slots = engine.implementation_slots
    assert slots is not None
    owner = ImplementationOwner("issue", 11)
    slots.record_provider_session(owner, "stale-session")
    jules = MagicMock()
    jules.get_session.return_value = {"state": "COMPLETED"}
    jules.list_sessions.return_value = [
        {
            "name": "sessions/stale-session",
            "state": "IN_PROGRESS",
            "createTime": "2000-01-01T00:00:00Z",
            "outputs": {},
        }
    ]
    cloud = MagicMock()
    cloud.get_issue_by_session.return_value = 11
    with (
        patch("auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("auto_coder.issue_processor.CloudManager", return_value=cloud),
        patch("auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("auto_coder.issue_processor.increment_attempt", return_value=2) as increment,
        patch("auto_coder.issue_processor._take_issue_actions", return_value=["replacement"]) as replacement,
    ):
        engine.handle_stale_jules_issue_sessions("owner/repo")

    assert replacement.called is replacement_expected
    assert increment.called is replacement_expected
    assert slots.active_execution_ids(owner) == ()
    if child_verdict == "BLOCKED":
        github.remove_labels.assert_called_once_with("owner/repo", 10, ["implementation-ready"], item_type="issue")
    else:
        github.remove_labels.assert_not_called()
        assert not is_implementation_ready(child)
