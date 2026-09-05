"""Generation-bound Issue specification validation lifecycle regressions."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import Mock

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

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
    assert github.removals == 1


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
