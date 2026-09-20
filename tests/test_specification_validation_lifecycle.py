"""Generation-bound Issue specification validation lifecycle regressions."""

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from unittest.mock import Mock, call, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import IndividualRelationshipContext, SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue

BODY = "## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")


def lifecycle(tmp_path, verdict, analyzer=None, policy="provider/model-a"):
    result = SpecificationAnalysisResult(verdict, (FINDING,) if verdict == "BLOCKED" else ())
    return SpecificationValidationLifecycle("owner/repo", policy, tmp_path / "decisions.json", analyzer or (lambda _manifest, _body: result))


def test_caller_reconciled_relationship_context_participates_in_durable_identity(tmp_path):
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    gate = lifecycle(tmp_path, "READY", calls)
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    standalone = gate.decide(manifest, "Title", BODY)
    child_context = IndividualRelationshipContext(
        role="child",
        related_contracts='[{"issue_number":1727,"relationship":"parent","normative_manifest":[]}]',
    )
    child = gate.decide(manifest, "Title", BODY, child_context)

    assert standalone.identity.relationship_digest != child.identity.relationship_digest
    assert standalone.identity.specification_digest == child.identity.specification_digest
    assert calls.call_count == 2


def test_completed_decision_survives_restart_and_provider_change_but_not_text_change(tmp_path):
    """Issue #2081, REQ-001/REQ-004: only a text change invalidates; a routing-only change never does."""
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    first = lifecycle(tmp_path, "READY", calls)
    assert first.decide(manifest, "Title", BODY).verdict == "READY"
    restarted = lifecycle(tmp_path, "READY", Mock(side_effect=AssertionError("must reuse")))
    assert restarted.decide(manifest, "Title", BODY).verdict == "READY"
    # A different configured provider/backend/model string is execution
    # routing, not semantic policy: it must not create a new identity and
    # must reuse the durable decision without any backend call at all.
    reused = lifecycle(tmp_path, "READY", Mock(side_effect=AssertionError("must reuse")), policy="provider/model-b").decide(manifest, "Title", BODY)
    assert reused.verdict == "READY"
    changed = lifecycle(tmp_path, "READY", calls)
    changed.decide(build_normative_issue_manifest(1728, "Edited", BODY), "Edited", BODY)
    assert calls.call_count == 2


def test_error_is_not_persisted_and_is_retried(tmp_path):
    calls = Mock(side_effect=[SpecificationAnalysisResult("ERROR", error="outage"), SpecificationAnalysisResult("READY")])
    gate = lifecycle(tmp_path, "READY", calls)
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    assert gate.decide(manifest, "Title", BODY).verdict == "ERROR"
    assert gate.decide(manifest, "Title", BODY).verdict == "READY"
    assert calls.call_count == 2


def test_first_valid_contract_becomes_immutable_baseline_across_policy_and_edits(tmp_path):
    first = lifecycle(tmp_path, "READY", policy="policy-a")
    baseline_manifest = build_normative_issue_manifest(1728, "Baseline", BODY)
    first.decide(baseline_manifest, "Baseline", BODY)
    edited_body = BODY.replace("current", "latest")
    edited_manifest = build_normative_issue_manifest(1728, "Edited", edited_body)
    lifecycle(tmp_path, "READY", policy="policy-b").decide(edited_manifest, "Edited", edited_body)

    raw = json.loads((tmp_path / "individual_review_history.json").read_text())
    baseline = json.loads(raw["1728"]["baseline"])
    assert baseline["title"] == "Baseline"
    assert baseline["body"] == BODY
    assert baseline["requirements"] == [{"requirement_id": "REQ-001", "text": "Return the current value."}]


def test_invalid_manifest_does_not_establish_baseline(tmp_path):
    gate = lifecycle(tmp_path, "ERROR")
    invalid = build_normative_issue_manifest(1728, "Invalid", "No Requirements section")
    assert gate.decide(invalid, "Invalid", "No Requirements section").verdict == "ERROR"
    assert not (tmp_path / "individual_review_history.json").exists()


def test_only_current_applied_blocked_outcomes_enter_review_history(tmp_path):
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")
    gate = lifecycle(tmp_path, "BLOCKED", Mock(return_value=analysis))
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    decision = gate.decide(manifest, "Title", BODY)

    assert gate.apply_blocked(GitHubFlow([snapshot(body=BODY + " stale")]), decision) is None
    history_path = tmp_path / "individual_review_history.json"
    assert json.loads(history_path.read_text())["1728"]["applied_outcomes"] == []

    assert gate.apply_blocked(GitHubFlow([snapshot()] * 4), decision) is None
    assert gate.repair_rounds.count("individual", 1728) == 0
    raw = json.loads(history_path.read_text())
    assert len(raw["1728"]["applied_outcomes"]) == 1
    outcome = json.loads(raw["1728"]["applied_outcomes"][0])
    assert outcome["verdict"] == "BLOCKED"
    assert outcome["remediation"] == "EDIT_IN_PLACE"
    assert outcome["findings"][0]["requirement_ids"] == ["REQ-001"]


def test_current_reissue_required_is_durable_idempotent_and_survives_restart(tmp_path):
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="REISSUE_REQUIRED")
    gate = lifecycle(tmp_path, "BLOCKED", Mock(return_value=analysis))
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    decision = gate.decide(manifest, "Title", BODY)
    github = GitHubFlow([snapshot()] * 12)

    assert gate.apply_blocked(github, decision) is None
    assert gate.apply_blocked(github, decision) is None
    assert gate.is_reissue_required(1728)
    assert len(github.comments) == 1
    assert github.removals == 2
    restarted = SpecificationValidationLifecycle("owner/repo", "provider/model-a", tmp_path / "decisions.json")
    assert restarted.is_reissue_required(1728)


def test_stale_reissue_required_does_not_mark_subject(tmp_path):
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="REISSUE_REQUIRED")
    gate = lifecycle(tmp_path, "BLOCKED", Mock(return_value=analysis))
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    decision = gate.decide(manifest, "Title", BODY)
    github = GitHubFlow([snapshot(body=BODY + "\nEdited")])

    assert gate.apply_blocked(github, decision) is None
    assert not gate.is_reissue_required(1728)
    assert github.comments == []
    assert github.removals == 0


def test_repair_round_circuit_breaker_survives_restart_and_ready_keeps_final_chance(tmp_path):
    """AS-001/002/003/004/005/007/008 cross analysis, persistence and GitHub application."""
    blocked = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")
    decisions_path = tmp_path / "decisions.json"

    for generation in range(3):
        body = BODY + f"\nGeneration {generation}"
        gate = SpecificationValidationLifecycle("owner/repo", f"policy-{generation}", decisions_path, lambda *_args: blocked)
        decision = gate.decide(build_normative_issue_manifest(1728, "Title", body), "Title", body)
        initiated = []
        authorization = gate.authorize_automatic_repair(decision, lambda: True, lambda: initiated.append(gate.repair_rounds.count("individual", 1728)))
        assert authorization.automatic_repair_authorized
        assert initiated == [generation + 1]
        assert gate.apply_blocked(GitHubFlow([snapshot(body=body)] * 4), decision) is None

    restarted = SpecificationValidationLifecycle("owner/repo", "policy-final", decisions_path, lambda *_args: blocked)
    assert restarted.repair_rounds.count("individual", 1728) == 3

    # An ERROR beyond the limit remains unpersisted/retryable, and READY is not rewritten.
    final_body = BODY + "\nFinal chance"
    error_then_ready = Mock(side_effect=[SpecificationAnalysisResult("ERROR", error="temporary"), SpecificationAnalysisResult("READY")])
    ready_gate = SpecificationValidationLifecycle("owner/repo", "policy-ready", decisions_path, error_then_ready)
    manifest = build_normative_issue_manifest(1728, "Title", final_body)
    assert ready_gate.decide(manifest, "Title", final_body).verdict == "ERROR"
    assert ready_gate.decide(manifest, "Title", final_body).verdict == "READY"
    assert ready_gate.repair_rounds.count("individual", 1728) == 3

    blocked_body = BODY + "\nStill blocked"
    blocked_gate = SpecificationValidationLifecycle("owner/repo", "policy-blocked", decisions_path, lambda *_args: blocked)
    blocked_decision = blocked_gate.decide(build_normative_issue_manifest(1728, "Title", blocked_body), "Title", blocked_body)
    github = GitHubFlow([snapshot(body=blocked_body)] * 8)
    assert blocked_gate.apply_blocked(github, blocked_decision) is None
    applied = blocked_gate.store.get(blocked_decision.identity)
    assert applied is not None and applied.remediation == "EDIT_IN_PLACE"
    assert applied.remediation_reason == "automatic_repair_paused(repair_round_limit_reached)"
    assert blocked_gate.repair_rounds.is_paused("individual", 1728, blocked_decision.identity.specification_digest)
    assert not blocked_gate.is_reissue_required(1728)
    assert len(github.comments) == 1
    assert "Automatic repair has paused" in github.comments[0]["body"]
    assert "replacement/reissue is not required" in github.comments[0]["body"]

    # Exact/policy-only reuse is one generation, while a replacement number is clean.
    duplicate_body = BODY + "\nGeneration 0"
    duplicate_gate = SpecificationValidationLifecycle("owner/repo", "another-policy", decisions_path, lambda *_args: blocked)
    duplicate = duplicate_gate.decide(build_normative_issue_manifest(1728, "Title", duplicate_body), "Title", duplicate_body)
    duplicate_gate.apply_blocked(GitHubFlow([snapshot(body=duplicate_body)] * 4), duplicate)
    assert duplicate_gate.repair_rounds.count("individual", 1728) == 3
    assert duplicate_gate.repair_rounds.count("individual", 200) == 0


