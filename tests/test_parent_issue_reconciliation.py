from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from src.auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
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

    def get_open_entities_strict(self, _repo):
        return SimpleNamespace(issues=[SimpleNamespace(number=number) for number, issue in self.issues.items() if issue["state"] == "open"])

    def get_open_issues_json(self, _repo):
        return [dict(issue) for issue in self.issues.values() if issue["state"] == "open"]

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

    def close_issue(self, _repo, number, comment=None):
        self.events.append("closed")
        self.issues[number]["state"] = "closed"


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


def test_family_discovery_refreshes_only_related_issues_and_records_scope():
    from src.auto_coder.execution_trace import get_trace_collector

    github = GraphGitHub(
        {100: graph_issue(100, ""), 101: graph_issue(101, "Parent-Issue: #100"), 102: graph_issue(102, ""), 900: graph_issue(900, "Parent-Issue: #899")},
        {102: 100},
        {100: [102]},
    )
    github.get_open_entities_strict = MagicMock(side_effect=AssertionError("Repository-wide cache bypass is forbidden"))
    github.get_issue_dispatch_snapshot_strict = MagicMock(wraps=github.get_issue_dispatch_snapshot_strict)
    engine = AutomationEngine(github, AutomationConfig())
    collector = get_trace_collector()
    with collector.start_execution("o/r", "issue", 100, origin="worker"):
        engine._reconcile_declared_family("o/r", 100)

    assert github.parents == {101: 100, 102: 100}
    assert {call.args[1] for call in github.get_issue_dispatch_snapshot_strict.call_args_list} <= {100, 101, 102}
    assert {101, 102} <= {call.args[1] for call in github.get_issue_dispatch_snapshot_strict.call_args_list}
    github.get_open_entities_strict.assert_not_called()
    events = [event for event in collector.get_snapshot(item_type="issue", item_number=100).events if event.stage_id == "issue.family-discovery"]
    assert events[-1].outcome == "completed"
    assert events[-1].facts == {"discovery_source": "cached-open-issue-list", "live_scope": "related-declarations-and-native-children", "declared_issue_numbers": [101], "authorizes_execution": False}


@pytest.mark.parametrize("fresh_body,state", [("Parent-Issue: #200", "open"), ("", "open"), ("Parent-Issue: #100", "closed")])
def test_stale_cached_declaration_cannot_materialize_child(fresh_body, state):
    github = GraphGitHub({100: graph_issue(100, ""), 101: graph_issue(101, fresh_body, state=state)}, {}, {})
    github.get_open_issues_json = MagicMock(return_value=[graph_issue(101, "Parent-Issue: #100")])
    engine = AutomationEngine(github, AutomationConfig())

    engine._reconcile_declared_family("o/r", 100)

    assert github.parents == {}
    assert github.events == []


def test_native_member_missing_from_cache_still_rejects_conflicting_declaration():
    from src.auto_coder.parent_issue_reconciliation import ParentSpecificationError

    github = GraphGitHub({100: graph_issue(100, ""), 101: graph_issue(101, "Parent-Issue: #200")}, {101: 100}, {100: [101]})
    github.get_open_issues_json = MagicMock(return_value=[])
    engine = AutomationEngine(github, AutomationConfig())

    with pytest.raises(ParentSpecificationError, match="conflicts with native parent #100"):
        engine._reconcile_declared_family("o/r", 100)

    assert github.events == []


def test_normal_relationship_preflight_uses_cached_discovery_without_refreshing_unrelated_issues():
    github = GraphGitHub(
        {100: graph_issue(100, ""), 101: graph_issue(101, "Parent-Issue: #100"), 900: graph_issue(900, "Parent-Issue: #899")},
        {},
        {},
    )
    github.get_open_entities_strict = MagicMock(side_effect=AssertionError("Repository-wide cache bypass is forbidden"))
    github.get_issue_dispatch_snapshot_strict = MagicMock(wraps=github.get_issue_dispatch_snapshot_strict)
    engine = AutomationEngine(github, AutomationConfig())

    result = engine._preflight_explicit_issue_relationships("o/r", 101)

    assert result == github.issues[101]
    assert github.parents == {101: 100}
    assert {call.args[1] for call in github.get_issue_dispatch_snapshot_strict.call_args_list} == {100, 101}
    github.get_open_entities_strict.assert_not_called()


