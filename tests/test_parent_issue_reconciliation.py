from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from src.auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from src.auto_coder.parent_issue_reconciliation import ParentDeclarationStatus, parse_parent_declaration
from src.auto_coder.specification_analyzer import SpecificationAnalysisResult
from src.auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from src.auto_coder.util.gh_cache import GitHubClient


class GraphGitHub(GitHubClient):
    def __init__(self, issues, parents, children):
        self.issues = issues
        self.parents = parents
        self.children = children
        self.events = []
        self.comments = []
        self.removals = []

    def get_issue_dispatch_snapshot_strict(self, _repo, number):
        return dict(self.issues[number])

    def get_parent_issue_details_strict(self, _repo, number):
        parent = self.parents.get(number)
        return dict(self.issues[parent]) if parent is not None else None

    def get_direct_sub_issues_strict(self, _repo, number):
        return [dict(self.issues[child]) for child in self.children.get(number, [])]

    def add_sub_issue_strict(self, _repo, parent, child, _child_id):
        self.events.append("linked")
        self.parents[child] = parent
        self.children.setdefault(parent, []).append(child)

    def get_issue_comments_strict(self, _repo, _number):
        return list(self.comments)

    def add_comment_to_issue(self, _repo, number, body):
        self.comments.append({"number": number, "body": body})

    def remove_labels(self, _repo, number, labels, item_type="issue"):
        self.removals.append((number, labels, item_type))
        self.issues[number]["labels"] = []


def graph_issue(number, body, ready=False, state="open", created_at=None):
    return {
        "id": number * 100,
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "state": state,
        "labels": [{"name": "implementation-ready"}] if ready else [],
        "user": {"id": 1},
        "created_at": created_at or "2020-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("key", ["Parent-Issue", "parent_issue", "PARENT ISSUE"])
def test_parser_accepts_all_supported_keys_and_equivalent_repetitions(key: str):
    declaration = parse_parent_declaration(f" {key}: #17 \nparent-issue: 17")
    assert declaration.status is ParentDeclarationStatus.SUPPORTED
    assert declaration.parent_number == 17


@pytest.mark.parametrize(
    "body",
    [
        "Parent-Issue: #abc",
        "Parent-Issue: 0",
        "Parent-Issue: #1 trailing",
        "Parent-Issue: #1\nparent issue: #2",
    ],
)
def test_parser_rejects_every_malformed_or_ambiguous_candidate(body: str):
    declaration = parse_parent_declaration(body)
    assert declaration.status is ParentDeclarationStatus.INVALID
    assert declaration.parent_number is None


def test_webhook_child_reconciles_and_refetches_before_eager_validation(tmp_path: Path):
    """The supported invalidation origin preserves reconciliation ordering."""
    body = "## Requirements\n- REQ-001: Keep the graph authoritative."
    initial_child = {"id": 202, "number": 2, "title": "Child", "body": body + "\nParent-Issue: #1", "state": "open", "labels": []}
    parent = {"id": 101, "number": 1, "title": "Parent", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github = GraphGitHub({1: parent, 2: initial_child}, {}, {})
    events: list[str] = []
    original_link = github.add_sub_issue_strict

    def link(*args):
        events.append("linked")
        original_link(*args)

    github.add_sub_issue_strict = link

    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: events.append("decomposition") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: events.append("individual") or SpecificationAnalysisResult("READY"))

    engine._validate_submitted_parent_generation_for_child("o/r", 2, initial_child)

    assert events[0] == "linked"
    assert set(events[1:]) == {"decomposition", "individual"}
    assert github.parents == {2: 1}


def test_invalid_marker_blocks_common_dispatch_without_side_effects():
    issue = {"id": 202, "number": 2, "title": "Child", "body": "Parent-Issue: #abc", "state": "open", "labels": [{"name": "implementation-ready"}], "user": {"id": 1}}
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_parent_issue_details_strict = MagicMock()
    github.get_issue_dispatch_snapshot_strict = MagicMock(return_value=dict(issue))
    github.add_sub_issue_strict = MagicMock()
    github.remove_labels = MagicMock()
    github.add_comment_to_issue = MagicMock()
    github.get_parent_issue_details_strict.return_value = None
    engine = AutomationEngine(github, AutomationConfig())
    engine._is_issue_author_allowed = MagicMock(return_value=True)

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("o/r", Candidate(type="issue", data=issue, priority=0), engine.config)

    assert result.error == "Parent-Issue reconciliation blocked processing: malformed Parent-Issue declaration"
    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    implementation.assert_not_called()
    github.add_sub_issue_strict.assert_not_called()
    github.remove_labels.assert_not_called()
    github.add_comment_to_issue.assert_not_called()


@pytest.mark.parametrize("child_body", ["Parent-Issue: #3", "Parent-Issue: #abc"])
def test_explicit_parent_reconciles_closed_children_before_any_validation(tmp_path: Path, child_body: str):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    github = GraphGitHub(
        {1: graph_issue(1, body, ready=True), 2: graph_issue(2, body + "\n" + child_body, state="closed"), 3: graph_issue(3, body)},
        {2: 1},
        {1: [2]},
    )
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("child") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)

    assert result.error is not None
    assert analyzed == []
    assert github.events == []
    assert github.parents == {2: 1}