def test_production_dispatch_authorizes_three_repair_rounds_then_pauses_uncounted(tmp_path):
    """REQ-003, REQ-005, REQ-014: real production processing authorizes/counts rounds.

    Drives ``AutomationEngine._process_single_candidate_unified`` -- the actual
    normal-worker production boundary, not the lifecycle API directly -- through
    three previously-unassociated BLOCKED + EDIT_IN_PLACE generations and asserts
    each durably authorizes and counts exactly one automatic repair round before
    its diagnostic/readiness effects publish. A fourth previously-unassociated
    generation must then pause the episode at the durable limit, leaving the
    count unchanged and issuing no automatic repair or reissue marker.
    """
    blocked = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")
    decisions_path = tmp_path / "production-decisions.json"
    gate = SpecificationValidationLifecycle("owner/repo", "policy", decisions_path, lambda *_args: blocked)
    engine = AutomationEngine(Mock(), config=AutomationConfig())
    engine._specification_validators["owner/repo"] = gate
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "production-slots.json")

    for generation in range(3):
        body = BODY + f"\n- REQ-{generation + 2:03d}: Generation {generation} marker."
        engine.github = GitHubFlow([snapshot(body=body)] * 20)
        candidate = Candidate(type="issue", data={"number": 1728, "title": "Title", "body": body}, priority=0)
        result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
        assert result.actions == ["Rejected - blocked specification"]
        assert gate.repair_rounds.count("individual", 1728) == generation + 1
        decision = gate.store.get(gate.identity(1728, "Title", body))
        assert decision is not None and decision.remediation_reason is None

    final_body = BODY + "\n- REQ-005: Generation 3 marker."
    engine.github = GitHubFlow([snapshot(body=final_body)] * 20)
    candidate = Candidate(type="issue", data={"number": 1728, "title": "Title", "body": final_body}, priority=0)
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Rejected - blocked specification"]
    # The circuit breaker pauses without consuming a fourth round.
    assert gate.repair_rounds.count("individual", 1728) == 3
    assert gate.repair_rounds.is_paused("individual", 1728, gate.identity(1728, "Title", final_body).specification_digest)
    assert not gate.is_reissue_required(1728)
    paused_decision = gate.store.get(gate.identity(1728, "Title", final_body))
    assert paused_decision is not None and paused_decision.remediation == "EDIT_IN_PLACE"
    assert paused_decision.remediation_reason == "automatic_repair_paused(repair_round_limit_reached)"
    assert "Automatic repair has paused" in engine.github.comments[-1]["body"]


def test_concurrent_paths_coalesce_semantic_validation(tmp_path):
    barrier = Barrier(2)
    calls = 0
    guard = Lock()

    def analyze(_manifest, _body):
        nonlocal calls
        with guard:
            calls += 1
        return SpecificationAnalysisResult("READY")

    gate = lifecycle(tmp_path, "READY", analyze)
    manifest = build_normative_issue_manifest(1728, "Title", BODY)

    def run():
        barrier.wait()
        return gate.decide(manifest, "Title", BODY).verdict

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _value: run(), range(2))) == ["READY", "READY"]
    assert calls == 1


REVIEWER_LOGIN = "auto-coder-reviewer[bot]"
REVIEWER_APP_ID = 990001


class GitHubFlow:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.last = snapshots[-1]
        self.comments = []
        self.removals = 0

    def get_issue_dispatch_snapshot_strict(self, _repo, _number):
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return dict(self.last)

    def get_issue_comments_strict(self, _repo, _number):
        return list(self.comments)

    def add_comment_to_issue(self, _repo, _number, body):
        self.comments.append({"body": body})

    def publish_issue_review_comment(self, repo, number, body, authorize_fn):
        from auto_coder.issue_review_publication import PublicationReceipt

        comment_id = len(self.comments) + 1
        self.comments.append({"id": comment_id, "body": body, "user": {"login": REVIEWER_LOGIN}, "performed_via_github_app": {"id": REVIEWER_APP_ID}})
        return PublicationReceipt(comment_id, REVIEWER_LOGIN, REVIEWER_APP_ID)

    def reviewer_app_identity(self, _repo):
        from auto_coder.github_app_reviewer import ReviewerAppIdentity

        return ReviewerAppIdentity(login=REVIEWER_LOGIN, app_id=REVIEWER_APP_ID)

    def remove_labels(self, _repo, _number, _labels, item_type="issue"):
        assert item_type == "issue"
        self.removals += 1

    def get_open_sub_issues(self, _repo, _number):
        return []


def snapshot(title="Title", body=BODY, ready=True):
    return {"number": 1728, "title": title, "body": body, "labels": [{"name": "implementation-ready"}] if ready else []}


def engine_with_gate(tmp_path, github, gate):
    engine = AutomationEngine(github, config=AutomationConfig())
    engine._specification_validators["owner/repo"] = gate
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    # Isolated from the real default path so production-ownership tombstones
    # (#2061) from one test/engine instance can never leak into another.
    engine.issue_stage_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    engine._process_single_candidate_reserved = Mock(return_value=CandidateProcessingResult("issue", 1728, "Title", True, ["dispatched"]))
    candidate = Candidate(type="issue", data={"number": 1728, "title": "Title", "body": BODY}, priority=0)
    return engine, candidate


def test_production_dispatch_gate_rejects_stale_ready_before_slot(tmp_path):
    github = GitHubFlow([snapshot(), snapshot(body=BODY + "\nEdited")])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, explicit_only=True, force=True)
    assert result.actions == ["Skipped - validated Issue generation is stale or no longer submitted"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert not (tmp_path / "slots.json").exists()


def test_production_dispatch_rejects_closed_ready_issue_before_slot(tmp_path):
    closed = {**snapshot(), "state": "closed"}
    github = GitHubFlow([closed])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Skipped - missing implementation-ready label"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


def test_processing_label_does_not_change_durable_admission(tmp_path):
    labeled = snapshot()
    labeled["labels"].append("@auto-coder")
    github = GitHubFlow([labeled, labeled, labeled])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    candidate.data["labels"] = labeled["labels"]

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)

    assert result.actions == ["dispatched"]
    engine._process_single_candidate_reserved.assert_called_once()
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 1728),)


def test_pre_admission_authoritative_failure_requests_refill_retry(tmp_path):
    github = GitHubFlow([snapshot(), snapshot()])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    original = github.get_issue_dispatch_snapshot_strict
    calls = 0

    def fail_pre_admission(repo, number):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("temporary pre-admission outage")
        return original(repo, number)

    github.get_issue_dispatch_snapshot_strict = fail_pre_admission
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.refill_retry_required is True
    assert "temporary pre-admission outage" in (result.error or "")
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


def test_open_child_rejects_parent_before_ownership(tmp_path):
    github = GitHubFlow([snapshot(), snapshot()])
    github.get_open_sub_issues = Mock(return_value=[1729])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Skipped - unresolved Issue hierarchy dependency"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


def test_real_refill_continues_from_parent_with_open_child_to_eligible_issue(tmp_path):
    github = Mock()
    issues = {
        20: {**snapshot(title="Parent"), "number": 20, "state": "open", "labels": [{"name": "implementation-ready"}, {"name": "urgent"}]},
        30: {**snapshot(title="Leaf"), "number": 30, "state": "open"},
    }
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(issues[number])
    github.get_issue_details.side_effect = lambda issue: issue
    github.get_open_sub_issues.side_effect = lambda _repo, number: [21] if number == 20 else []
    github.get_item_type_strict.return_value = "issue"
    github.try_add_labels.return_value = True
    github.get_issue.side_effect = lambda _repo, number: issues[number]
    engine = AutomationEngine(github, config=AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._specification_validators["owner/repo"] = lifecycle(tmp_path, "READY")
    dispatched = []
    engine._process_single_candidate_reserved = Mock(side_effect=lambda _repo, candidate, *_args, **_kwargs: dispatched.append(candidate.data["number"]) or CandidateProcessingResult("issue", candidate.data["number"], success=True))

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert dispatched == [30]
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 30),)


def test_elder_sibling_rejects_leaf_before_ownership(tmp_path):
    child = snapshot()
    github = GitHubFlow([child, child])
    github.get_open_sub_issues = Mock(side_effect=lambda _repo, number: [19, 1728] if number == 10 else [])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    candidate.data["parent_issue_number"] = 10
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["dispatched"]
    engine._process_single_candidate_reserved.assert_called_once()
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 1728),)


def test_specification_error_keeps_real_refill_pending_then_admits(tmp_path):
    github = Mock()
    issue = {**snapshot(), "state": "open"}
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(1728)])
    github.get_issue_dispatch_snapshot_strict.return_value = issue
    github.get_issue_details.side_effect = lambda value: value
    github.get_open_sub_issues.return_value = []
    github.get_item_type_strict.return_value = "issue"
    github.try_add_labels.return_value = True
    github.get_issue.return_value = issue
    decisions = Mock(side_effect=[SpecificationAnalysisResult("ERROR", error="outage"), SpecificationAnalysisResult("READY")])
    engine = AutomationEngine(github, config=AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "validator", tmp_path / "retry.json", decisions)
    dispatched = Mock(return_value=CandidateProcessingResult("issue", 1728, success=True))
    engine._process_single_candidate_reserved = dispatched

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is False
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    dispatched.assert_called_once()


