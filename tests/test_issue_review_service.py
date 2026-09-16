"""Production-boundary regressions for the Issue Review worker lane (Issue #2054).

These tests cross the real engine admission/routing boundary, the real
specification/decomposition lifecycles (durable stores, evidence authority,
repair episodes, reissue stops), and the real Review worker lane, with only
the model transport faked. They cover REQ-011 slices mapping to AS-001,
AS-002, AS-004, AS-005, AS-006, AS-007, AS-008, AS-010, AS-011, and AS-013.
"""

from pathlib import Path
from unittest.mock import Mock

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_stage_routing import REVIEW_STAGE
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

REPO = "owner/repo"
BODY = "## Objective\n\nShip the widget.\n\n## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")
REVIEWER_LOGIN = "auto-coder-reviewer[bot]"
REVIEWER_APP_ID = 990001


def ready_body(extra: str = "") -> str:
    return BODY + extra


class FakeGitHub:
    """Dict-backed authoritative GitHub double with effect tracking."""

    def __init__(self, issues, parents=None):
        self.issues = {int(snap["number"]): dict(snap) for snap in issues}
        self.parents = dict(parents or {})
        self.comments: list[tuple[int, str]] = []
        self.removals: list[tuple[int, tuple[str, ...]]] = []

    def snapshot(self, number, *, ready=True, state="open", body=None, labels=None, title="Title"):
        snap = {"number": number, "id": number * 10, "title": title, "body": BODY if body is None else body, "state": state}
        names = list(labels) if labels is not None else (["implementation-ready"] if ready else [])
        snap["labels"] = [{"name": name} for name in names]
        if number in self.parents:
            snap["parent_issue_number"] = self.parents[number]
        return snap

    def get_issue_dispatch_snapshot_strict(self, _repo, number):
        return dict(self.issues[number])

    def get_direct_sub_issues_strict(self, _repo, number):
        children = [n for n, p in self.parents.items() if p == number]
        return [dict(self.issues[n]) for n in sorted(children)]

    def get_parent_issue_details_strict(self, _repo, number):
        parent = self.parents.get(number)
        return {"number": parent} if parent is not None else None

    def get_issue_comments_strict(self, _repo, number):
        return [{"id": index + 1, "body": body, "user": {"login": REVIEWER_LOGIN}, "performed_via_github_app": {"id": REVIEWER_APP_ID}} for index, (n, body) in enumerate(self.comments) if n == number]

    def add_comment_to_issue(self, _repo, number, body):
        self.comments.append((number, body))

    def publish_issue_review_comment(self, _repo, number, body, authorize_fn):
        from auto_coder.issue_review_publication import PublicationReceipt

        comment_id = len(self.comments) + 1
        self.comments.append((number, body))
        return PublicationReceipt(comment_id, REVIEWER_LOGIN, REVIEWER_APP_ID)

    def reviewer_app_identity(self, _repo):
        from auto_coder.github_app_reviewer import ReviewerAppIdentity

        return ReviewerAppIdentity(login=REVIEWER_LOGIN, app_id=REVIEWER_APP_ID)

    def remove_labels(self, _repo, number, labels, item_type="issue"):
        assert item_type == "issue"
        self.removals.append((number, tuple(labels)))
        current = self.issues[number]
        removed = set(labels)
        current["labels"] = [entry for entry in current["labels"] if entry["name"] not in removed]


def spec_lifecycle(tmp_path: Path, analyzer, name="spec.json", policy="provider/model"):
    return SpecificationValidationLifecycle(REPO, policy, tmp_path / name, analyzer)


def decomp_lifecycle(tmp_path: Path, analyzer, name="decomp.json", policy="provider/model"):
    return DecompositionValidationLifecycle(REPO, policy, tmp_path / name, analyzer)


