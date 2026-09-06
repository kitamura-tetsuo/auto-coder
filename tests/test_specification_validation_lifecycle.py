"""Generation-bound Issue specification validation lifecycle regressions."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from unittest.mock import Mock, call, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue

BODY = "## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")


def lifecycle(tmp_path, verdict, analyzer=None, policy="provider/model-a"):
    result = SpecificationAnalysisResult(verdict, (FINDING,) if verdict == "BLOCKED" else ())
    return SpecificationValidationLifecycle("owner/repo", policy, tmp_path / "decisions.json", analyzer or (lambda _manifest, _body: result))


def test_completed_decision_survives_restart_but_text_and_policy_do_not_reuse(tmp_path):
    calls = Mock(return_value=SpecificationAnalysisResult("READY"))
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    first = lifecycle(tmp_path, "READY", calls)
    assert first.decide(manifest, "Title", BODY).verdict == "READY"
    restarted = lifecycle(tmp_path, "READY", Mock(side_effect=AssertionError("must reuse")))
    assert restarted.decide(manifest, "Title", BODY).verdict == "READY"
    changed = lifecycle(tmp_path, "READY", calls)
    changed.decide(build_normative_issue_manifest(1728, "Edited", BODY), "Edited", BODY)
    lifecycle(tmp_path, "READY", calls, policy="provider/model-b").decide(manifest, "Title", BODY)
    assert calls.call_count == 3


def test_error_is_not_persisted_and_is_retried(tmp_path):
    calls = Mock(side_effect=[SpecificationAnalysisResult("ERROR", error="outage"), SpecificationAnalysisResult("READY")])
    gate = lifecycle(tmp_path, "READY", calls)
    manifest = build_normative_issue_manifest(1728, "Title", BODY)
    assert gate.decide(manifest, "Title", BODY).verdict == "ERROR"
    assert gate.decide(manifest, "Title", BODY).verdict == "READY"
    assert calls.call_count == 2


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


def test_processing_label_rejection_releases_newly_admitted_owner(tmp_path):
    labeled = snapshot()
    labeled["labels"].append("@auto-coder")
    github = GitHubFlow([labeled, labeled, labeled])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    engine.config.CHECK_LABELS = True
    candidate.data["labels"] = labeled["labels"]

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)

    assert result.actions == ["Skipped - another instance started processing (@auto-coder label added)"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


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
    child = {**snapshot(), "parent_issue_number": 10}
    github = GitHubFlow([child, child])
    github.get_open_sub_issues = Mock(side_effect=lambda _repo, number: [19, 1728] if number == 10 else [])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    candidate.data["parent_issue_number"] = 10
    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Skipped - unresolved Issue hierarchy dependency"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


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
    assert dispatched == [30]
    assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 30),)


def test_hierarchy_uses_newly_authorized_parent_metadata(tmp_path):
    old = {**snapshot(), "body": "Parent-Issue: #10\n\n" + BODY}
    current = {**snapshot(), "body": "Parent-Issue: #11\n\n" + BODY}
    github = GitHubFlow([current, current, current])
    github.get_open_sub_issues = Mock(return_value=[])
    engine, candidate = engine_with_gate(tmp_path, github, lifecycle(tmp_path, "READY"))
    candidate.data.update(old)
    candidate.data["refill_metadata_open_children"] = {11: [19, 20]}

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert result.actions == ["Deferred - unresolved parent relationship"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_owners() == ()


@pytest.mark.parametrize(
    "child_state,child_author,native_parent,expected_dispatch",
    [
        ("open", 999, None, 30),
        ("closed", 1, None, 10),
        ("open", 1, 11, 10),
    ],
)
def test_real_refill_graph_uses_all_open_issues_and_native_precedence(tmp_path, child_state, child_author, native_parent, expected_dispatch):
    def issue(number, title, labels, body=BODY, state="open", author=1):
        return {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": label} for label in labels],
            "user": {"login": f"user-{author}", "id": author},
        }

    issues = {
        10: issue(10, "Parent", ["implementation-ready", "breaking-change"]),
        20: issue(20, "Child", ["implementation-ready"], "Parent-Issue: #10\n\n" + BODY, child_state, child_author),
        30: issue(30, "Fallback", ["implementation-ready"]),
    }
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance(token="test-token")
    github.get_open_entities_strict = Mock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(number) for number in issues]))
    github.get_issue_dispatch_snapshot_strict = Mock(side_effect=lambda _repo, number: dict(issues[number]))
    github.get_parent_issue_number_strict = Mock(side_effect=lambda _repo, number: native_parent if number == 20 else None)
    github.get_open_sub_issues_strict = Mock(return_value=[])
    github.get_all_sub_issues_strict = Mock(return_value=[])
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

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert dispatched == [expected_dispatch]
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


def test_changed_generation_revalidation_defers_until_live_implementation_finishes(tmp_path):
    state = {1728: snapshot(body=BODY + " A")}
    validation_b_started = Event()

    class MutableGitHub(GitHubFlow):
        def get_issue_dispatch_snapshot_strict(self, _repo, number):
            return dict(state[number])

    github = MutableGitHub([state[1728]])

    def analyze(_manifest, body):
        if body.endswith(" B"):
            validation_b_started.set()
            assert ImplementationOwner("issue", 1728) not in engine.implementation_slots.active_owners()
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
        deferred = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
        assert deferred.actions == ["Deferred - implementation ownership already exists (issue:1728)"]
        assert not validation_b_started.is_set()
        owner = ImplementationOwner("issue", 1728)
        assert engine.implementation_slots.active_execution_ids(owner)
        release.set()
        assert running.result(timeout=5).success is True

    engine._process_single_candidate_reserved = Mock()
    blocked = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    assert validation_b_started.is_set()
    assert blocked.actions == ["Rejected - blocked specification"]
    engine._process_single_candidate_reserved.assert_not_called()
    assert engine.implementation_slots.active_execution_ids(owner) == ()
    assert owner not in engine.implementation_slots.active_owners()


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


def test_supported_high_score_fallback_model_changes_production_policy_identity(monkeypatch):
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


def test_supported_alias_provider_change_invalidates_production_policy(monkeypatch):
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


def test_async_logical_owner_defers_changed_generation_validation(tmp_path):
    """A remote implementation owner remains authoritative after launch execution returns."""
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
    assert analyzed == [BODY + " A"]
    assert owner in engine.implementation_slots.active_owners()


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

        def get_linked_prs(self, _repo, _number, strict=False):
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

    def create_real_pr(_repo, issue_data, _backend_manager=None):
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