def test_refill_builds_metadata_hierarchy_and_continues_past_younger_sibling(tmp_path):
    github = Mock()
    issues = {
        10: {**snapshot(title="Parent"), "number": 10, "state": "open", "labels": ["implementation-ready", "breaking-change"]},
        19: {**snapshot(title="Elder", ready=False), "number": 19, "body": "Parent-Issue: #10\n\n" + BODY, "state": "open"},
        20: {**snapshot(title="Younger"), "number": 20, "body": "Parent-Issue: #10\n\n" + BODY, "state": "open", "labels": ["implementation-ready", "urgent"]},
        30: {**snapshot(title="Eligible"), "number": 30, "state": "open"},
    }
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(number) for number in issues])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(issues[number])
    github.get_issue_details.side_effect = lambda issue: issue
    github.get_open_sub_issues.return_value = []
    github.get_item_type_strict.return_value = "issue"
    github.get_issue_comments_strict.return_value = []
    github.try_add_labels.return_value = True
    github.get_issue.side_effect = lambda _repo, number: issues[number]
    engine = AutomationEngine(github, config=AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "metadata-slots.json")
    engine._specification_validators["owner/repo"] = lifecycle(tmp_path, "READY", policy="metadata-validator")
    dispatched = []
    engine._process_single_candidate_reserved = Mock(side_effect=lambda _repo, candidate, *_args, **_kwargs: dispatched.append(candidate.data["number"]) or CandidateProcessingResult("issue", candidate.data["number"], success=True))

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert dispatched == [20]
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 20),)


def test_hierarchy_uses_newly_authorized_parent_metadata(tmp_path):
    old = {**snapshot(), "body": "Parent-Issue: #10\n\n" + BODY}
    current = {**snapshot(), "body": "Parent-Issue: #11\n\n" + BODY}
    github = GitHubFlow([current, current, current])
    github.get_open_sub_issues = Mock(return_value=[])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    candidate.data.update(old)
    candidate.data["refill_metadata_open_children"] = {11: [19, 20]}

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["dispatched"]
    engine._process_single_candidate_reserved.assert_called_once()
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 1728),)


@pytest.mark.parametrize(
    "child_state,child_author,native_parent,expected_dispatch",
    [
        ("open", 999, None, 30),
        ("closed", 1, None, 10),
        ("open", 1, 11, 30),
    ],
)
def test_real_refill_graph_uses_all_open_issues_and_native_precedence(tmp_path, child_state, child_author, native_parent, expected_dispatch):
    def issue(number, title, labels, body=BODY, state="open", author=1):
        return {
            "id": number * 100,
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": label} for label in labels],
            "user": {"login": f"user-{author}", "id": author},
            "updated_at": "2024-01-01T00:00:00Z",
            "created_at": "2020-01-01T00:00:00Z",
        }

    issues = {
        10: issue(10, "Parent", ["implementation-ready", "breaking-change"]),
        20: issue(20, "Child", ["implementation-ready"], "Parent-Issue: #10\n\n" + BODY, child_state, child_author),
        30: issue(30, "Fallback", ["implementation-ready"]),
    }
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance(token="test-token")
    github.get_open_entities_strict = Mock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(number) for number, snapshot in issues.items() if snapshot["state"] == "open"]))
    github.get_open_issues_json = Mock(side_effect=lambda _repo: [dict(snapshot) for snapshot in issues.values() if snapshot["state"] == "open"])
    github.get_open_issue_declarations = github.get_open_issues_json
    github.get_issue_dispatch_snapshot_strict = Mock(side_effect=lambda _repo, number: dict(issues[number]))
    github.get_parent_issue_number_strict = Mock(side_effect=lambda _repo, number: native_parent if number == 20 else None)
    parents = {20: native_parent} if native_parent is not None else {}
    github.get_parent_issue_details_strict = Mock(side_effect=lambda _repo, number: ({"number": parents[number], "state": "open"} if number in parents else None))
    github.get_direct_sub_issues_strict = Mock(side_effect=lambda _repo, number: [dict(issues[20])] if parents.get(20) == number else [])
    github.add_sub_issue_strict = Mock(side_effect=lambda _repo, parent, child, _id: parents.__setitem__(child, parent))
    github.get_open_sub_issues_strict = Mock(return_value=[])
    github.get_item_type_strict = Mock(return_value="issue")
    github.try_add_labels = Mock(return_value=True)
    github.get_issue = Mock(side_effect=lambda _repo, number: issues[number])
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = [1]
    engine = AutomationEngine(github, config=config)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / f"graph-{child_state}-{child_author}-{native_parent}.json")
    engine._specification_validators["owner/repo"] = lifecycle(tmp_path, "READY", policy=f"graph-{child_state}-{child_author}-{native_parent}")
    dispatched = []
    engine._process_single_candidate_reserved = Mock(side_effect=lambda _repo, candidate, *_args, **_kwargs: dispatched.append(candidate.data["number"]) or CandidateProcessingResult("issue", candidate.data["number"], success=True))

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is (expected_dispatch is not None)
    assert dispatched == ([] if expected_dispatch is None else [expected_dispatch])
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", expected_dispatch),)


def test_production_blocked_gate_generation_checks_and_deduplicates_effects(tmp_path):
    revised = snapshot(body=BODY + "\nEdited")
    github = GitHubFlow([snapshot(), revised])
    gate = lifecycle(tmp_path, "BLOCKED")
    engine, candidate = engine_with_gate(tmp_path, github, gate)
    first = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert first.actions == ["Rejected - blocked specification"]
    assert github.comments == []
    assert github.removals == 0
    assert not (tmp_path / "slots.json").exists()

    github.snapshots = [snapshot(), snapshot(), snapshot()]
    github.last = snapshot()
    second = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert second.actions == ["Rejected - blocked specification"]
    assert len(github.comments) == 1
    assert github.removals == 1
    github.snapshots = [snapshot(), snapshot(), snapshot()]
    engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert len(github.comments) == 1
    assert github.removals == 2


def test_production_error_preserves_submission_without_github_mutation(tmp_path):
    github = GitHubFlow([snapshot()])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "ERROR"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Deferred - specification validation error"]
    assert github.comments == []
    assert github.removals == 0
    assert not (tmp_path / "decisions.json").exists()
    engine._process_single_candidate_reserved.assert_not_called()


def test_blocked_side_effect_failure_remains_observable_and_denies_dispatch(tmp_path):
    class FailingGitHub(GitHubFlow):
        def remove_labels(self, *_args, **_kwargs):
            raise RuntimeError("label API unavailable")

    github = FailingGitHub([snapshot(), snapshot(), snapshot()])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "BLOCKED"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Rejected - blocked specification (side effects incomplete)"]
    assert "label API unavailable" in (result.error or "")
    assert len(github.comments) == 1
    engine._process_single_candidate_reserved.assert_not_called()


def test_production_dispatch_replaces_stale_candidate_payload_with_validated_snapshot(tmp_path):
    revised = snapshot(title="Revised title", body=BODY.replace("current", "revised"))
    github = GitHubFlow([revised, revised])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    captured = []
    engine._process_single_candidate_reserved.side_effect = lambda _repo, dispatched, *_args, **_kwargs: captured.append(dict(dispatched.data)) or CandidateProcessingResult("issue", 1728, "Revised title", True, ["dispatched"])
    assert engine._process_single_candidate_unified("owner/repo", candidate, engine.config).success is True
    assert captured[0]["title"] == "Revised title"
    assert captured[0]["body"] == revised["body"]


def test_concurrent_force_and_ordinary_paths_cannot_both_dispatch(tmp_path):
    github = GitHubFlow([snapshot()] * 6)
    gate = lifecycle(tmp_path, "READY")
    engine, candidate = engine_with_gate(tmp_path, github, gate)
    entered = Barrier(2)
    release = Barrier(2)
    dispatches = []

    def dispatch(*_args, **_kwargs):
        dispatches.append("started")
        entered.wait()
        release.wait()
        return CandidateProcessingResult("issue", 1728, "Title", True, ["dispatched"])

    engine._process_single_candidate_reserved.side_effect = dispatch
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(engine._process_single_candidate_unified, "owner/repo", candidate, engine.config)
        entered.wait()
        second = pool.submit(engine._process_single_candidate_unified, "owner/repo", candidate, engine.config, False, True, True)
        second_result = second.result(timeout=5)
        release.wait()
        assert first.result(timeout=5).success is True
    assert len(dispatches) == 1
    assert second_result.actions == ["Deferred - implementation ownership already exists (issue:1728)"]