def engine_with_lane(tmp_path: Path, github, spec_analyzer, decomp_analyzer=None, monkeypatch=None):
    from auto_coder.github_pending_work import PendingWorkStore

    store = PendingWorkStore(tmp_path / "pending-work.sqlite3")
    if monkeypatch is not None:
        monkeypatch.setattr("auto_coder.specification_validation_lifecycle.get_pending_work_store", lambda: store)
        monkeypatch.setenv("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", str(tmp_path / "routing.sqlite3"))
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    config = AutomationConfig(repo_name=REPO)
    engine = AutomationEngine(github, config=config)
    engine._specification_validators[REPO] = spec_lifecycle(tmp_path, spec_analyzer)
    engine._decomposition_validators[REPO] = decomp_lifecycle(tmp_path, decomp_analyzer or Mock(return_value=DecompositionAnalysisResult("READY")))
    return engine


def test_standalone_ready_pump_hands_off_and_reuses_without_new_backend_call(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert calls.call_count == 1
    assert engine.issue_stage_routing.pending(REPO, REVIEW_STAGE) == ()

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert calls.call_count == 1


def test_closed_issue_removes_review_work_without_invoking_analyzer(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    engine._route_issue_stages_authoritatively(REPO, 1, dict(github.issues[1]))
    assert len(engine.issue_stage_routing.pending(REPO, REVIEW_STAGE)) == 1

    github.issues[1] = github.snapshot(1, state="closed")
    outcomes = engine.pump_issue_review_lane(REPO, origin="test-origin")
    assert calls.call_count == 0
    assert engine.issue_stage_routing.pending(REPO, REVIEW_STAGE) == ()
    assert all(outcome.status == "stale" for outcome in outcomes)


def test_blocked_standalone_publishes_once_and_withdraws_readiness(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")
    calls = Mock(return_value=analysis)
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert len(github.comments) == 1 and github.comments[0][0] == 1
    assert github.removals == [(1, ("implementation-ready",))]

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert calls.call_count == 1
    assert len(github.comments) == 1
    assert len(github.removals) == 1


def test_child_blocked_withdraws_only_child_label(tmp_path, monkeypatch):
    github = FakeGitHub([], parents={2: 1})
    github.issues[1] = github.snapshot(1, title="Parent", body="## Objective\n\nCoordinate.")
    github.issues[2] = github.snapshot(2)
    calls = Mock(return_value=SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    outcome = engine._get_review_service(REPO).pump_target(2, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert [number for number, _ in github.comments] == [2]
    assert github.removals == [(2, ("implementation-ready",))]
    parent_labels = [entry["name"] for entry in github.issues[1]["labels"]]
    assert "implementation-ready" in parent_labels


def test_decomposition_blocked_targets_parent(tmp_path, monkeypatch):
    github = FakeGitHub([], parents={2: 1})
    github.issues[1] = github.snapshot(1, title="Parent", body="## Objective\n\nCoordinate.")
    github.issues[2] = github.snapshot(2)
    from auto_coder.decomposition_analyzer import AffectedIssue, DecompositionFinding

    finding = DecompositionFinding(
        "missing_requirement_ownership",
        (AffectedIssue(1, ()), AffectedIssue(2, ("REQ-001",))),
        "Nobody owns the parent outcome.",
        "Assign it.",
    )
    decomp_calls = Mock(return_value=DecompositionAnalysisResult("BLOCKED", (finding,), remediation="EDIT_IN_PLACE"))
    engine = engine_with_lane(tmp_path, github, Mock(return_value=SpecificationAnalysisResult("READY")), decomp_calls, monkeypatch=monkeypatch)

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert [number for number, _ in github.comments] == [1]
    assert (1, ("implementation-ready",)) in github.removals


def test_crash_before_wake_recovers_without_new_review(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    service = engine._get_review_service(REPO)

    reconciled = engine._reconcile_review_target(REPO, 1)
    assert reconciled is not None
    original = reconciled.reevaluate
    state = {"failed": False}

    def flaky():
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("crash before wake")
        return original() if original else None

    object.__setattr__(reconciled, "reevaluate", flaky)

    real_reconcile = service._reconcile
    service._reconcile = lambda _number, _snapshot=None: reconciled  # noqa: E731
    try:
        outcome = service.pump_target(1, "test-origin")
    except RuntimeError:
        outcome = None
    finally:
        service._reconcile = real_reconcile
    assert outcome is None
    assert calls.call_count == 1

    engine.issue_stage_routing.recover(REPO)
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert calls.call_count == 1


def test_error_decision_is_retryable_and_persists_nothing_terminal(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(side_effect=[SpecificationAnalysisResult("ERROR", error="outage"), SpecificationAnalysisResult("READY")])
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "error" and not outcome.handed_off
    assert github.comments == [] and github.removals == []

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert calls.call_count == 2


def test_priority_orders_lane_and_equal_priority_uses_arrival(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1, labels=["implementation-ready"])
    github.issues[2] = github.snapshot(2, labels=["implementation-ready", "urgent"])
    github.issues[3] = github.snapshot(3, labels=["implementation-ready", "breaking-change"])
    order = []
    engine = engine_with_lane(tmp_path, github, Mock(side_effect=lambda manifest, _body: order.append(manifest.issue_number) or SpecificationAnalysisResult("READY")), monkeypatch=monkeypatch)
    for number in (1, 2, 3):
        engine._route_issue_stages_authoritatively(REPO, number, dict(github.issues[number]))

    outcomes = engine.pump_issue_review_lane(REPO, origin="test-origin", max_items=8)
    assert [outcome.target_number for outcome in outcomes] == [3, 2, 1]
    assert order == [3, 2, 1]


def test_review_progress_with_implementation_slots_full(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    github.issues[2] = github.snapshot(2)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    engine.implementation_slots = ImplementationSlotRepository(REPO, 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 999)
    assert engine.implementation_slots.reserve(owner) is True
    assert owner in engine.implementation_slots.active_owners()
    for number in (1, 2):
        engine._route_issue_stages_authoritatively(REPO, number, dict(github.issues[number]))

    outcomes = engine.pump_issue_review_lane(REPO, origin="test-origin", max_items=8)
    assert [outcome.target_number for outcome in outcomes] == [1, 2]
    assert all(outcome.handed_off for outcome in outcomes)
    assert engine.implementation_slots.active_owners() == (owner,)


def test_implementation_gate_submits_no_new_review_job(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    engine._submit_individual_validation = Mock(side_effect=AssertionError("implementation path must not submit review jobs"))
    engine._schedule_parent_validations = Mock(side_effect=AssertionError("implementation path must not schedule review jobs"))

    decision, _ = engine._review_individual_via_lane(REPO, 1, "Title", BODY, None, "normal-worker-processing")
    assert decision is not None and decision.verdict == "READY"
    assert calls.call_count == 1


def test_disabled_category_creates_no_work_and_reenable_reuses(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    object.__setattr__(engine.config, "issue_specification_validation", False)

    assert engine._get_review_service(REPO).pump_target(1, "test-origin") is None
    assert calls.call_count == 0

    object.__setattr__(engine.config, "issue_specification_validation", True)
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    assert calls.call_count == 1

    object.__setattr__(engine.config, "issue_specification_validation", False)
    object.__setattr__(engine.config, "issue_specification_validation", True)
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    assert calls.call_count == 1


def test_provider_only_change_reuses_decision_without_new_backend_call(tmp_path, monkeypatch):
    """Issue #2081, REQ-001/REQ-004: an execution-routing-only change never triggers a new review."""
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    assert engine._get_review_service(REPO).pump_target(1, "test-origin") is not None
    assert calls.call_count == 1

    # A lifecycle constructed against a different execution route over the
    # same durable store must reuse the existing READY decision.
    engine._specification_validators[REPO] = spec_lifecycle(tmp_path, calls, policy="provider/model-b")
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    assert calls.call_count == 1

    engine._specification_validators[REPO] = spec_lifecycle(tmp_path, calls, policy="provider/model")
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    assert calls.call_count == 1


def test_anchored_objective_edit_becomes_objective_conflict(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    assert engine._get_review_service(REPO).pump_target(1, "test-origin") is not None
    assert calls.call_count == 1

    github.issues[1] = github.snapshot(1, body=BODY.replace("Ship the widget.", "Ship a different widget."))
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    decision = next(iter(outcome.decisions.values()))
    assert decision.verdict == "BLOCKED"
    assert [finding.category for finding in decision.findings] == ["objective_conflict"]
    assert calls.call_count == 1


def test_unavailable_evidence_prevents_unsafe_terminal_reuse(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    assert engine._get_review_service(REPO).pump_target(1, "test-origin") is not None
    assert calls.call_count == 1

    (tmp_path / "individual_review_history.json").write_text("{broken", encoding="utf-8")
    decision, _ = engine._review_individual_via_lane(REPO, 1, "Title", BODY, None, "test-origin")
    assert decision is None
    assert calls.call_count == 1


def test_production_specification_parser_classifies_responses(tmp_path):
    from auto_coder.requirement_contract import build_normative_issue_manifest
    from auto_coder.specification_analyzer import parse_specification_analysis_response

    manifest = build_normative_issue_manifest(1, "Title", BODY)
    assert manifest.error is None

    valid_ready = '{"verdict": "READY", "remediation": "NONE", "findings": []}'
    assert parse_specification_analysis_response(valid_ready, manifest).verdict == "READY"

    valid_blocked = '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": [{' '"category": "material_ambiguity", "requirement_ids": ["REQ-001"], ' '"explanation": "Undefined.", "clarification": "Define it.", ' '"counterexample": "", "missing_normative_boundary": ""}]}'
    blocked = parse_specification_analysis_response(valid_blocked, manifest)
    assert blocked.verdict == "BLOCKED" and blocked.remediation == "EDIT_IN_PLACE"

    cases = [
        '{"verdict": "READY", "remediation": "NONE", "findings": [], "findings": []}',
        '{"verdict": "MAYBE", "remediation": "NONE", "findings": []}',
        '{"verdict": "READY", "remediation": "NONE", "findings": [{"category": "material_ambiguity", "requirement_ids": ["REQ-001"], "explanation": "x", "clarification": "y", "counterexample": "", "missing_normative_boundary": ""}]}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": []}',
        '{"verdict": "BLOCKED", "remediation": "NONE", "findings": [{"category": "material_ambiguity", "requirement_ids": ["REQ-001"], "explanation": "x", "clarification": "y", "counterexample": "", "missing_normative_boundary": ""}]}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": [{"category": "unknown_category", "requirement_ids": ["REQ-001"], "explanation": "x", "clarification": "y", "counterexample": "", "missing_normative_boundary": ""}]}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": [{"category": "material_ambiguity", "requirement_ids": ["REQ-999"], "explanation": "x", "clarification": "y", "counterexample": "", "missing_normative_boundary": ""}]}',
        "not json at all",
    ]
    for raw in cases:
        assert parse_specification_analysis_response(raw, manifest).verdict == "ERROR", raw


def test_production_specification_analyzer_crosses_prompt_and_parser(tmp_path):
    from auto_coder.objective_evidence import ObjectiveAnchorStore
    from auto_coder.requirement_contract import build_normative_issue_manifest
    from auto_coder.specification_analyzer import analyze_issue_specification, individual_review_evidence
    from auto_coder.specification_validation_lifecycle import IndividualReviewHistoryStore

    manifest = build_normative_issue_manifest(1, "Title", BODY)
    history = IndividualReviewHistoryStore(REPO, tmp_path / "history.json")
    import json as _json

    from auto_coder.specification_analyzer import IndividualReviewEvidence

    contract = _json.dumps({"issue_number": 1, "title": "Title", "body": BODY, "requirements": [{"requirement_id": "REQ-001", "text": "Return the current value."}]})
    baseline = history.evidence(1, contract).baseline
    anchors = ObjectiveAnchorStore(REPO, tmp_path / "history.json")
    evidence = IndividualReviewEvidence(baseline, (), anchors.capture(1, BODY, "individual-current-snapshot:v1"))

    seen: list[str] = []

    def runner(prompt: str) -> str:
        seen.append(prompt)
        return '{"verdict": "READY", "remediation": "NONE", "findings": []}'

    with individual_review_evidence(evidence):
        result = analyze_issue_specification(manifest, BODY, prompt_runner=runner)
    assert result.verdict == "READY"
    assert len(seen) == 1 and "REQ-001" in seen[0]

    def bad_runner(_prompt: str) -> str:
        return '{"verdict": "READY", "remediation": "NONE", "findings": [{"category": "nope"}]}'

    with individual_review_evidence(evidence):
        malformed = analyze_issue_specification(manifest, BODY, prompt_runner=bad_runner)
    assert malformed.verdict == "ERROR"


def test_production_decomposition_parser_classifies_responses():
    from auto_coder.decomposition_analyzer import DecompositionIssue, parse_decomposition_analysis_response
    from auto_coder.requirement_contract import build_normative_issue_manifest

    parent = DecompositionIssue(build_normative_issue_manifest(1, "Parent", "## Objective\n\nCoordinate."), "## Objective\n\nCoordinate.")
    child = DecompositionIssue(build_normative_issue_manifest(2, "Child", BODY), BODY)
    assert parent.manifest.error is None and child.manifest.error is None

    valid = '{"verdict": "READY", "remediation": "NONE", "findings": []}'
    assert parse_decomposition_analysis_response(valid, parent, (child,)).verdict == "READY"

    cases = [
        '{"verdict": "READY", "remediation": "NONE", "findings": [{"category": "missing_requirement_ownership", "affected_issues": [{"issue_number": 1, "requirement_ids": []}], "explanation": "x", "clarification": "y"}]}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": []}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": [{"category": "nope", "affected_issues": [{"issue_number": 2, "requirement_ids": ["REQ-001"]}], "explanation": "x", "clarification": "y"}]}',
        '{"verdict": "BLOCKED", "remediation": "EDIT_IN_PLACE", "findings": [{"category": "cross_issue_contradiction", "affected_issues": [{"issue_number": 99, "requirement_ids": ["REQ-001"]}], "explanation": "x", "clarification": "y"}]}',
        "not json",
    ]
    for raw in cases:
        assert parse_decomposition_analysis_response(raw, parent, (child,)).verdict == "ERROR", raw


def test_restored_readiness_renews_withdrawal_without_new_diagnostic(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE")
    calls = Mock(return_value=analysis)
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)

    assert engine._get_review_service(REPO).pump_target(1, "test-origin") is not None
    assert len(github.comments) == 1 and len(github.removals) == 1

    github.issues[1]["labels"] = [{"name": "implementation-ready"}]
    decision, _ = engine._review_individual_via_lane(REPO, 1, "Title", BODY, None, "normal-worker-processing")
    assert decision is not None and decision.verdict == "BLOCKED"
    engine._authorize_and_apply_blocked(engine._get_specification_validator(REPO), github, decision, lambda: True)
    assert calls.call_count == 1
    assert len(github.comments) == 1
    assert len(github.removals) == 2


def test_repair_episode_pauses_without_fabricating_reissue(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    bodies = [BODY + f"\n\n<!-- round {round} -->" for round in range(4)]
    analyses = [SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE") for _ in bodies]
    calls = Mock(side_effect=list(analyses))
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    validator = engine._get_specification_validator(REPO)

    for round, body in enumerate(bodies[:3]):
        github.issues[1] = github.snapshot(1, body=body)
        outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
        assert outcome is not None and outcome.status == "completed", round
    assert calls.call_count == 3
    assert validator.repair_rounds.count("individual", 1) == 3

    github.issues[1] = github.snapshot(1, body=bodies[3])
    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed"
    decision = next(iter(outcome.decisions.values()))
    assert decision.remediation == "EDIT_IN_PLACE"
    assert validator.repair_rounds.is_paused("individual", 1, decision.identity.specification_digest) is True
    assert validator.is_reissue_required(1) is False


def test_genuine_reissue_stop_requires_current_authority(tmp_path, monkeypatch):
    github = FakeGitHub([])
    github.issues[1] = github.snapshot(1)
    analysis = SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="REISSUE_REQUIRED")
    calls = Mock(return_value=analysis)
    engine = engine_with_lane(tmp_path, github, calls, monkeypatch=monkeypatch)
    validator = engine._get_specification_validator(REPO)

    outcome = engine._get_review_service(REPO).pump_target(1, "test-origin")
    assert outcome is not None and outcome.status == "completed" and outcome.handed_off
    assert validator.is_reissue_required(1) is True