def test_family_discovery_reuses_valid_production_list_cache():
    github = GraphGitHub({100: graph_issue(100, ""), 101: graph_issue(101, "Parent-Issue: #100")}, {101: 100}, {100: [101]})
    github._open_issues_cache_lock = RLock()
    github._open_issues_cache_repo = "o/r"
    github._open_issues_cache_time = datetime.now() - timedelta(minutes=59)
    github._open_issues_cache = [dict(issue) for issue in github.issues.values()]
    github.get_open_issues_json = GitHubClient.get_open_issues_json.__get__(github)
    github.get_issue_dispatch_snapshot_strict = MagicMock(wraps=github.get_issue_dispatch_snapshot_strict)
    engine = AutomationEngine(github, AutomationConfig())

    with patch("src.auto_coder.util.gh_cache.get_ghapi_client", side_effect=AssertionError("The valid list cache must not be refreshed")):
        engine._reconcile_declared_family("o/r", 100)

    assert github.parents == {101: 100}
    assert {call.args[1] for call in github.get_issue_dispatch_snapshot_strict.call_args_list} == {101}


def test_only_parent_reconciles_every_declared_child_before_unified_processing():
    """The supported explicit origin cannot pass a partial child set downstream."""
    body = "## Requirements\n- REQ-001: Preserve the complete set."
    github = GraphGitHub(
        {
            100: graph_issue(100, body, ready=True),
            101: graph_issue(101, body + "\nParent-Issue: #100"),
            102: graph_issue(102, body + "\nParent-Issue: #100"),
            103: graph_issue(103, body + "\nParent-Issue: #100"),
            900: graph_issue(900, "Unrelated Issue"),
        },
        {101: 100},
        {100: [101]},
    )
    github.get_open_issues_json = MagicMock(return_value=[dict(github.issues[100])])
    github.get_open_entities_strict = MagicMock(wraps=github.get_open_entities_strict)
    github.get_issue_dispatch_snapshot_strict = MagicMock(wraps=github.get_issue_dispatch_snapshot_strict)
    engine = AutomationEngine(github, AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(return_value=Candidate("issue", dict(github.issues[100]), 0))
    observed = []

    def process(*_args, **_kwargs):
        observed.append(tuple(sorted(github.children[100])))
        from src.auto_coder.automation_config import CandidateProcessingResult

        return CandidateProcessingResult("issue", 100, "Issue 100", True, ["target only"])

    engine._process_single_candidate_unified = MagicMock(side_effect=process)
    engine._validate_submitted_parent_generation_for_child = MagicMock()

    with patch("auto_coder.llm_backend_config.is_jules_mode_enabled", return_value=False):
        result = engine.process_single("o/r", "issue", 100, explicit_only=True)

    assert observed == [(101, 102, 103)]
    github.get_open_entities_strict.assert_called_once_with("o/r")
    assert {call.args[1] for call in github.get_issue_dispatch_snapshot_strict.call_args_list} == {100, 101, 102, 103, 900}
    assert github.events == ["linked", "linked"]
    assert result["issues_processed"][0]["actions_taken"] == ["target only"]
    engine._validate_submitted_parent_generation_for_child.assert_called_once_with("o/r", 100, engine._create_candidate_from_single.return_value.data, target_only=True)


def test_only_child_reconciles_unmaterialized_elder_without_processing_it():
    body = "## Requirements\n- REQ-001: Preserve sibling order."
    github = GraphGitHub(
        {
            100: graph_issue(100, body, ready=True),
            102: graph_issue(102, body + "\nParent-Issue: #100"),
            103: graph_issue(103, body + "\nParent-Issue: #100", ready=True),
        },
        {103: 100},
        {100: [103]},
    )
    engine = AutomationEngine(github, AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(return_value=Candidate("issue", dict(github.issues[103]), 0))

    def process(_repo, candidate, *_args, **_kwargs):
        assert candidate.data["number"] == 103
        assert sorted(github.children[100]) == [102, 103]
        from src.auto_coder.automation_config import CandidateProcessingResult

        return CandidateProcessingResult("issue", 103, "Issue 103", True, ["deferred behind #102"])

    engine._process_single_candidate_unified = MagicMock(side_effect=process)
    engine._validate_submitted_parent_generation_for_child = MagicMock()

    with patch("auto_coder.llm_backend_config.is_jules_mode_enabled", return_value=False):
        engine.process_single("o/r", "issue", 103, explicit_only=True)

    assert github.events == ["linked"]
    assert engine._process_single_candidate_unified.call_count == 1
    engine._validate_submitted_parent_generation_for_child.assert_called_once_with("o/r", 103, engine._create_candidate_from_single.return_value.data, target_only=True)


def test_only_relationship_contradiction_fails_closed_before_dispatch():
    body = "## Requirements\n- REQ-001: Preserve the hierarchy."
    github = GraphGitHub(
        {
            100: graph_issue(100, body, ready=True),
            102: graph_issue(102, body + "\nParent-Issue: #100"),
            200: graph_issue(200, body),
        },
        {102: 200},
        {200: [102]},
    )
    engine = AutomationEngine(github, AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(return_value=Candidate("issue", dict(github.issues[100]), 0))
    engine._process_single_candidate_unified = MagicMock()

    with patch("auto_coder.llm_backend_config.is_jules_mode_enabled", return_value=False):
        result = engine.process_single("o/r", "issue", 100, explicit_only=True)

    assert result["errors"] == ["Blocked relationship reconciliation for Issue #100: Parent-Issue declaration #100 conflicts with native parent #200"]
    engine._process_single_candidate_unified.assert_not_called()


def test_only_materialization_failure_is_retryable_and_starts_no_target_work():
    body = "## Requirements\n- REQ-001: Preserve the hierarchy."
    github = GraphGitHub(
        {
            100: graph_issue(100, body, ready=True),
            102: graph_issue(102, body + "\nParent-Issue: #100"),
        },
        {},
        {},
    )

    def fail_link(*_args):
        raise RuntimeError("temporary GitHub failure")

    github.add_sub_issue_strict = fail_link
    engine = AutomationEngine(github, AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(return_value=Candidate("issue", dict(github.issues[100]), 0))
    engine._process_single_candidate_unified = MagicMock()

    with patch("auto_coder.llm_backend_config.is_jules_mode_enabled", return_value=False):
        result = engine.process_single("o/r", "issue", 100, explicit_only=True)

    assert result["errors"] == ["Retryable relationship reconciliation failure for Issue #100: " "cannot materialize Parent-Issue relationship: temporary GitHub failure"]
    engine._process_single_candidate_unified.assert_not_called()


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
    initial_child = graph_issue(2, body + "\nParent-Issue: #1")
    parent = graph_issue(1, body, ready=True)
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


def test_ordinary_child_validation_discovers_all_declared_siblings(tmp_path: Path):
    """A first child invalidation cannot admit a repeatedly stable native subset."""
    body = "## Requirements\n- REQ-001: Keep the graph authoritative."
    github = GraphGitHub(
        {
            1: graph_issue(1, body, ready=True),
            2: graph_issue(2, body + "\nParent-Issue: #1"),
            3: graph_issue(3, body + "\nparent_issue: 1"),
            4: graph_issue(4, body + "\nPARENT ISSUE: #1"),
            5: graph_issue(5, body, state="closed"),
            99: graph_issue(99, "Parent-Issue: invalid"),
        },
        {2: 1, 5: 1},
        {1: [2, 5]},
    )
    decompositions: list[tuple[int, ...]] = []
    individuals: list[int] = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle(
        "o/r",
        "provider/model",
        tmp_path / "sets.json",
        lambda _parent, children: decompositions.append(tuple(sorted(child.manifest.issue_number for child in children))) or DecompositionAnalysisResult("READY"),
    )
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle(
        "o/r",
        "provider/model",
        tmp_path / "issues.json",
        lambda manifest, *_args: individuals.append(manifest.issue_number) or SpecificationAnalysisResult("READY"),
    )

    engine._validate_submitted_parent_generation_for_child("o/r", 2, github.issues[2])

    assert sorted(github.children[1]) == [2, 3, 4, 5]
    assert decompositions == [(2, 3, 4, 5)]
    assert sorted(individuals) == [2, 3, 4, 5]


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


def test_closed_issue_cannot_be_materialized_as_new_parent():
    body = "## Requirements\n- REQ-001: Keep the graph shallow."
    github = GraphGitHub(
        {
            1: graph_issue(1, body, state="closed"),
            2: graph_issue(2, body + "\nParent-Issue: #1", ready=True),
        },
        {},
        {},
    )
    engine = AutomationEngine(github, AutomationConfig())

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[2]), 0), engine.config)

    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    assert result.error == "Parent-Issue reconciliation blocked processing: declared parent #1 is closed"
    assert github.parents == {}
    assert github.events == []


def test_preexisting_nested_parent_fails_before_validation_or_implementation(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Keep the graph shallow."
    github = GraphGitHub(
        {1: graph_issue(1, body), 2: graph_issue(2, body, ready=True), 3: graph_issue(3, body)},
        {2: 1, 3: 2},
        {1: [2], 2: [3]},
    )
    analyzed: list[str] = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[2]), 0), engine.config, force=True, explicit_only=True)

    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    assert "both a child and a parent" in (result.error or "")
    assert analyzed == []
    assert github.events == []


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
    assert second.actions == ["Completed - closed container parent after all direct children completed"]


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
    assert second.actions == ["Rejected - blocked child specification"]


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