def test_changed_generation_revalidates_while_live_implementation_remains_owned(tmp_path):
    state = {1728: snapshot(body=BODY + " A")}
    validation_b_started = Event()

    class MutableGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return dict(state[number])

    github = MutableGitHub([state[1728]])

    def analyze(_manifest, body):
        if body.endswith(" B"):
            validation_b_started.set()
            assert ImplementationOwner("issue", 1728) in engine.implementation_slots.active_owners()
            return SpecificationAnalysisResult("BLOCKED", (FINDING,))
        return SpecificationAnalysisResult("READY")

    gate = SpecificationValidationLifecycle("owner/repo", "validator", tmp_path / "live.json", analyze)
    engine, candidate = engine_with_gate(tmp_path, github, gate)
    entered = Event()
    release = Event()

    def live_dispatch(*_args, **_kwargs):
        entered.set()
        assert release.wait(5)
        return CandidateProcessingResult("issue", 1728, "Title", True, ["dispatched"])

    engine._process_single_candidate_reserved.side_effect = live_dispatch
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(engine._process_single_candidate_unified, "owner/repo", candidate, engine.config)
        assert entered.wait(5)
        state[1728] = snapshot(body=BODY + " B")
        revalidated = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
        assert revalidated.actions == ["Rejected - blocked specification"]
        assert validation_b_started.is_set()
        owner = ImplementationOwner("issue", 1728)
        assert engine.implementation_slots.active_execution_ids(owner)
        release.set()
        assert running.result(timeout=5).success is True
    assert engine.implementation_slots.active_execution_ids(owner) == ()


def test_resubmitted_unchanged_blocked_generation_removes_label_again_after_restart(tmp_path):
    class StatefulGitHub(GitHubFlow):
        def remove_labels(self, *_args, **_kwargs):
            self.removals += 1
            self.last["labels"] = []

        def submit(self):
            self.last["labels"] = [{"name": "implementation-ready"}]
            self.snapshots = [dict(self.last), dict(self.last), dict(self.last)]

    github = StatefulGitHub([snapshot(), snapshot(), snapshot()])
    first_gate = lifecycle(tmp_path, "BLOCKED")
    engine, candidate = engine_with_gate(tmp_path, github, first_gate)
    engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert github.removals == 1
    github.submit()
    engine._specification_validators["owner/repo"] = lifecycle(tmp_path, "BLOCKED", Mock(side_effect=AssertionError("must reuse")))
    engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert github.removals == 2
    assert len(github.comments) == 1


def test_supported_high_score_fallback_model_changes_execution_provenance(monkeypatch):
    """``configured_provider_identity()`` still varies with the configured model.

    Issue #2081, REQ-005: this is execution *provenance*, not the semantic
    *policy* identity used for decision reuse (see
    ``validation_policy_identity`` and ``ValidationDecision.execution_provenance``);
    this route snapshot is expected to keep varying with configuration.
    """
    from auto_coder.llm_backend_config import LLMBackendConfiguration
    from auto_coder.specification_validation_lifecycle import configured_provider_identity

    def config(model):
        return LLMBackendConfiguration.load_from_dict({"backend_with_high_score": {"order": ["codex"]}, "backends": {"codex": {"model": model}}})

    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda: config("model-a"))
    first = configured_provider_identity()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda: config("model-b"))
    second = configured_provider_identity()
    assert first != second
    assert "model-a" in first
    assert "model-b" in second


def test_concurrent_different_issue_records_both_survive_restart(tmp_path):
    path = tmp_path / "decisions.json"
    calls = []

    def analyze(manifest, _body):
        calls.append(manifest.issue_number)
        return SpecificationAnalysisResult("READY")

    gate = SpecificationValidationLifecycle("owner/repo", "provider/model", path, analyze)
    manifests = [build_normative_issue_manifest(number, f"Title {number}", BODY) for number in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda manifest: gate.decide(manifest, manifest.title, BODY).verdict, manifests)) == ["READY", "READY"]
    restarted = SpecificationValidationLifecycle("owner/repo", "provider/model", path, Mock(side_effect=AssertionError("must reuse")))
    assert [restarted.decide(manifest, manifest.title, BODY).verdict for manifest in manifests] == ["READY", "READY"]
    assert sorted(calls) == [1, 2]


def test_supported_alias_provider_change_changes_execution_provenance(monkeypatch):
    """``configured_provider_identity()`` still varies with the configured backend alias.

    Issue #2081, REQ-001/REQ-005: this route snapshot is execution provenance
    only. It must keep varying with configuration for observability, but a
    change here must never, by itself, invalidate a durable decision (see
    the reuse regressions in this module for that half of the contract).
    """
    from auto_coder.llm_backend_config import LLMBackendConfiguration
    from auto_coder.specification_validation_lifecycle import configured_provider_identity

    def config(backend_type):
        return LLMBackendConfiguration.load_from_dict(
            {
                "backend_adversarial_validation": {"order": ["reviewer"]},
                "backends": {"reviewer": {"backend_type": backend_type, "model": "shared-model"}},
            }
        )

    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda: config("codex"))
    codex_identity = configured_provider_identity()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda: config("claude"))
    claude_identity = configured_provider_identity()
    assert codex_identity != claude_identity
    assert '"provider":"codex"' in codex_identity
    assert '"provider":"claude"' in claude_identity


def test_async_logical_owner_allows_changed_generation_validation(tmp_path):
    """A remote implementation owner remains authoritative after launch execution returns."""
    TraceCollector._instance = None
    state = {"body": BODY + " A"}
    analyzed = []

    class AsyncGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return snapshot(body=state["body"])

        def get_issue(self, _repo, number):
            return {"number": number, "state": "open"}

        def get_issue_details(self, issue):
            return issue

    def analyze(_manifest, body):
        analyzed.append(body)
        return SpecificationAnalysisResult("READY")

    github = AsyncGitHub([snapshot(body=state["body"])])
    engine, candidate = engine_with_gate(
        tmp_path,
        github,
        SpecificationValidationLifecycle("owner/repo", "validator", tmp_path / "async.json", analyze),
    )
    owner = ImplementationOwner("issue", 1728)

    def launch_async(*_args, **_kwargs):
        assert engine.implementation_slots.record_provider_session(owner, "remote-session-a") is True
        return CandidateProcessingResult("issue", 1728, "Title", True, ["launched"])

    engine._process_single_candidate_reserved.side_effect = launch_async
    launched = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert launched.success is True
    assert engine.implementation_slots.active_execution_ids(owner) == ()
    assert owner in engine.implementation_slots.active_owners()

    state["body"] = BODY + " B"
    deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    assert analyzed == [BODY + " A", BODY + " B"]
    assert owner in engine.implementation_slots.active_owners()
    trace_snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=1728)
    retained_results = [event for event in trace_snapshot.events if event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "issue.individual-validation-job" and event.facts and event.facts.get("caller_origin") == "retained-owner-reevaluation"]
    assert len(retained_results) == 1
    assert retained_results[0].outcome == Outcome.COMPLETED.value
    assert retained_results[0].facts["evaluation_source"] == "model"


def test_reconciliation_edit_is_rechecked_before_retry_ownership(tmp_path):
    """Capacity retry cannot carry READY across authoritative reconciliation I/O."""
    current = {"body": BODY + " A"}

    class ReconcileGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return snapshot(body=current["body"])

        def get_issue(self, _repo, number):
            assert number == 99
            current["body"] = BODY + " B"
            return {"number": 99, "state": "closed"}

        def get_issue_details(self, issue):
            return issue

        def get_connected_prs(self, _repo, _number, strict=False):
            assert strict is True
            return []

    github = ReconcileGitHub([snapshot(body=current["body"])])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    assert engine.implementation_slots.reserve(ImplementationOwner("issue", 99)) is True
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Skipped - validated Issue generation changed during capacity reconciliation"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


def test_production_jules_launch_registers_retained_provider_ownership(monkeypatch, tmp_path):
    """Shared admission consumes the real Jules/CloudManager launch persistence."""
    from auto_coder.issue_processor import _process_issue_jules_mode

    monkeypatch.setenv("HOME", str(tmp_path))
    current = {"body": BODY + " A"}
    github = Mock()
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "title": "Async",
        "body": current["body"],
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_all_sub_issues.return_value = []
    github.get_item_type_strict.return_value = "issue"
    github.try_add_labels.return_value = True
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    analyzed = []

    def analyze(_manifest, body):
        analyzed.append((body, slots.active_owners()))
        if body.endswith(" B"):
            return SpecificationAnalysisResult("BLOCKED", (FINDING,))
        return SpecificationAnalysisResult("READY")

    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "validator",
        tmp_path / "validations.json",
        analyze,
    )
    jules = Mock()
    jules.start_session.return_value = "real-session-a"

    def production_launch(repo, issue_data, config, client, label_context=None, implementation_slots=None):
        assert implementation_slots is slots
        return _process_issue_jules_mode(repo, issue_data, config, client, label_context)

    candidate = Candidate(type="issue", data={"number": 1728, "title": "Async", "body": current["body"]}, priority=0)
    with (
        patch("auto_coder.issue_processor._process_issue_cloud_backend", side_effect=production_launch),
        patch("auto_coder.issue_processor.JulesClient", return_value=jules),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        launched = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, jules_mode=True)
    assert launched.success is True
    owner = ImplementationOwner("issue", 1728)
    assert slots.has_provider_sessions(owner) is True
    assert slots.active_execution_ids(owner) == ()

    current["body"] = BODY + " B"
    deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, jules_mode=True)
    assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    assert analyzed == [(BODY + " A", ())]

    # The actual stale-session daemon stops remote A and retires its production
    # membership before it semantically validates submitted generation B.
    github.get_issue.return_value = {"number": 1728, "state": "open"}
    github.get_issue_details.return_value = {"number": 1728, "state": "open"}
    github.get_issue_comments_strict.return_value = []
    github.has_linked_pr.return_value = False
    github.get_direct_sub_issues_strict.return_value = []
    github.get_parent_issue_details_strict.return_value = None
    github.get_issue_comments_strict.return_value = []
    stale_jules = Mock()
    stale_jules.get_session.side_effect = [{"state": "IN_PROGRESS"}, {"state": "COMPLETED"}]
    stale_jules.list_sessions.return_value = [
        {
            "name": "sessions/real-session-a",
            "state": "IN_PROGRESS",
            "createTime": "2000-01-01T00:00:00Z",
            "outputs": {},
        }
    ]
    with (
        patch("auto_coder.issue_processor.JulesClient", return_value=stale_jules),
        patch("auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("auto_coder.issue_processor.increment_attempt") as increment,
        patch("auto_coder.issue_processor._take_issue_actions") as replacement,
    ):
        engine.handle_stale_jules_issue_sessions("owner/repo")
        assert analyzed == [(BODY + " A", ())]
        assert owner in slots.active_owners()
        assert slots.start_execution(ImplementationOwner("issue", 99)) is None

        engine.handle_stale_jules_issue_sessions("owner/repo")
    assert stale_jules.send_message.call_args_list == [
        call("real-session-a", "stop"),
        call("real-session-a", "stop"),
    ]
    assert analyzed[-1] == (BODY + " B", ())
    assert owner not in slots.active_owners()
    increment.assert_not_called()
    replacement.assert_not_called()


def test_blocked_edit_during_comment_lookup_prevents_stale_publication(tmp_path):
    """Shared BLOCKED processing rechecks generation after deduplication I/O."""
    current = {"body": BODY}

    class EditingCommentsGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return snapshot(body=current["body"])

        def get_issue_comments_strict(self, _repo, _number):
            current["body"] = BODY + "\nRevised during comment lookup"
            return []

    github = EditingCommentsGitHub([snapshot()])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "BLOCKED"))
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Rejected - blocked specification"]
    assert github.comments == []
    assert github.removals == 0
    engine._process_single_candidate_reserved.assert_not_called()