def test_fresh_authoritative_marker_is_reconciled_before_stale_candidate_validation(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    stale = graph_issue(2, body, ready=True)
    current = graph_issue(2, body + "\nParent-Issue: #1", ready=True)
    github = GraphGitHub({1: graph_issue(1, body), 2: current}, {}, {})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("individual") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", stale, 0), engine.config)

    assert github.events == ["linked"]
    assert github.parents == {2: 1}
    assert analyzed == []
    assert result.actions == ["Skipped - authoritative parent is missing implementation-ready label"]


def test_explicit_new_parent_waits_for_creation_window_and_uses_latest_body(tmp_path: Path, monkeypatch):
    body = "## Requirements\n- REQ-001: Initial."
    created = datetime.now(timezone.utc)
    issues = {1: graph_issue(1, body, ready=True, created_at=created.isoformat()), 2: graph_issue(2, body, state="closed")}
    github = GraphGitHub(issues, {2: 1}, {1: [2]})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda parent, _children: analyzed.append(parent.body) or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: SpecificationAnalysisResult("READY"))

    first = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(issues[1]), 0), engine.config)
    assert first.actions == ["Deferred - readiness submission is in its initial stabilization window"]
    assert analyzed == []

    issues[1]["body"] = body + "\nLatest."
    monkeypatch.setattr("src.auto_coder.automation_engine.time.time", lambda: (created + timedelta(seconds=61)).timestamp())
    second = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(issues[1]), 0), engine.config)
    assert analyzed == [issues[1]["body"]]
    assert second.actions == ["Skipped - submitted parent has no open child eligible for sequential implementation"]


def test_ready_native_leaf_under_unready_parent_starts_no_analyzer(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    github = GraphGitHub({1: graph_issue(1, body), 2: graph_issue(2, body, ready=True)}, {2: 1}, {1: [2]})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("individual") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[2]), 0), engine.config)

    assert analyzed == []
    assert result.actions == ["Skipped - authoritative parent is missing implementation-ready label"]


def test_standalone_blocked_completion_cannot_act_after_child_is_added(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    github = GraphGitHub({1: graph_issue(1, body, ready=True), 2: graph_issue(2, body, state="closed")}, {}, {})
    analyzed = []

    def block_and_add_child(*_args):
        analyzed.append("individual")
        github.parents[2] = 1
        github.children[1] = [2]
        from src.auto_coder.specification_analyzer import SpecificationFinding

        finding = SpecificationFinding("material_ambiguity", ("REQ-001",), "Ambiguous.", "Clarify it.", "Two outcomes.", "Required outcome.")
        return SpecificationAnalysisResult("BLOCKED", (finding,))

    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", block_and_add_child)
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))

    first = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)
    assert first.actions == ["Rejected - blocked specification"]
    assert github.comments == []
    assert github.removals == []
    assert github.issues[1]["labels"] == [{"name": "implementation-ready"}]

    second = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)
    assert analyzed.count("individual") == 2
    assert analyzed.count("set") == 1
    assert second.actions == ["Skipped - submitted parent has no open child eligible for sequential implementation"]


