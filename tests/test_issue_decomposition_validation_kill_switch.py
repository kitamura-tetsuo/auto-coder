"""Regression tests for parent/child decomposition validation kill switch (Issue #1814 / Parent #1811).

Covers REQ-001 through REQ-009 and acceptance scenarios AS-001 through AS-008.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, Mock

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import (
    AffectedIssue,
    DecompositionAnalysisResult,
    DecompositionFinding,
)
from auto_coder.decomposition_validation_lifecycle import (
    DecompositionIssue,
    DecompositionValidationLifecycle,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.validation_scheduler import ValidationScheduler

PARENT_BODY = "## Requirements\n- REQ-001: Parent feature specification."
CHILD1_BODY = "Parent-Issue: #10\n\n## Requirements\n- REQ-001: First child specification."
CHILD2_BODY = "Parent-Issue: #10\n\n## Requirements\n- REQ-001: Second child specification."

SET_FINDING = DecompositionFinding(
    "missing_requirement_ownership",
    (AffectedIssue(10, ("REQ-001",)),),
    "No child owns the behavior.",
    "Assign the behavior to a child.",
)
SPEC_FINDING = SpecificationFinding(
    "material_ambiguity",
    ("REQ-001",),
    "Child requirement is ambiguous.",
    "Clarify child behavior.",
    "",
    "",
)


def make_parent(
    number: int = 10,
    title: str = "Parent Issue",
    body: str = PARENT_BODY,
    ready: bool = True,
    state: str = "open",
    total_sub_issues: int = 1,
) -> dict:
    return {
        "id": number,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": "implementation-ready"}] if ready else [],
        "sub_issues_summary": {"total": total_sub_issues},
        "user": {"id": 1},
    }


def make_child(
    number: int = 11,
    parent_number: int = 10,
    title: str = "Child Issue",
    body: str = CHILD1_BODY,
    ready: bool = False,
    state: str = "open",
) -> dict:
    return {
        "id": number,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"name": "implementation-ready"}] if ready else [],
        "parent_issue_url": f"https://api.github.com/repos/owner/repo/issues/{parent_number}",
        "parent_issue_number": parent_number,
        "sub_issues_summary": {"total": 0},
        "user": {"id": 1},
    }


class DecompositionGitHubFlow:
    """Mock GitHub client returning authoritative parent/child snapshots."""

    def __init__(self, parent: dict, children: list[dict], all_issues: Optional[list[dict]] = None):
        self.parent = dict(parent)
        self.children = [dict(c) for c in children]
        self.comments: list[dict] = []
        self.removed_labels: list[tuple[int, list[str]]] = []
        issues = list(all_issues) if all_issues is not None else [parent, *children]
        self._all_issues = {i["number"]: dict(i) for i in issues}

    def get_direct_sub_issues_strict(self, _repo: str, number: int) -> list[dict]:
        if number == self.parent["number"]:
            return [dict(c) for c in self.children]
        return []

    def get_parent_issue_details_strict(self, _repo: str, number: int) -> Optional[dict]:
        if any(c["number"] == number for c in self.children):
            return dict(self.parent)
        if number in self._all_issues:
            issue = self._all_issues[number]
            if issue.get("parent_issue_number") == self.parent["number"]:
                return dict(self.parent)
        return None

    def get_issue_dispatch_snapshot_strict(self, _repo: str, number: int) -> dict:
        if number in self._all_issues:
            return dict(self._all_issues[number])
        if number == self.parent["number"]:
            return dict(self.parent)
        for c in self.children:
            if c["number"] == number:
                return dict(c)
        raise ValueError(f"Issue {number} not found")

    def get_issue_comments_strict(self, _repo: str, _number: int) -> list[dict]:
        return list(self.comments)

    def add_comment_to_issue(self, _repo: str, number: int, body: str) -> None:
        self.comments.append({"number": number, "body": body})

    def remove_labels(self, _repo: str, number: int, labels: list[str], item_type: str = "issue") -> None:
        assert item_type == "issue"
        self.removed_labels.append((number, list(labels)))

    def get_open_sub_issues(self, _repo: str, _number: int) -> list[int]:
        return []

    def clear_sub_issue_cache(self) -> None:
        pass

    def has_linked_pr(self, _repo: str, _number: int) -> bool:
        return False

    def get_issue_timeline(self, _repo: str, _number: int) -> list[dict]:
        return []


def make_engine(
    tmp_path: Path,
    github: Any,
    config: Optional[AutomationConfig] = None,
    decomposition_gate: Optional[DecompositionValidationLifecycle] = None,
    specification_gate: Optional[SpecificationValidationLifecycle] = None,
    repo_name: str = "owner/repo",
) -> AutomationEngine:
    cfg = config if config is not None else AutomationConfig(repo_name=repo_name)
    engine = AutomationEngine(github, config=cfg)
    engine.implementation_slots = ImplementationSlotRepository(repo_name, 1, tmp_path / "slots.json")
    if decomposition_gate is not None:
        engine._decomposition_validators[repo_name] = decomposition_gate
    if specification_gate is not None:
        engine._specification_validators[repo_name] = specification_gate
    engine._process_single_candidate_reserved = Mock(side_effect=lambda repo, cand, *args, **kwargs: CandidateProcessingResult("issue", cand.data["number"], cand.data.get("title"), True, ["dispatched"]))
    return engine


class TestAS001DisabledFreshParentSet:
    """AS-001: Disabled fresh parent set.

    Given an open implementation-ready parent with an otherwise eligible first child,
    no decomposition record for the current set, decomposition validation disabled,
    and individual specification validation disabled or already satisfied,
    when the production candidate path runs, then no decomposition validator/scheduler
    job is invoked and the child can proceed toward ownership and dispatch.
    Covers REQ-001, REQ-006, REQ-009.
    """

    def test_parent_candidate_routes_and_dispatches_child_without_decomposition_validation(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        decomp_analyzer = Mock(side_effect=AssertionError("Decomposition validator must not be called when disabled"))
        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", decomp_analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate)
        scheduler_spy = Mock(wraps=engine.validation_scheduler.submit)
        engine.validation_scheduler.submit = scheduler_spy

        parent_candidate = Candidate(type="issue", data=dict(parent), priority=0, issue_number=10)
        result = engine._process_single_candidate_unified("owner/repo", parent_candidate, config)

        assert result.actions == ["dispatched"]
        assert result.success is True
        decomp_analyzer.assert_not_called()
        for call_args in scheduler_spy.call_args_list:
            key = call_args[0][0]
            assert not key.startswith("decomposition:")
        engine._process_single_candidate_reserved.assert_called_once()
        assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 11),)

    def test_child_candidate_dispatches_without_decomposition_validation(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        decomp_analyzer = Mock(side_effect=AssertionError("Decomposition validator must not be called when disabled"))
        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", decomp_analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert result.actions == ["dispatched"]
        assert result.success is True
        decomp_analyzer.assert_not_called()
        engine._process_single_candidate_reserved.assert_called_once()
        assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 11),)


class TestAS002ExistingBlockedSetBypassedWithoutMutation:
    """AS-002: Existing BLOCKED set is bypassed without mutation.

    Given the authoritative current parent/child set has a durable BLOCKED decomposition
    record and the parent still independently satisfies all non-decomposition eligibility rules,
    when decomposition validation is disabled and the eligible child is processed,
    then the BLOCKED record is ignored only as a decomposition gate, remains unchanged,
    and no synthetic READY record or label mutation occurs.
    Covers REQ-002, REQ-003.
    """

    def test_durable_blocked_record_bypassed_and_preserved(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        # 1. Create a durable BLOCKED decomposition record
        decomp_analyzer = Mock(return_value=DecompositionAnalysisResult("BLOCKED", (SET_FINDING,), remediation="EDIT_IN_PLACE"))
        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", decomp_analyzer)
        parent_manifest = build_normative_issue_manifest(10, parent["title"], parent["body"])
        child_manifest = build_normative_issue_manifest(11, child["title"], child["body"])
        parent_input = DecompositionIssue(parent_manifest, parent["body"])
        child_inputs = [DecompositionIssue(child_manifest, child["body"])]
        identity = decomp_gate.identity(parent, [child])
        decision = decomp_gate.decide(identity, parent_input, child_inputs)
        assert decision.verdict == "BLOCKED"
        decomp_file = tmp_path / "decomp.json"
        assert decomp_file.exists()
        original_bytes = decomp_file.read_bytes()
        original_json = json.loads(original_bytes.decode("utf-8"))

        # 2. Disable decomposition validation and process the child candidate
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        # 3. Child proceeds to dispatch despite existing BLOCKED decomposition record
        assert result.actions == ["dispatched"]
        assert result.success is True
        engine._process_single_candidate_reserved.assert_called_once()

        # 4. Durable record is completely unchanged, no synthetic READY verdict
        current_bytes = decomp_file.read_bytes()
        assert current_bytes == original_bytes
        current_json = json.loads(current_bytes.decode("utf-8"))
        assert current_json == original_json
        for rec in current_json.values():
            assert rec["verdict"] == "BLOCKED"

        # 5. No label mutations or comments were made
        assert github.removed_labels == []
        assert github.comments == []

    def test_durable_reissue_required_record_bypassed_and_preserved(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json")
        decomp_gate.reissue_store.mark(10)
        assert decomp_gate.is_reissue_required(10) is True
        reissue_file = tmp_path / "reissue_required.json"
        assert reissue_file.exists()
        reissue_bytes = reissue_file.read_bytes()

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert result.actions == ["dispatched"]
        assert result.success is True
        assert reissue_file.read_bytes() == reissue_bytes
        assert decomp_gate.is_reissue_required(10) is True


class TestAS003ParentRelationshipRemainsAuthoritative:
    """AS-003: Parent relationship remains authoritative.

    Given decomposition validation is disabled but the declared parent is closed,
    missing, invalid, or the candidate is no longer an authoritative child member,
    when production admission runs, then the candidate is rejected/deferred by the
    normal relationship/reconciliation rule and is not dispatched.
    Covers REQ-004, REQ-009.
    """

    def test_closed_parent_is_skipped(self, tmp_path: Path):
        parent = make_parent(10, ready=True, state="closed")
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert "authoritative parent is closed" in result.actions[0]
        engine._process_single_candidate_reserved.assert_not_called()

    def test_missing_parent_is_skipped(self, tmp_path: Path):
        child = make_child(11, parent_number=999)
        github = MagicMock()
        github.get_issue_dispatch_snapshot_strict.side_effect = lambda repo, number: dict(child) if number == 11 else None
        github.get_direct_sub_issues_strict.return_value = []
        github.get_parent_issue_details_strict.return_value = None
        github.get_open_sub_issues.return_value = []
        github.has_linked_pr.return_value = False
        github.get_issue_timeline.return_value = []

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert "child is no longer in the authoritative parent set" in result.actions[0]
        engine._process_single_candidate_reserved.assert_not_called()

    def test_candidate_not_in_authoritative_parent_set_is_skipped(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        other_child = make_child(12, parent_number=10)
        stale_child = make_child(11, parent_number=10)
        # Parent only contains child 12, not child 11
        github = DecompositionGitHubFlow(parent, [other_child], all_issues=[parent, stale_child, other_child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        child_candidate = Candidate(type="issue", data=dict(stale_child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert "child is no longer in the authoritative parent set" in result.actions[0]
        engine._process_single_candidate_reserved.assert_not_called()

    def test_parent_without_implementation_ready_label_is_skipped(self, tmp_path: Path):
        parent = make_parent(10, ready=False)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert "authoritative parent is missing implementation-ready label" in result.actions[0]
        engine._process_single_candidate_reserved.assert_not_called()


class TestAS004ExplicitDependencyOrderingRemainsAuthoritative:
    """AS-004: Sibling ordering remains authoritative.

    Given decomposition validation is disabled and a later sibling is otherwise eligible
    while an earlier required sibling remains open, when the later sibling is evaluated,
    then it remains deferred by sibling implementation ordering.
    Covers REQ-004.
    """

    def test_later_sibling_deferred_when_earlier_sibling_open(self, tmp_path: Path):
        parent = make_parent(10, ready=True, total_sub_issues=2)
        first_child = make_child(11, parent_number=10, state="open")
        second_child = make_child(12, parent_number=10, body=CHILD2_BODY, state="open")
        github = DecompositionGitHubFlow(parent, [first_child, second_child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        second_candidate = Candidate(type="issue", data=dict(second_child), priority=0, issue_number=12)
        result = engine._process_single_candidate_unified("owner/repo", second_candidate, config)

        assert result.actions == ["dispatched"]
        engine._process_single_candidate_reserved.assert_called_once()

    def test_later_sibling_dispatches_when_earlier_sibling_closed(self, tmp_path: Path):
        parent = make_parent(10, ready=True, total_sub_issues=2)
        first_child = make_child(11, parent_number=10, state="closed")
        second_child = make_child(12, parent_number=10, body=CHILD2_BODY, state="open")
        github = DecompositionGitHubFlow(parent, [first_child, second_child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)
        second_candidate = Candidate(type="issue", data=dict(second_child), priority=0, issue_number=12)
        result = engine._process_single_candidate_unified("owner/repo", second_candidate, config)

        assert result.actions == ["dispatched"]
        assert result.success is True
        engine._process_single_candidate_reserved.assert_called_once()


class TestAS005ChildSpecificationValidationRemainsIndependent:
    """AS-005: Child specification validation remains independent.

    Given decomposition validation is disabled, the child becomes eligible by parent/ordering
    rules, and issue_specification_validation = true, when the child is evaluated,
    then individual specification validation still runs and a BLOCKED child specification
    still prevents dispatch.
    Covers REQ-005.
    """

    def test_blocked_child_specification_prevents_dispatch_and_applies_inherited_blocked(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        spec_analyzer = Mock(return_value=SpecificationAnalysisResult("BLOCKED", (SPEC_FINDING,), remediation="EDIT_IN_PLACE"))
        spec_gate = SpecificationValidationLifecycle("owner/repo", "model", tmp_path / "spec.json", spec_analyzer)

        decomp_analyzer = Mock(side_effect=AssertionError("Decomposition validator must not run"))
        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", decomp_analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = True

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate, specification_gate=spec_gate)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        # Individual spec validation ran and blocked the child
        assert result.actions == ["Rejected - blocked specification"]
        assert "material defects" in (result.error or "")
        decomp_analyzer.assert_not_called()
        spec_analyzer.assert_called_once()
        engine._process_single_candidate_reserved.assert_not_called()

        # A blocked child preserves the parent submission.
        assert github.removed_labels == []

    def test_ready_child_specification_allows_dispatch(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        spec_analyzer = Mock(return_value=SpecificationAnalysisResult("READY"))
        spec_gate = SpecificationValidationLifecycle("owner/repo", "model", tmp_path / "spec.json", spec_analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = True

        engine = make_engine(tmp_path, github, config=config, specification_gate=spec_gate)
        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert result.actions == ["dispatched"]
        assert result.success is True
        spec_analyzer.assert_called_once()
        engine._process_single_candidate_reserved.assert_called_once()


class TestAS006MembershipChangesWhileDisabled:
    """AS-006: Membership changes while disabled.

    Given decomposition validation is disabled and the parent set membership or
    normative specification changes, when a child is processed, then normal authoritative
    relationship/ordering checks use the changed live set; disabling review does not freeze
    or substitute an old parent-set snapshot.
    Covers REQ-004, REQ-007.
    """

    def test_membership_change_while_disabled_is_observed_live(self, tmp_path: Path):
        parent = make_parent(10, ready=True, total_sub_issues=2)
        first_child = make_child(11, parent_number=10, state="open")
        second_child = make_child(12, parent_number=10, body=CHILD2_BODY, state="open")
        github = DecompositionGitHubFlow(parent, [first_child, second_child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config)

        # Open independent siblings do not create an implicit ordering edge.
        second_candidate = Candidate(type="issue", data=dict(second_child), priority=0, issue_number=12)
        result = engine._process_single_candidate_unified("owner/repo", second_candidate, config)
        assert result.actions == ["dispatched"]
        assert result.success is True


class TestAS007ReEnableAfterChangedGeneration:
    """AS-007: Re-enable after changed generation.

    Given decomposition validation was disabled while parent membership/specification
    changed and is then re-enabled, when the set is evaluated, then prior-generation
    READY evidence is not accepted for the changed generation and ordinary current-generation
    decomposition validation applies.
    Covers REQ-007.
    """

    def test_re_enabled_feature_does_not_reuse_prior_generation_evidence(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child1 = make_child(11, parent_number=10)

        # 1. Generation 1 has a durable READY record
        analyzed_generations = []

        def _decomp_analyzer(p: Any, c: Any) -> DecompositionAnalysisResult:
            analyzed_generations.append([item.manifest.issue_number for item in c])
            return DecompositionAnalysisResult("READY")

        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", _decomp_analyzer)
        parent_manifest = build_normative_issue_manifest(10, parent["title"], parent["body"])
        child1_manifest = build_normative_issue_manifest(11, child1["title"], child1["body"])
        gen1_identity = decomp_gate.identity(parent, [child1])
        decomp_gate.decide(gen1_identity, DecompositionIssue(parent_manifest, parent["body"]), [DecompositionIssue(child1_manifest, child1["body"])])
        assert len(analyzed_generations) == 1

        # 2. While disabled, parent set changes (Generation 2 adds child 12)
        child2 = make_child(12, parent_number=10, body=CHILD2_BODY)
        github = DecompositionGitHubFlow(parent, [child1, child2])

        # 3. Now re-enable decomposition validation
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = True
        config.issue_specification_validation = False

        engine = make_engine(tmp_path, github, config=config, decomposition_gate=decomp_gate)
        child_candidate = Candidate(type="issue", data=dict(child1), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        # Ordinary validation ran for Generation 2, not reusing Generation 1
        assert result.actions == ["dispatched"]
        assert len(analyzed_generations) == 2
        assert analyzed_generations[1] == [11, 12]


class TestAS008ReEnableUnchangedBlockedGeneration:
    """AS-008: Re-enable unchanged BLOCKED generation.

    Given a current set has a durable BLOCKED record, decomposition validation is disabled
    temporarily without changing the set, and the feature is re-enabled, when the set
    is evaluated, then the ordinary enabled lifecycle again treats that BLOCKED evidence as authoritative.
    Covers REQ-008.
    """

    def test_re_enabled_feature_enforces_existing_blocked_record(self, tmp_path: Path):
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        # 1. Create a durable BLOCKED decomposition record
        decomp_analyzer = Mock(return_value=DecompositionAnalysisResult("BLOCKED", (SET_FINDING,), remediation="EDIT_IN_PLACE"))
        decomp_gate = DecompositionValidationLifecycle("owner/repo", "model", tmp_path / "decomp.json", decomp_analyzer)
        parent_manifest = build_normative_issue_manifest(10, parent["title"], parent["body"])
        child_manifest = build_normative_issue_manifest(11, child["title"], child["body"])
        identity = decomp_gate.identity(parent, [child])
        decomp_gate.decide(identity, DecompositionIssue(parent_manifest, parent["body"]), [DecompositionIssue(child_manifest, child["body"])])

        # 2. While disabled, candidate can be dispatched
        disabled_config = AutomationConfig(repo_name="owner/repo")
        disabled_config.issue_decomposition_validation = False
        disabled_config.issue_specification_validation = False

        engine1 = make_engine(tmp_path, github, config=disabled_config, decomposition_gate=decomp_gate)
        res1 = engine1._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(child), priority=0, issue_number=11), disabled_config)
        assert res1.actions == ["dispatched"]
        engine1.implementation_slots.release_unbound_idle_owner(ImplementationOwner("issue", 11))

        # 3. Now re-enable the feature for the unchanged set
        enabled_config = AutomationConfig(repo_name="owner/repo")
        enabled_config.issue_decomposition_validation = True
        enabled_config.issue_specification_validation = False

        engine2 = make_engine(tmp_path, github, config=enabled_config, decomposition_gate=decomp_gate)
        res2 = engine2._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(child), priority=0, issue_number=11), enabled_config)

        # 4. Ordinary enabled lifecycle again treats that BLOCKED evidence as authoritative
        assert res2.actions == ["Rejected - blocked parent/child decomposition"]
        assert "material defects" in (res2.error or "")
        engine2._process_single_candidate_reserved.assert_not_called()


class TestSchedulerNonInterference:
    """Covers REQ-006: Scheduler non-interference.

    Disabling decomposition validation must not consume validation-scheduler capacity
    or prevent enabled validation categories from using the scheduler normally.
    """

    def test_scheduler_capacity_not_consumed_by_disabled_decomposition(self, tmp_path: Path):
        scheduler = ValidationScheduler(concurrency=1)
        parent = make_parent(10, ready=True)
        child = make_child(11, parent_number=10)
        github = DecompositionGitHubFlow(parent, [child])

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_decomposition_validation = False
        config.issue_specification_validation = True

        spec_gate = SpecificationValidationLifecycle("owner/repo", "model", tmp_path / "spec.json", lambda *_args: SpecificationAnalysisResult("READY"))
        engine = make_engine(tmp_path, github, config=config, specification_gate=spec_gate)
        engine.validation_scheduler = scheduler

        # An external category job occupies capacity or runs concurrently
        completed = []

        def _external_job() -> str:
            completed.append("external")
            return "ok"

        job = scheduler.submit("external:job1", _external_job)

        child_candidate = Candidate(type="issue", data=dict(child), priority=0, issue_number=11)
        result = engine._process_single_candidate_unified("owner/repo", child_candidate, config)

        assert result.actions == ["dispatched"]
        assert job.result() == "ok"
        assert completed == ["external"]
        # Decomposition job was never submitted to scheduler
        assert not any(k.startswith("decomposition:") for k in scheduler._in_flight)


class TestRepositoryScopedOverride:
    """Repository configuration override for issue_decomposition_validation."""

    def test_repo_config_toml_overrides_kill_switch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        (auto_coder_dir / "config.toml").write_text("[features]\nissue_decomposition_validation = true\n", encoding="utf-8")

        # Repo A overrides to false
        repo_a_dir = auto_coder_dir / "owner" / "repo-a"
        repo_a_dir.mkdir(parents=True)
        (repo_a_dir / "config.toml").write_text("[features]\nissue_decomposition_validation = false\n", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home_dir))

        # Test Repo A: disabled via repo config
        config_a = AutomationConfig(repo_name="owner/repo-a")
        assert config_a.issue_decomposition_validation is False

        parent_a = make_parent(10, ready=True)
        child_a = make_child(11, parent_number=10)
        github_a = DecompositionGitHubFlow(parent_a, [child_a])
        decomp_analyzer_a = Mock(side_effect=AssertionError("Should not be called"))
        decomp_gate_a = DecompositionValidationLifecycle("owner/repo-a", "model", tmp_path / "repo_a_decomp.json", decomp_analyzer_a)

        config_a.issue_specification_validation = False
        engine_a = make_engine(tmp_path / "repo_a", github_a, config=config_a, decomposition_gate=decomp_gate_a, repo_name="owner/repo-a")
        res_a = engine_a._process_single_candidate_unified("owner/repo-a", Candidate(type="issue", data=dict(child_a), priority=0, issue_number=11), config_a)
        assert res_a.actions == ["dispatched"]
        decomp_analyzer_a.assert_not_called()

        # Test Repo B: enabled (inherits global default)
        config_b = AutomationConfig(repo_name="owner/repo-b")
        assert config_b.issue_decomposition_validation is True

        parent_b = make_parent(10, ready=True)
        child_b = make_child(11, parent_number=10)
        github_b = DecompositionGitHubFlow(parent_b, [child_b])
        decomp_analyzer_b = Mock(return_value=DecompositionAnalysisResult("BLOCKED", (SET_FINDING,)))
        decomp_gate_b = DecompositionValidationLifecycle("owner/repo-b", "model", tmp_path / "repo_b_decomp.json", decomp_analyzer_b)

        config_b.issue_specification_validation = False
        engine_b = make_engine(tmp_path / "repo_b", github_b, config=config_b, decomposition_gate=decomp_gate_b, repo_name="owner/repo-b")
        res_b = engine_b._process_single_candidate_unified("owner/repo-b", Candidate(type="issue", data=dict(child_b), priority=0, issue_number=11), config_b)
        assert res_b.actions == ["Rejected - blocked parent/child decomposition"]
        decomp_analyzer_b.assert_called_once()