def test_completed_local_generation_owner_is_retired_before_changed_validation(tmp_path):
    """A real shared local route cannot retain bare A ownership during B validation."""
    current = {"body": BODY + " A"}
    github = Mock()
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "title": "Local",
        "body": current["body"],
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_item_type_strict.return_value = "issue"
    github.get_all_sub_issues.return_value = []
    github.try_add_labels.return_value = True
    github.get_issue.return_value = {"number": 1728, "state": "open"}
    github.get_issue_details.return_value = {"number": 1728, "state": "open"}
    github.get_issue_comments_strict.return_value = []
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    owner = ImplementationOwner("issue", 1728)
    observations = []

    def analyze(_manifest, body):
        observations.append((body, slots.active_owners()))
        if body.endswith(" B"):
            return SpecificationAnalysisResult("BLOCKED", (FINDING,))
        return SpecificationAnalysisResult("READY")

    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "validator", tmp_path / "local.json", analyze)
    candidate = Candidate(type="issue", data={"number": 1728, "title": "Local", "body": current["body"]}, priority=0)
    with patch.object(engine, "_take_issue_actions", return_value=["implemented A"]) as local_backend:
        first = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert first.success is True
    local_backend.assert_called_once()
    assert slots.active_execution_ids(owner) == ()
    assert owner in slots.active_owners()

    current["body"] = BODY + " B"
    blocked = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert blocked.actions == ["Rejected - blocked specification"]
    assert observations[-1] == (BODY + " B", ())
    assert owner not in slots.active_owners()


def test_real_local_pr_creation_preserves_capacity_across_issue_edit(tmp_path):
    """Production PR creation records membership before local launch cleanup."""
    from auto_coder.issue_processor import _create_pr_for_issue

    current = {"body": BODY + " A"}
    github = Mock(token="token")
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "title": "Local PR",
        "body": current["body"],
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_item_type_strict.return_value = "issue"
    github.get_all_sub_issues.return_value = []
    github.try_add_labels.return_value = True
    github.get_issue.return_value = {"number": 1728, "state": "open"}
    github.get_issue_details.return_value = {"number": 1728, "state": "open"}
    github.find_pr_by_head_branch.return_value = None
    github.get_pr_closing_issues.return_value = [1728]
    github.get_labels.return_value = []
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    analyzed = []
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "validator",
        tmp_path / "local-pr.json",
        lambda _manifest, body: analyzed.append(body) or SpecificationAnalysisResult("READY"),
    )
    api = Mock()
    api.pulls.create.return_value = {"number": 100, "html_url": "https://github.test/pull/100"}

    def create_real_pr(_repo, issue_data, backend_manager=None):
        return [
            _create_pr_for_issue(
                "owner/repo",
                issue_data,
                "issue-1728",
                "main",
                "implemented",
                github,
                engine.config,
                implementation_slots=slots,
            )
        ]

    candidate = Candidate(type="issue", data={"number": 1728, "title": "Local PR", "body": current["body"]}, priority=0)
    with (
        patch.object(engine, "_take_issue_actions", side_effect=create_real_pr),
        patch("auto_coder.issue_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.issue_processor.run_llm_noedit_prompt", return_value=""),
        patch("auto_coder.issue_processor.validate_issue_references"),
        patch("time.sleep"),
    ):
        assert engine._process_single_candidate_unified("owner/repo", candidate, engine.config).success is True
    owner = ImplementationOwner("issue", 1728)
    assert slots.active_execution_ids(owner) == ()
    assert slots.active_owners() == (owner,)

    current["body"] = BODY + " B"
    deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    assert analyzed == [BODY + " A"]
    assert slots.start_execution(ImplementationOwner("issue", 99)) is None


@pytest.mark.parametrize("route", ["ordinary-cloud", "high-score-cloud"])
def test_cloud_fallback_pr_preserves_capacity_across_issue_edit(tmp_path, route):
    """Cloud fallback propagates production slot ownership into PR creation."""
    from auto_coder.issue_processor import _create_pr_for_issue

    current = {"body": BODY + " A"}
    route_labels = [{"name": "implementation-ready"}]
    if route == "high-score-cloud":
        route_labels.append({"name": "difficult"})
    github = Mock(token="token")
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "title": "Fallback PR",
        "body": current["body"],
        "state": "open",
        "labels": route_labels,
    }
    github.get_item_type_strict.return_value = "issue"
    github.get_all_sub_issues.return_value = []
    github.get_parent_issue_details.return_value = None
    github.get_open_sub_issues.return_value = []
    github.get_direct_sub_issues_strict.return_value = []
    github.get_parent_issue_details_strict.return_value = None
    github.try_add_labels.return_value = True
    github.find_pr_by_head_branch.return_value = None
    github.get_pr_closing_issues.return_value = [1728]
    github.get_labels.return_value = []
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / f"{route}-slots.json")
    engine.implementation_slots = slots
    analyzed = []
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "validator",
        tmp_path / f"{route}-validations.json",
        lambda _manifest, body: analyzed.append(body) or SpecificationAnalysisResult("READY"),
    )
    llm_config = Mock(
        backend_cloud_order=["unsupported"],
        backend_cloud_priority_groups=[],
        backend_with_high_score_cloud_order=["unsupported"],
    )
    llm_config.get_backend_cloud.return_value = None
    llm_config.get_backend_with_high_score_cloud.return_value = None
    llm_config.get_backend_config.return_value = Mock(backend_type="unsupported")
    api = Mock()
    api.pulls.create.return_value = {"number": 100, "html_url": "https://github.test/pull/100"}

    def fallback_actions(repo, issue_data, config, client, **kwargs):
        assert kwargs["implementation_slots"] is slots
        return [
            _create_pr_for_issue(
                repo,
                issue_data,
                "issue-1728",
                "main",
                "implemented",
                client,
                config,
                implementation_slots=kwargs["implementation_slots"],
            )
        ]

    candidate = Candidate(type="issue", data={"number": 1728}, priority=0)
    with (
        patch("auto_coder.llm_backend_config.get_llm_config", return_value=llm_config),
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", side_effect=lambda values, _config: values),
        patch("auto_coder.issue_processor._apply_issue_actions_directly", side_effect=fallback_actions),
        patch("auto_coder.issue_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.issue_processor.run_llm_noedit_prompt", return_value=""),
        patch("auto_coder.issue_processor.validate_issue_references"),
        patch("auto_coder.cli_helpers.create_cloud_backend_manager", return_value=Mock()),
        patch("auto_coder.cli_helpers.create_high_score_cloud_backend_manager", return_value=Mock()),
        patch("time.sleep"),
    ):
        launched = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, jules_mode=route == "ordinary-cloud")
    assert launched.success is True
    owner = ImplementationOwner("issue", 1728)
    assert slots.active_owners() == (owner,)

    current["body"] = BODY + " B"
    deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    assert analyzed == [BODY + " A"]
    assert slots.start_execution(ImplementationOwner("issue", 99)) is None