@pytest.mark.parametrize("replacement", ["Parent-Issue: #3", "Parent-Issue: #abc"])
def test_reconciliation_rechecks_the_snapshot_it_returns(tmp_path: Path, replacement: str):
    body = "## Requirements\n- REQ-001: Preserve the graph."

    class ReconciliationRaceGraph(GraphGitHub):
        child_reads = 0

        def get_issue_dispatch_snapshot_strict(self, repo, number):
            snapshot = super().get_issue_dispatch_snapshot_strict(repo, number)
            if number == 2:
                self.child_reads += 1
                if self.child_reads == 2:
                    snapshot["body"] += "\nParent-Issue: #1"
                    self.issues[2]["body"] = snapshot["body"]
                elif self.child_reads >= 3:
                    snapshot["body"] = body + "\n" + replacement
                    self.issues[2]["body"] = snapshot["body"]
            return snapshot

    github = ReconciliationRaceGraph(
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


def test_final_parent_snapshot_is_reconciled_before_decomposition(tmp_path: Path):
    body = "## Requirements\n- REQ-001: Preserve the graph."

    class ParentRaceGraph(GraphGitHub):
        parent_reads = 0

        def get_issue_dispatch_snapshot_strict(self, repo, number):
            snapshot = super().get_issue_dispatch_snapshot_strict(repo, number)
            if number == 1:
                self.parent_reads += 1
                if self.parent_reads >= 4:
                    snapshot["body"] = body + "\nParent-Issue: #abc"
                    self.issues[1]["body"] = snapshot["body"]
            return snapshot

    github = ParentRaceGraph({1: graph_issue(1, body, ready=True), 2: graph_issue(2, body, state="closed")}, {2: 1}, {1: [2]})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle("o/r", "provider/model", tmp_path / "sets.json", lambda *_args: analyzed.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("child") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)

    assert result.error is not None
    assert analyzed == []


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


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("declaration", ["Parent-Issue: #3", "Parent-Issue: #abc"])
def test_late_individual_validation_snapshot_is_reconciled(tmp_path: Path, owned: bool, declaration: str):
    body = "## Requirements\n- REQ-001: Preserve the graph."

    class LateDeclarationGraph(GraphGitHub):
        issue_reads = 0

        def get_issue_dispatch_snapshot_strict(self, repo, number):
            snapshot = super().get_issue_dispatch_snapshot_strict(repo, number)
            if number == 1:
                self.issue_reads += 1
                threshold = 2 if owned else 3
                if self.issue_reads >= threshold:
                    snapshot["body"] = declaration + "\n" + body
                    self.issues[1]["body"] = snapshot["body"]
            return snapshot

    github = LateDeclarationGraph({1: graph_issue(1, body, ready=True), 3: graph_issue(3, body)}, {}, {})
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("o/r", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    owner = ImplementationOwner("issue", 1)
    if owned:
        execution_id = slots.start_execution(owner)
        assert execution_id is not None
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", lambda *_args: analyzed.append("individual") or SpecificationAnalysisResult("READY"))

    result = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)

    assert analyzed == []
    if declaration == "Parent-Issue: #3":
        assert github.events == ["linked"]
        assert github.parents == {1: 3}
    else:
        assert github.events == []
        assert "blocked" in (result.error or "").lower()
    if owned:
        assert slots.active_execution_ids(owner)


def test_late_marker_for_new_ready_parent_defers_until_latest_generation(tmp_path: Path, monkeypatch):
    body = "## Requirements\n- REQ-001: Preserve the graph."
    created = datetime.now(timezone.utc)

    class LateNewParentGraph(GraphGitHub):
        child_reads = 0

        def get_issue_dispatch_snapshot_strict(self, repo, number):
            snapshot = super().get_issue_dispatch_snapshot_strict(repo, number)
            if number == 1:
                self.child_reads += 1
                if self.child_reads >= 3:
                    snapshot["body"] = "Parent-Issue: #3\n" + body
                    self.issues[1]["body"] = snapshot["body"]
            return snapshot

    github = LateNewParentGraph(
        {
            1: graph_issue(1, body, ready=True),
            3: graph_issue(3, body, ready=True, created_at=created.isoformat()),
        },
        {},
        {},
    )
    analyzed = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle(
        "o/r",
        "provider/model",
        tmp_path / "sets.json",
        lambda parent, _children: analyzed.append(("set", parent.body)) or DecompositionAnalysisResult("READY"),
    )
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle(
        "o/r",
        "provider/model",
        tmp_path / "issues.json",
        lambda manifest, _body: analyzed.append(("individual", manifest.issue_number)) or SpecificationAnalysisResult("READY"),
    )

    first = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)
    assert first.actions == ["Deferred - readiness submission is in its initial stabilization window"]
    assert github.events == ["linked"]
    assert analyzed == []
    assert engine.invalidations.pending_count("o/r") == 1

    github.issues[3]["body"] = body + "\nLatest."
    github.issues[1]["state"] = "closed"
    monkeypatch.setattr("src.auto_coder.automation_engine.time.time", lambda: (created + timedelta(seconds=61)).timestamp())
    second = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[3]), 0), engine.config)

    assert ("set", github.issues[3]["body"]) in analyzed
    assert ("individual", 1) in analyzed
    assert second.actions == ["Completed - closed container parent after all direct children completed"]