def test_ambiguous_422_is_operational_and_preserves_submission(monkeypatch):
    body = "## Requirements\n- REQ-001: Preserve the graph.\nParent-Issue: #1"
    child = graph_issue(2, body, ready=True)
    parent = graph_issue(1, body.replace("\nParent-Issue: #1", ""))
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_issue_dispatch_snapshot_strict = MagicMock(side_effect=lambda _repo, number: dict(parent if number == 1 else child))
    github.get_parent_issue_details_strict = MagicMock(return_value=None)
    github.remove_labels = MagicMock()
    github.add_comment_to_issue = MagicMock()
    request = httpx.Request("POST", "https://api.github.com/repos/o/r/issues/1/sub_issues")
    response = httpx.Response(422, request=request, json={"message": "Validation failed, or the endpoint has been spammed."})
    context = MagicMock()
    context.__enter__.return_value.post.return_value = response
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.Client", lambda: context)
    engine = AutomationEngine(github, AutomationConfig())

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(child), 0), engine.config)

    assert result.refill_retry_required is True
    assert result.actions == ["Deferred - Parent-Issue reconciliation requires retry"]
    assert child["labels"] == [{"name": "implementation-ready"}]
    github.remove_labels.assert_not_called()
    github.add_comment_to_issue.assert_not_called()


def test_child_trigger_defers_new_parent_and_later_uses_latest_identity(tmp_path: Path, monkeypatch):
    body = "## Requirements\n- REQ-001: Initial."
    created = datetime.now(timezone.utc)
    issues = {1: graph_issue(1, body, ready=True, created_at=created.isoformat()), 2: graph_issue(2, body)}
    github = GraphGitHub(issues, {2: 1}, {1: [2]})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda parent, _children: analyzed.append(parent.body) or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: SpecificationAnalysisResult("READY"))

    engine._validate_submitted_parent_generation_for_child("o/r", 2, dict(issues[2]))
    assert analyzed == []
    assert engine.invalidations.pending_count("o/r") == 1

    issues[1]["body"] = body + "\nLatest."
    monkeypatch.setattr("src.auto_coder.automation_engine.time.time", lambda: (created + timedelta(seconds=61)).timestamp())
    engine._validate_submitted_parent_generation_for_child("o/r", 2, dict(issues[2]))
    assert analyzed == [issues[1]["body"]]


@pytest.mark.parametrize("late_declaration", ["Parent-Issue: #3", "Parent-Issue: #abc"])
def test_final_child_snapshot_is_reconciled_before_analyzers(tmp_path: Path, late_declaration: str):
    body = "## Requirements\n- REQ-001: Preserve the graph."

    class MutatingGraph(GraphGitHub):
        child_reads = 0

        def get_issue_dispatch_snapshot_strict(self, repo, number):
            snapshot = super().get_issue_dispatch_snapshot_strict(repo, number)
            if number == 2:
                self.child_reads += 1
                if self.child_reads >= 2:
                    snapshot["body"] += "\n" + late_declaration
                    self.issues[2]["body"] = snapshot["body"]
            return snapshot

    github = MutatingGraph(
        {1: graph_issue(1, body, ready=True), 2: graph_issue(2, body, state="closed"), 3: graph_issue(3, body)},
        {2: 1},
        {1: [2]},
    )
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("child") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)

    assert result.error is not None
    assert analyzed == []
    assert github.parents == {2: 1}


def test_jules_replacement_reconciles_current_declaration_before_validation(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    child = graph_issue(2, body + "\nParent-Issue: #3", ready=True)
    github = GraphGitHub({2: child, 3: graph_issue(3, body)}, {}, {})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("individual") or SpecificationAnalysisResult("READY"))

    authorized = engine._authorize_stale_jules_dispatch("o/r", 2, dict(child))

    assert authorized is None
    assert github.events == ["linked"]
    assert github.parents == {2: 3}
    assert analyzed == []


def test_jules_standalone_blocked_completion_cannot_mutate_new_parent_set(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    github = GraphGitHub({1: graph_issue(1, body, ready=True), 2: graph_issue(2, body, state="closed")}, {}, {})

    def block_and_add_child(*_args):
        github.parents[2] = 1
        github.children[1] = [2]
        from src.auto_coder.specification_analyzer import SpecificationFinding

        finding = SpecificationFinding("material_ambiguity", ("REQ-001",), "Ambiguous.", "Clarify it.", "Two outcomes.", "Required outcome.")
        return SpecificationAnalysisResult("BLOCKED", (finding,))

    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", block_and_add_child)

    authorized = engine._authorize_stale_jules_dispatch("o/r", 1, dict(github.issues[1]))

    assert authorized is None
    assert github.comments == []
    assert github.removals == []
    assert github.issues[1]["labels"] == [{"name": "implementation-ready"}]
