"""Generation-bound decomposition authorization regressions."""

import asyncio
from unittest.mock import Mock

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult, DecompositionIssue
from auto_coder.entity_invalidation import EntityIdentity
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
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


class MutableHierarchyGitHub:
    def __init__(self):
        self.snapshots = {
            10: {"number": 10, "title": "Parent", "body": "## Requirements\n- REQ-001: Children deliver behavior.", "state": "open", "labels": ["implementation-ready"]},
            11: {"number": 11, "title": "Child", "body": "## Requirements\n- REQ-001: Deliver behavior.", "state": "open", "labels": []},
            12: {"number": 12, "title": "Sibling", "body": "## Requirements\n- REQ-001: Deliver sibling behavior.", "state": "open", "labels": []},
        }
        self.children = [11, 12]
        self.native_parent = 10
        self.removals = []
        self.comments = []
        self.links = []

    def get_issue_dispatch_snapshot_strict(self, _repo, number):
        return dict(self.snapshots[number])

    def get_parent_issue_number_strict(self, _repo, number):
        return self.native_parent if number == 11 else None

    def get_all_sub_issues_strict(self, _repo, _number):
        return list(self.children)

    def get_open_sub_issues(self, _repo, number):
        return list(self.children) if number == 10 else []

    def get_open_sub_issues_strict(self, _repo, number):
        return list(self.children) if number == 10 else []

    def add_sub_issue(self, _repo, parent, child):
        self.links.append((parent, child))
        self.native_parent = parent
        if child not in self.children:
            self.children.append(child)
        return True

    def get_issue_comments_strict(self, _repo, _number):
        return list(self.comments)

    def add_comment_to_issue(self, _repo, _number, body):
        self.comments.append({"body": body})

    def remove_labels(self, _repo, number, _labels, item_type="issue"):
        self.removals.append(number)
        self.snapshots[number]["labels"] = []


def hierarchy_engine(tmp_path, github, decomposition_analyzer, individual_analyzer):
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "set.json", decomposition_analyzer)
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "individual.json", individual_analyzer)
    engine._process_single_candidate_reserved = Mock(return_value=CandidateProcessingResult("issue", 11, "Child", True, ["dispatched"]))
    return engine


def test_stale_decomposition_blocked_does_not_withdraw_edited_set(tmp_path):
    github = MutableHierarchyGitHub()

    def analyze(_parent, _children):
        github.snapshots[12]["title"] = "Edited sibling"
        return DecompositionAnalysisResult("BLOCKED")

    engine = hierarchy_engine(tmp_path, github, analyze, lambda *_args: SpecificationAnalysisResult("READY"))
    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11), engine.config)

    assert result.actions == ["Rejected - blocked decomposition"]
    assert github.removals == []


def test_stale_child_blocked_does_not_withdraw_detached_set(tmp_path):
    github = MutableHierarchyGitHub()

    def analyze_child(_manifest, _body):
        github.children.remove(11)
        return SpecificationAnalysisResult("BLOCKED")

    engine = hierarchy_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("READY"), analyze_child)
    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11), engine.config)

    assert result.actions == ["Rejected - blocked specification"]
    assert github.removals == []


def test_conflicting_parent_declaration_stops_before_semantic_analysis(tmp_path):
    github = MutableHierarchyGitHub()
    github.snapshots[11]["body"] = "Parent-Issue: #99\n\n## Requirements\n- REQ-001: Deliver behavior."
    decomposition = Mock(return_value=DecompositionAnalysisResult("READY"))
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = hierarchy_engine(tmp_path, github, decomposition, individual)

    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11), engine.config)

    assert result.actions == ["Rejected - conflicting parent relationships"]
    decomposition.assert_not_called()
    individual.assert_not_called()