def test_ready_completion_defers_parent_discovered_during_analysis(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    body = "## Requirements\n- REQ-001: Preserve the graph."
    created = datetime.now(timezone.utc)
    github = GraphGitHub(
        {
            1: graph_issue(1, body, ready=True),
            3: graph_issue(3, body, ready=True, created_at=created.isoformat()),
        },
        {},
        {},
    )
    analyzed = []

    def analyze_individual(manifest, _body):
        analyzed.append(("individual", manifest.issue_number))
        github.parents[1] = 3
        github.children[3] = [1]
        return SpecificationAnalysisResult("READY")

    engine = AutomationEngine(github, AutomationConfig())
    engine._specification_validators["o/r"] = SpecificationValidationLifecycle("o/r", "provider/model", tmp_path / "issues.json", analyze_individual)
    engine._decomposition_validators["o/r"] = DecompositionValidationLifecycle(
        "o/r",
        "provider/model",
        tmp_path / "sets.json",
        lambda parent, _children: analyzed.append(("set", parent.body)) or DecompositionAnalysisResult("READY"),
    )

    first = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[1]), 0), engine.config)
    assert first.actions == ["Skipped - validated Issue generation is stale or no longer submitted"]
    assert analyzed == [("individual", 1)]
    assert engine.invalidations.pending_count("o/r") == 1

    github.issues[3]["body"] = body + "\nLatest."
    github.issues[1]["state"] = "closed"
    monkeypatch.setattr("src.auto_coder.automation_engine.time.time", lambda: (created + timedelta(seconds=61)).timestamp())
    second = engine._process_single_candidate_unified("o/r", Candidate("issue", dict(github.issues[3]), 0), engine.config)

    # Other tests may have already populated the process-wide validation caches,
    # which can change whether child revalidation runs before or after set review.
    # The lifecycle guarantee here is that both reviews occur, not their ordering.
    assert analyzed.count(("individual", 1)) == 2
    assert analyzed.count(("set", github.issues[3]["body"])) == 1
    assert second.actions == ["Completed - closed container parent after all direct children completed"]