def test_daemon_replacement_pr_preserves_capacity_across_later_edit(monkeypatch, tmp_path):
    """A real Jules launch and daemon fallback retain the replacement PR owner."""
    from auto_coder.issue_processor import _create_pr_for_issue, _process_issue_jules_mode

    monkeypatch.setenv("HOME", str(tmp_path))
    current = {"body": BODY + " A"}
    github = Mock(token="token")
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "title": "Daemon PR",
        "body": current["body"],
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_item_type_strict.return_value = "issue"
    github.get_all_sub_issues.return_value = []
    github.get_parent_issue_details.return_value = None
    github.get_open_sub_issues.return_value = []
    github.get_direct_sub_issues_strict.return_value = []
    github.get_parent_issue_details_strict.return_value = None
    github.get_issue.return_value = {"number": 1728, "state": "open"}
    github.get_issue_details.return_value = {"number": 1728, "state": "open"}
    github.has_linked_pr.return_value = False
    github.find_pr_by_head_branch.return_value = None
    github.get_pr_closing_issues.return_value = [1728]
    github.get_labels.return_value = []
    github.try_add_labels.return_value = True
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "daemon-pr-slots.json")
    engine.implementation_slots = slots
    analyzed = []
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle(
        "owner/repo",
        "validator",
        tmp_path / "daemon-pr-validations.json",
        lambda _manifest, body: analyzed.append(body) or SpecificationAnalysisResult("READY"),
    )
    launch_jules = Mock()
    launch_jules.start_session.return_value = "daemon-session-a"

    def production_launch(repo, issue_data, config, client, label_context=None, implementation_slots=None):
        assert implementation_slots is slots
        return _process_issue_jules_mode(repo, issue_data, config, client, label_context)

    candidate = Candidate(type="issue", data={"number": 1728}, priority=0)
    with (
        patch("auto_coder.issue_processor._process_issue_cloud_backend", side_effect=production_launch),
        patch("auto_coder.issue_processor.JulesClient", return_value=launch_jules),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        assert engine._process_single_candidate_unified("owner/repo", candidate, engine.config, jules_mode=True).success is True
    owner = ImplementationOwner("issue", 1728)
    assert slots.has_provider_sessions(owner) is True

    current["body"] = BODY + " B"
    stale_jules = Mock()
    stale_jules.list_sessions.return_value = [
        {
            "name": "sessions/daemon-session-a",
            "state": "IN_PROGRESS",
            "createTime": "2000-01-01T00:00:00Z",
            "outputs": {},
        }
    ]
    stale_jules.get_session.return_value = {"state": "COMPLETED"}
    api = Mock()
    api.pulls.create.return_value = {"number": 100, "html_url": "https://github.test/pull/100"}

    def replacement_actions(repo, issue_data, config, client, **kwargs):
        assert kwargs["implementation_slots"] is slots
        return [
            _create_pr_for_issue(
                repo,
                issue_data,
                "issue-1728-attempt-2",
                "main",
                "replacement implemented",
                client,
                config,
                implementation_slots=kwargs["implementation_slots"],
            )
        ]

    with (
        patch("auto_coder.issue_processor.JulesClient", return_value=stale_jules),
        patch("auto_coder.issue_processor.is_session_stopped", return_value=False),
        patch("auto_coder.issue_processor.increment_attempt", return_value=2),
        patch("auto_coder.issue_processor._apply_issue_actions_directly", side_effect=replacement_actions),
        patch("auto_coder.issue_processor.get_ghapi_client", return_value=api),
        patch("auto_coder.issue_processor.run_llm_noedit_prompt", return_value=""),
        patch("auto_coder.issue_processor.validate_issue_references"),
        patch("auto_coder.cli_helpers.create_high_score_backend_manager", return_value=Mock()),
        patch("time.sleep"),
    ):
        engine.handle_stale_jules_issue_sessions("owner/repo")
    assert analyzed == [BODY + " A", BODY + " B"]
    assert slots.has_provider_sessions(owner) is False
    assert slots.active_execution_ids(owner) == ()
    assert slots.active_owners() == (owner,)

    current["body"] = BODY + " C"
    deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    assert analyzed == [BODY + " A", BODY + " B"]
    assert slots.start_execution(ImplementationOwner("issue", 99)) is None


@pytest.mark.parametrize("child_ready", [True, False])
@pytest.mark.parametrize("failed_target", [None, 1728, 1727])
def test_inherited_blocked_preserves_parent_with_restart_retry(tmp_path, child_ready, failed_target):
    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    ready = {1727: True, 1728: child_ready}
    removals = []

    class FamilyGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return snapshot(ready=ready[number])

        def remove_labels(self, repo, number, labels, item_type="issue"):
            assert (repo, labels, item_type) == ("owner/repo", ["implementation-ready"], "issue")
            if number == failed_target:
                raise RuntimeError("label API unavailable")
            removals.append(number)
            ready[number] = False

    github = FamilyGitHub([snapshot()])
    error = gate.apply_inherited_blocked(github, decision, lambda: ready[1727])
    failed = failed_target == 1728 and child_ready
    assert error == ("readiness withdrawal failed: label API unavailable" if failed else None)
    assert gate.store.get(decision.identity).readiness_removed is (not failed)
    if failed:
        assert ready[1727] is True
        failed_target = None
        restarted = lifecycle(tmp_path, "BLOCKED", Mock(side_effect=AssertionError("must reuse")))
        assert restarted.apply_inherited_blocked(github, decision, lambda: ready[1727]) is None
        assert restarted.store.get(decision.identity).readiness_removed is True
    assert ready == {1727: True, 1728: False}
    assert removals == ([1728] if child_ready else [])
    assert len(github.comments) == 1


def test_inherited_blocked_edit_after_comment_preserves_both_labels(tmp_path):
    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)

    class EditingGitHub(GitHubFlow):
        def publish_issue_review_comment(self, repo, number, body, authorize_fn):
            receipt = super().publish_issue_review_comment(repo, number, body, authorize_fn)
            self.last = snapshot(body=BODY + " edited")
            return receipt

    github = EditingGitHub([snapshot()])
    assert gate.apply_inherited_blocked(github, decision, lambda: True) is None
    assert github.removals == 0
    assert len(github.comments) == 1
    assert gate.store.get(decision.identity).readiness_removed is False


@pytest.mark.parametrize("explicit_only,force,retry", [(True, True, True), (True, True, False), (True, False, True), (False, True, True)])
def test_manual_retry_retained_provider_admission_and_trace(tmp_path, explicit_only, force, retry):
    github = GitHubFlow([snapshot()])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    slots = engine.implementation_slots
    owner = ImplementationOwner("issue", 1728)
    # Seed retained evidence the way a real prior admission would have bound
    # it (#2061), so this manual retry is recognized as a continuation of
    # the same Implementation generation rather than failing closed on an
    # unrecognized (legacy-shaped) binding.
    generation = engine._compute_implementation_generation("owner/repo", snapshot(), None)
    execution = slots.start_execution(owner, generation=generation)
    assert slots.record_provider_session(owner, "old-session")
    slots.finish_execution(owner, execution)
    TraceCollector._instance = None

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, explicit_only=explicit_only, force=force, retry=retry)

    authorized = explicit_only and force and retry
    if authorized:
        engine._process_single_candidate_reserved.assert_called_once()
        call = engine._process_single_candidate_reserved.call_args
        assert call.args == ("owner/repo", candidate, engine.config, False)
        assert call.kwargs["manual_retry"] is True
        authority = call.kwargs["retry_authority"]
        assert authority.target_number == 1728
        assert authority.generation == generation
        assert authority.status == "owned"
        assert result.actions[0].startswith("Retry accepted for issue #1728: request=")
        assert result.actions[1] == "dispatched"
    else:
        engine._process_single_candidate_reserved.assert_not_called()
        assert result.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
    events = get_trace_collector().get_snapshot(item_type="issue", item_number=1728).events
    retry_events = [event for event in events if event.stage_id == "issue.manual-retry"]
    assert len(retry_events) == int(authorized)
    if authorized:
        assert retry_events[0].outcome == Outcome.COMPLETED.value
    assert slots.snapshot().owners[0].provider_sessions == ("old-session",)
    assert slots.active_execution_ids(owner) == ()


@pytest.mark.parametrize("ready,active_execution", [(False, False), (True, True)])
def test_manual_retry_preserves_readiness_and_live_dispatch_gates(tmp_path, ready, active_execution):
    engine, candidate = engine_with_gate(tmp_path, GitHubFlow([snapshot(ready=ready)]), lifecycle(tmp_path, "READY"))
    slots = engine.implementation_slots
    owner = ImplementationOwner("issue", 1728)
    execution = slots.start_execution(owner)
    assert slots.record_provider_session(owner, "old-session")
    if not active_execution:
        slots.finish_execution(owner, execution)
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config, explicit_only=True, force=True, retry=True)
    engine._process_single_candidate_reserved.assert_not_called()
    assert result.actions == (["Deferred - implementation ownership already exists (issue:1728)"] if active_execution else ["Skipped - missing implementation-ready label"])
    assert slots.snapshot().owners[0].provider_sessions == ("old-session",)


def test_paused_episode_exact_reversion_and_new_episode_are_durable(tmp_path):
    """AS-001/005/006/010: only a novel generation receives a new allowance."""
    from auto_coder.specification_repair_rounds import SpecificationRepairRoundStore

    path = tmp_path / "rounds.json"
    rounds = SpecificationRepairRoundStore("owner/repo", path)
    for generation in ("g1", "g2", "g3"):
        applied = rounds.authorize("individual", 7, generation, "EDIT_IN_PLACE", 3)
        assert applied.automatic_repair_authorized
        assert not applied.paused

    trigger = rounds.apply("individual", 7, "g4", "EDIT_IN_PLACE", 3)
    assert trigger.remediation == "EDIT_IN_PLACE"
    assert trigger.previous_rounds == 3
    assert trigger.paused and not trigger.automatic_repair_authorized
    assert rounds.count("individual", 7) == 3

    restarted = SpecificationRepairRoundStore("owner/repo", path)
    reverted = restarted.apply("individual", 7, "g2", "EDIT_IN_PLACE", 9)
    assert reverted.episode == 1
    assert reverted.paused and not reverted.automatic_repair_authorized
    assert reverted.previous_rounds == 3

    fresh = restarted.authorize("individual", 7, "g5", "EDIT_IN_PLACE", 3)
    assert fresh.episode == 2
    assert fresh.automatic_repair_authorized and not fresh.paused
    assert restarted.count("individual", 7, episode=2) == 1

    reverted_again = restarted.apply("individual", 7, "g2", "EDIT_IN_PLACE", 3)
    assert reverted_again.episode == 1 and reverted_again.paused
    assert not reverted_again.automatic_repair_authorized