def test_parent_declaration_is_materialized_and_refetched_before_validation(tmp_path):
    github = MutableHierarchyGitHub()
    github.native_parent = None
    github.children = []
    github.snapshots[11]["body"] = "Parent-Issue: #10\n\n## Requirements\n- REQ-001: Deliver behavior."
    engine = hierarchy_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: SpecificationAnalysisResult("READY"),
    )

    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11), engine.config)

    assert github.links == [(10, 11)]
    assert result.actions == ["dispatched"]


def test_invalid_contract_requires_explicit_readiness_resubmission(tmp_path):
    github = MutableHierarchyGitHub()
    github.native_parent = None
    github.children = []
    github.snapshots[11]["labels"] = ["implementation-ready"]
    github.snapshots[11]["body"] = "## Requirements\nREQ-001: First.\nREQ-001: Duplicate."
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = hierarchy_engine(tmp_path, github, lambda *_args: DecompositionAnalysisResult("READY"), individual)
    candidate = Candidate(type="issue", data=dict(github.snapshots[11]), priority=0, issue_number=11)

    rejected = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    github.snapshots[11]["body"] = "## Requirements\nREQ-001: Corrected."
    still_unsubmitted = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)

    assert rejected.actions[0].startswith("Rejected - invalid requirement contract")
    assert github.removals == [11]
    assert still_unsubmitted.actions == ["Skipped - missing implementation-ready label"]
    individual.assert_not_called()


def test_parent_candidate_validates_set_and_selected_child_not_parent(tmp_path):
    github = MutableHierarchyGitHub()
    decomposition = Mock(return_value=DecompositionAnalysisResult("READY"))
    individual = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = hierarchy_engine(tmp_path, github, decomposition, individual)

    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(github.snapshots[10]), priority=0, issue_number=10), engine.config)

    assert result.actions == ["dispatched"]
    decomposition.assert_called_once()
    assert individual.call_args.args[0].issue_number == 11


def test_stale_replacement_requires_parent_readiness_and_sibling_order(tmp_path):
    github = MutableHierarchyGitHub()
    engine = hierarchy_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: SpecificationAnalysisResult("READY"),
    )
    github.snapshots[10]["labels"] = []
    assert engine._authorize_stale_jules_dispatch("owner/repo", 11, dict(github.snapshots[11])) is None

    github.snapshots[10]["labels"] = ["implementation-ready"]
    github.children = [9, 11]
    github.snapshots[9] = {
        "number": 9,
        "title": "Elder",
        "body": "## Requirements\n- REQ-001: Deliver elder behavior.",
        "state": "open",
        "labels": [],
    }
    assert engine._authorize_stale_jules_dispatch("owner/repo", 11, dict(github.snapshots[11])) is None


def test_invalidation_feeds_bounded_validation_worker_when_slots_are_full(tmp_path):
    github = MutableHierarchyGitHub()
    engine = hierarchy_engine(
        tmp_path,
        github,
        lambda *_args: DecompositionAnalysisResult("READY"),
        lambda *_args: SpecificationAnalysisResult("READY"),
    )
    engine.invalidations = engine.invalidations.__class__(tmp_path / "invalidations.sqlite3")
    assert engine.implementation_slots is not None
    # Occupy the sole implementation slot; validation has its own queue.
    assert engine.implementation_slots.reserve(ImplementationOwner("issue", 99))
    engine.invalidations.invalidate(EntityIdentity("owner/repo", "issue", 10), "delivery")
    observed = asyncio.Event()
    engine._create_candidate_from_single = Mock(return_value=Candidate(type="issue", data=dict(github.snapshots[10]), priority=0, issue_number=10))

    def validate_only(*args, **kwargs):
        assert args[-1] is True
        observed.set()
        return CandidateProcessingResult("issue", 10, success=True)

    engine._process_single_candidate_unified = Mock(side_effect=validate_only)

    async def exercise():
        await engine._enqueue_pending_invalidations("owner/repo")
        worker = asyncio.create_task(engine._specification_worker_loop("owner/repo", 0))
        await asyncio.wait_for(observed.wait(), timeout=1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(exercise())
    assert engine._process_single_candidate_unified.call_count == 1