@pytest.mark.parametrize("body", ["Blocked-By:", "", "blocked-by:   "])
def test_standalone_dependency_reconciliation_accepts_empty_declaration(body):
    from src.auto_coder.sibling_dependencies import DependencySatisfaction

    issue = graph_issue(1998, body, ready=True)
    github = GraphGitHub({1998: issue}, {}, {})
    engine = AutomationEngine(github, AutomationConfig())
    assert engine._reconcile_sibling_dependencies("o/r", 1998, issue) is DependencySatisfaction.SATISFIED
    assert github.events == []
    assert github.comments == []
    assert github.removals == []


def test_empty_dependency_declaration_does_not_bypass_native_child_validation():
    from src.auto_coder.sibling_dependencies import DependencySatisfaction

    issue = graph_issue(1998, "Blocked-By:", ready=True)
    github = GraphGitHub({1998: issue, 100: graph_issue(100, "## Objective\nTrack work.", ready=True)}, {1998: 100}, {100: [1998]})
    engine = AutomationEngine(github, AutomationConfig())
    assert engine._reconcile_sibling_dependencies("o/r", 1998, issue) is DependencySatisfaction.INVALID
    assert [number for number, _, _ in github.removals] == [1998, 100]
    assert len(github.comments) == 1


def test_empty_dependency_declaration_requires_available_parent_evidence():
    issue = graph_issue(1998, "Blocked-By:", ready=True)
    github = GraphGitHub({1998: issue}, {}, {})
    github.get_parent_issue_details_strict = MagicMock(side_effect=RuntimeError("read unavailable"))
    engine = AutomationEngine(github, AutomationConfig())
    with pytest.raises(RuntimeError, match="read unavailable"):
        engine._reconcile_sibling_dependencies("o/r", 1998, issue)
    assert github.removals == []