def test_semantic_reissue_is_not_changed_by_repair_episode_budget(tmp_path):
    from auto_coder.specification_repair_rounds import SpecificationRepairRoundStore

    rounds = SpecificationRepairRoundStore("owner/repo", tmp_path / "rounds.json")
    rounds.authorize("decomposition", 8, "g1", "EDIT_IN_PLACE", 1)
    paused = rounds.apply("decomposition", 8, "g2", "EDIT_IN_PLACE", 1)
    assert paused.paused and paused.remediation == "EDIT_IN_PLACE"
    semantic = rounds.apply("decomposition", 8, "g2", "REISSUE_REQUIRED", 1)
    assert semantic.remediation == "REISSUE_REQUIRED"
    assert semantic.previous_rounds == 1


# ---------------------------------------------------------------------------
# Issue #2081: routing-independent policy identity, execution provenance, and
# legacy on-disk migration handling.
# ---------------------------------------------------------------------------


def test_execution_provenance_captured_for_fresh_model_decision_and_preserved_on_reuse(tmp_path):
    """REQ-005: provenance reflects the route effective when the analyzer starts, and reuse never relabels it."""
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    with patch("auto_coder.specification_validation_lifecycle.configured_provider_identity", return_value="route-a"):
        decision = lifecycle(tmp_path, "READY").decide(manifest, "Title", BODY)
    assert decision.evaluation_source == "model"
    assert decision.execution_provenance == "route-a"

    with patch("auto_coder.specification_validation_lifecycle.configured_provider_identity", return_value="route-b"):
        reused = lifecycle(tmp_path, "READY", Mock(side_effect=AssertionError("must reuse"))).decide(manifest, "Title", BODY)
    assert reused.evaluation_source == "stored-decision-reuse"
    assert reused.execution_provenance == "route-a"

    # A restart (a brand-new lifecycle/store instance over the same file)
    # must still return the original producing provenance verbatim.
    restarted = SpecificationValidationLifecycle("owner/repo", "route-c", tmp_path / "decisions.json", Mock(side_effect=AssertionError("must reuse")))
    assert restarted.decide(manifest, "Title", BODY).execution_provenance == "route-a"


def test_local_only_objective_conflict_never_calls_configured_provider_identity(tmp_path):
    """REQ-005: a local-only decision (no analyzer call) records no provenance and never probes routing."""
    body_with_objective = "## Objective\n\nShip the widget.\n\n## Requirements\n- REQ-001: Return the current value."
    manifest = build_normative_issue_manifest(1728, "Title", body_with_objective)
    gate = lifecycle(tmp_path, "READY")
    first = gate.decide(manifest, "Title", body_with_objective)
    assert first.verdict == "READY"
    assert first.execution_provenance is not None

    edited_objective_body = body_with_objective.replace("Ship the widget.", "Ship something else entirely.")
    edited_manifest = build_normative_issue_manifest(1728, "Title", edited_objective_body)
    with patch("auto_coder.specification_validation_lifecycle.configured_provider_identity", side_effect=AssertionError("must not be called for a local-only decision")):
        conflict = gate.decide(edited_manifest, "Title", edited_objective_body)
    assert conflict.verdict == "BLOCKED"
    assert conflict.evaluation_source == "local-only"
    assert conflict.execution_provenance is None


def test_ready_and_blocked_decisions_both_reuse_across_route_change(tmp_path):
    """AS-002: READY and BLOCKED both reuse across a route-only change without a new backend call."""
    ready_manifest = build_normative_issue_manifest(1728, "Ready Title", BODY)
    blocked_body = BODY + "\nBlocked variant."
    blocked_manifest = build_normative_issue_manifest(1729, "Blocked Title", blocked_body)
    blocked_result = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")

    gate_a = lifecycle(tmp_path, "READY", Mock(return_value=SpecificationAnalysisResult("READY")), policy="route-a")
    ready = gate_a.decide(ready_manifest, "Ready Title", BODY)
    gate_a_blocked = lifecycle(tmp_path, "BLOCKED", Mock(return_value=blocked_result), policy="route-a")
    blocked = gate_a_blocked.decide(blocked_manifest, "Blocked Title", blocked_body)
    assert ready.verdict == "READY" and blocked.verdict == "BLOCKED"

    never_call = Mock(side_effect=AssertionError("must reuse"))
    gate_b = lifecycle(tmp_path, "READY", never_call, policy="route-b")
    assert gate_b.decide(ready_manifest, "Ready Title", BODY).verdict == "READY"
    assert gate_b.decide(blocked_manifest, "Blocked Title", blocked_body).verdict == "BLOCKED"
    never_call.assert_not_called()


def test_in_flight_route_change_is_not_a_rerun(tmp_path):
    """AS-004: a route change while an analyzer call is in flight causes no second execution."""
    entered = Barrier(2)
    release = Barrier(2)
    calls = []
    route = {"value": "provider/model-a"}

    def analyze(_manifest, _body):
        calls.append(route["value"])
        if len(calls) == 1:
            entered.wait(timeout=5)
            release.wait(timeout=5)
        return SpecificationAnalysisResult("READY")

    gate = lifecycle(tmp_path, "READY", analyze)
    manifest = build_normative_issue_manifest(1728, "Title", BODY)

    with patch("auto_coder.specification_validation_lifecycle.configured_provider_identity", lambda: route["value"]):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(gate.decide, manifest, "Title", BODY)
            entered.wait(timeout=5)
            route["value"] = "provider/model-b"
            second = pool.submit(gate.decide, manifest, "Title", BODY)
            release.wait(timeout=5)
            decision_1 = first.result(timeout=5)
            decision_2 = second.result(timeout=5)

    # The route changed mid-flight, but only one analyzer call happened, and
    # it observed the route effective when it actually started (REQ-001,
    # REQ-004, REQ-008).
    assert calls == ["provider/model-a"]
    assert decision_1.verdict == "READY" and decision_2.verdict == "READY"
    assert decision_1.identity == decision_2.identity
    assert decision_1.execution_provenance == "provider/model-a"
    assert decision_2.execution_provenance == "provider/model-a"

    # A later, genuinely new review (a different identity) uses the new route.
    edited_body = BODY + "\nEdited."
    edited_manifest = build_normative_issue_manifest(1728, "Title", edited_body)
    with patch("auto_coder.specification_validation_lifecycle.configured_provider_identity", lambda: route["value"]):
        fresh = gate.decide(edited_manifest, "Title", edited_body)
    assert fresh.execution_provenance == "provider/model-b"


def test_legacy_terminal_record_is_retained_but_not_authoritative(tmp_path):
    """AS-005/REQ-006/REQ-007/REQ-009: a pre-migration record is retained and diagnosed, never silently reused.

    Hand-constructs the exact pre-migration on-disk shape (an opaque combined
    policy hash that mixed provider routing into the same fields this
    Issue's contract fixes, with no ``execution_provenance`` key at all) and
    drives the real ``decide()`` production path over it.
    """
    path = tmp_path / "decisions.json"
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    probe = SpecificationValidationLifecycle("owner/repo", "provider/model", path)
    identity = probe.identity(1728, "Title", BODY)

    legacy_policy_identity = hashlib.sha256(b"legacy-combined-provider-and-prompt-hash").hexdigest()
    legacy_identity = {
        "repository": identity.repository,
        "issue_number": identity.issue_number,
        "specification_digest": identity.specification_digest,
        "policy_identity": legacy_policy_identity,
        "relationship_digest": identity.relationship_digest,
    }
    legacy_key = hashlib.sha256(json.dumps(legacy_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                legacy_key: {
                    "identity": legacy_identity,
                    "verdict": "READY",
                    "findings": [],
                    "findings_published": False,
                    "readiness_removed": False,
                    "remediation": "NONE",
                    "remediation_reason": None,
                }
            }
        ),
        encoding="utf-8",
    )

    # Durable state this migration must never touch (REQ-007): a pre-existing
    # immutable baseline plus applied-outcome history for this Issue number.
    history_path = path.with_name("individual_review_history.json")
    original_contract = json.dumps({"issue_number": 1728, "title": "Original", "body": "## Requirements\n- REQ-001: Original.", "requirements": []})
    history_path.write_text(json.dumps({"1728": {"baseline": original_contract, "applied_outcomes": ["legacy-outcome"], "applied_identity_keys": ["legacy-key"]}}), encoding="utf-8")

    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    live_gate = SpecificationValidationLifecycle("owner/repo", "provider/model", path, calls)
    with patch("auto_coder.specification_validation_lifecycle.logger") as mock_logger:
        fresh = live_gate.decide(manifest, "Title", BODY)

    # A legacy-compatibility miss still runs a genuine, currently authorized
    # review (REQ-006): it is never shortcut into BLOCKED or READY, and never
    # skips the analyzer.
    assert fresh.verdict == "READY"
    assert calls.call_count == 1
    assert fresh.identity.key != legacy_key

    # The distinct diagnostic signal fires exactly once, before the fresh
    # decision, and reports the true legacy-candidate count (REQ-009).
    assert fresh.legacy_candidates_detected == 1
    assert mock_logger.warning.call_count == 1
    assert "legacy_policy_unproven" in mock_logger.warning.call_args.args[0]

    # The legacy record is retained exactly as-is: never deleted, overwritten,
    # or "promoted" into the new format (REQ-006).
    raw = json.loads(path.read_text())
    assert raw[legacy_key]["verdict"] == "READY"
    assert raw[legacy_key]["identity"]["policy_identity"] == legacy_policy_identity

    # Pre-existing baseline/history for this Issue number is untouched by the
    # migration (REQ-007); ``decide()`` alone (no ``apply_blocked``) does not
    # append a new applied outcome either.
    history_after = json.loads(history_path.read_text())
    assert history_after["1728"]["baseline"] == original_contract
    assert history_after["1728"]["applied_outcomes"] == ["legacy-outcome"]

    # A cache miss with zero legacy candidates is a distinguishable signal
    # from this legacy-compatibility miss (REQ-009).
    other_manifest = build_normative_issue_manifest(1730, "Other", BODY)
    with patch("auto_coder.specification_validation_lifecycle.logger") as ordinary_logger:
        ordinary = live_gate.decide(other_manifest, "Other", BODY)
    assert ordinary.legacy_candidates_detected == 0
    ordinary_logger.warning.assert_not_called()

    # After a valid new-format evaluation is persisted, further route changes
    # and restarts reuse it rather than repeating the migration miss.
    with patch("auto_coder.specification_validation_lifecycle.logger") as reuse_logger:
        restarted = SpecificationValidationLifecycle("owner/repo", "provider/model-b", path, Mock(side_effect=AssertionError("must reuse")))
        reused = restarted.decide(manifest, "Title", BODY)
    assert reused.verdict == "READY"
    assert reused.evaluation_source == "stored-decision-reuse"
    reuse_logger.warning.assert_not_called()


def test_conflicting_legacy_candidates_are_reported_without_preferring_ready(tmp_path):
    """REQ-006: multiple conflicting legacy terminal records are reported as a count, not resolved by preferring READY."""
    path = tmp_path / "decisions.json"
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    probe = SpecificationValidationLifecycle("owner/repo", "provider/model", path)
    identity = probe.identity(1728, "Title", BODY)

    def legacy_record(policy_suffix: str, verdict: str) -> tuple[str, dict]:
        legacy_identity = {
            "repository": identity.repository,
            "issue_number": identity.issue_number,
            "specification_digest": identity.specification_digest,
            "policy_identity": hashlib.sha256(f"legacy-{policy_suffix}".encode()).hexdigest(),
            "relationship_digest": identity.relationship_digest,
        }
        key = hashlib.sha256(json.dumps(legacy_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return key, {"identity": legacy_identity, "verdict": verdict, "findings": [], "findings_published": False, "readiness_removed": False, "remediation": "NONE", "remediation_reason": None}

    ready_key, ready_record = legacy_record("a", "READY")
    blocked_key, blocked_record = legacy_record("b", "BLOCKED")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({ready_key: ready_record, blocked_key: blocked_record}), encoding="utf-8")

    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    gate = SpecificationValidationLifecycle("owner/repo", "provider/model", path, calls)
    with patch("auto_coder.specification_validation_lifecycle.logger") as mock_logger:
        fresh = gate.decide(manifest, "Title", BODY)
    assert fresh.verdict == "READY"
    assert fresh.legacy_candidates_detected == 2
    assert calls.call_count == 1
    assert mock_logger.warning.call_args.kwargs == {} and "2" in str(mock_logger.warning.call_args)


def test_validator_identity_override_env_var_is_provenance_only(tmp_path, monkeypatch):
    """AS-006/REQ-001: ``AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY`` is execution provenance,
    never a hidden cache-invalidation input for the semantic policy identity."""
    manifest = build_normative_issue_manifest(1728, "Title", BODY)

    monkeypatch.setenv("AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY", "override-a")
    first_identity = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "decisions.json").identity(1728, "Title", BODY)
    decision = lifecycle(tmp_path, "READY").decide(manifest, "Title", BODY)
    assert decision.execution_provenance == "override-a"

    monkeypatch.setenv("AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY", "override-b")
    second_identity = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "decisions.json").identity(1728, "Title", BODY)
    assert second_identity == first_identity

    reused = lifecycle(tmp_path, "READY", Mock(side_effect=AssertionError("must reuse"))).decide(manifest, "Title", BODY)
    assert reused.verdict == "READY"
    # The overridden route is honest execution provenance for a fresh
    # decision, but reusing the earlier decision preserves its original
    # provenance rather than relabeling it with the current override.
    assert reused.execution_provenance == "override-a"


def test_legacy_findings_published_record_is_never_reposted_or_relabeled(tmp_path):
    """Issue #2026 REQ-008: a pre-App-routing findings_published record stays trusted-complete."""
    from dataclasses import replace

    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    legacy = replace(decision, findings_published=True, publication_schema_version=0, publication_receipt=None)
    gate.store.save(legacy)

    github = GitHubFlow([snapshot()])
    assert gate.apply_blocked(github, legacy) is None
    assert github.comments == [], "legacy completion must never trigger a repost"
    saved = gate.store.get(decision.identity)
    assert saved.findings_published is True
    assert saved.publication_schema_version == 0, "legacy record must never be relabeled as proven App-authored"
    assert saved.publication_receipt is None


def test_post_change_record_with_missing_receipt_is_not_silently_grandfathered(tmp_path):
    """Issue #2026 REQ-008: a tagged post-change record without its receipt is re-verified, not trusted."""
    from dataclasses import replace

    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    corrupt = replace(decision, findings_published=True, publication_schema_version=1, publication_receipt=None)
    gate.store.save(corrupt)

    github = GitHubFlow([snapshot()])
    assert gate.apply_blocked(github, corrupt) is None
    assert len(github.comments) == 1, "missing receipt must trigger re-verification and (re)publication"
    saved = gate.store.get(decision.identity)
    assert saved.publication_receipt is not None
    assert saved.publication_receipt["comment_id"] == github.comments[0]["id"]


def test_new_decision_is_stamped_before_first_send_and_receipt_persists(tmp_path):
    """Issue #2026 REQ-008: versioned publication ownership is initialized before the first send attempt."""
    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    assert decision.publication_schema_version == 0
    assert decision.publication_receipt is None

    github = GitHubFlow([snapshot()])
    assert gate.apply_blocked(github, decision) is None
    saved = gate.store.get(decision.identity)
    assert saved.publication_schema_version == 1
    assert saved.publication_receipt == {"comment_id": 1, "publisher_login": REVIEWER_LOGIN, "publisher_app_id": REVIEWER_APP_ID}


def test_repair_round_policy_reconstruction_preserves_publication_receipt(tmp_path):
    """Issue #2026 REQ-003/REQ-008: a repair-round reason change must never drop an already-confirmed receipt.

    Found by adversarial review: ``_apply_repair_round_policy`` rebuilt the
    decision without copying ``publication_schema_version``/
    ``publication_receipt`` (or ``evaluation_source``) whenever the repair
    round policy produced a different remediation/reason, silently
    downgrading an App-confirmed record to indistinguishable-from-legacy.
    """
    from dataclasses import replace
    from unittest.mock import patch as mock_patch

    from auto_coder.specification_repair_rounds import RepairRoundApplication

    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    receipt = {"comment_id": 42, "publisher_login": REVIEWER_LOGIN, "publisher_app_id": REVIEWER_APP_ID}
    confirmed = replace(decision, findings_published=True, publication_schema_version=1, publication_receipt=receipt)
    gate.store.save(confirmed)

    # Simulate the repair-round policy newly reaching its pause limit for
    # this exact generation, exactly as a real budget exhaustion would,
    # without needing to choreograph the full multi-generation sequence.
    with mock_patch.object(gate.repair_rounds, "apply", return_value=RepairRoundApplication("EDIT_IN_PLACE", 3, reason="automatic_repair_paused(repair_round_limit_reached)", paused=True)):
        updated = gate._apply_repair_round_policy(confirmed)

    assert updated.remediation_reason == "automatic_repair_paused(repair_round_limit_reached)"
    assert updated.publication_schema_version == 1
    assert updated.publication_receipt == receipt
    assert updated.findings_published is True
    saved = gate.store.get(decision.identity)
    assert saved.publication_schema_version == 1
    assert saved.publication_receipt == receipt


def test_existing_comment_with_wrong_author_is_an_unconfirmed_conflict_not_a_repost(tmp_path):
    """Issue #2026 REQ-005: a marker-bearing comment from another actor must not be silently accepted or reposted."""
    gate = lifecycle(tmp_path, "BLOCKED")
    decision = gate.decide(build_normative_issue_manifest(1728, "Title", BODY), "Title", BODY)
    github = GitHubFlow([snapshot()])
    github.comments.append({"id": 1, "body": gate.findings_comment(decision), "user": {"login": "human-imitator"}})

    error = gate.apply_blocked(github, decision)

    assert error is not None and "conflict" in error
    assert len(github.comments) == 1, "must never repost over an unresolved conflict"
    saved = gate.store.get(decision.identity)
    assert saved.findings_published is False
