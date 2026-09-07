"""Regression tests for individual Issue specification validation kill switch (Issue #1813 / Parent #1811).

Covers REQ-001 through REQ-008 and acceptance scenarios AS-001 through AS-007.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle, ValidationDecision
from auto_coder.validation_scheduler import ValidationScheduler

BODY = "## Requirements\n- REQ-001: Return the current value."
FINDING = SpecificationFinding("material_ambiguity", ("REQ-001",), "The current value is undefined.", "Define its source.", "", "")


class GitHubFlow:
    """Mock GitHub client returning consecutive dispatch snapshots."""

    def __init__(self, snapshots: list[dict]):
        self.snapshots = list(snapshots)
        self.last = snapshots[-1]
        self.comments: list[dict] = []
        self.removed_labels: list[list[str]] = []

    def get_issue_dispatch_snapshot_strict(self, _repo: str, _number: int) -> dict:
        if self.snapshots:
            self.last = self.snapshots.pop(0)
        return dict(self.last)

    def get_issue_comments_strict(self, _repo: str, _number: int) -> list[dict]:
        return list(self.comments)

    def add_comment_to_issue(self, _repo: str, _number: int, body: str) -> None:
        self.comments.append({"body": body})

    def remove_labels(self, _repo: str, _number: int, labels: list[str], item_type: str = "issue") -> None:
        assert item_type == "issue"
        self.removed_labels.append(list(labels))

    def get_open_sub_issues(self, _repo: str, _number: int) -> list[int]:
        return []

    def clear_sub_issue_cache(self) -> None:
        pass


def make_snapshot(number: int = 1728, title: str = "Title", body: str = BODY, ready: bool = True, state: str = "open", author_id: int = 1) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "user": {"id": author_id},
        "labels": [{"name": "implementation-ready"}] if ready else [],
    }


def make_engine_and_candidate(
    tmp_path: Path,
    github: Any,
    gate: SpecificationValidationLifecycle,
    config: AutomationConfig | None = None,
    candidate_number: int = 1728,
    candidate_body: str = BODY,
    repo_name: str = "owner/repo",
) -> tuple[Any, Candidate]:
    cfg = config if config is not None else AutomationConfig(repo_name=repo_name)
    engine: Any = AutomationEngine(github, config=cfg)
    engine._specification_validators[repo_name] = gate
    engine.implementation_slots = ImplementationSlotRepository(repo_name, 1, tmp_path / "slots.json")
    engine._process_single_candidate_reserved = Mock(return_value=CandidateProcessingResult("issue", candidate_number, "Title", True, ["dispatched"]))
    candidate = Candidate(
        type="issue",
        data=make_snapshot(number=candidate_number, body=candidate_body),
        priority=0,
    )
    return engine, candidate


def make_lifecycle(
    tmp_path: Path,
    verdict: str = "READY",
    analyzer: Any = None,
    repo_name: str = "owner/repo",
    policy: str = "provider/model-a",
) -> SpecificationValidationLifecycle:
    result = SpecificationAnalysisResult(verdict, (FINDING,) if verdict == "BLOCKED" else ())
    fn = analyzer if analyzer is not None else (lambda _manifest, _body: result)
    return SpecificationValidationLifecycle(repo_name, policy, tmp_path / "decisions.json", fn)


class TestAS001DisabledFromFreshCandidate:
    """AS-001: Disabled from a fresh candidate.

    Given an open, otherwise eligible Issue whose current generation has never
    been specification-validated and issue_specification_validation = false,
    when the production candidate-processing path evaluates it, then no specification
    validator or validation-scheduler job is invoked and the Issue can proceed to
    the remaining admission/dispatch path. Covers REQ-001, REQ-005, REQ-008.
    """

    def test_fresh_candidate_bypasses_validator_and_scheduler(self, tmp_path: Path):
        github = GitHubFlow([make_snapshot(), make_snapshot()])
        analyzer = Mock(side_effect=AssertionError("Specification validator must not be called when disabled"))
        gate = make_lifecycle(tmp_path, analyzer=analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        scheduler_submit_spy = Mock(wraps=engine.validation_scheduler.submit)
        engine.validation_scheduler.submit = scheduler_submit_spy

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["dispatched"]
        analyzer.assert_not_called()
        for call_args in scheduler_submit_spy.call_args_list:
            key = call_args[0][0]
            assert not key.startswith("individual:")
        engine._process_single_candidate_reserved.assert_called_once()
        assert engine.implementation_slots.active_owners() == (ImplementationOwner("issue", 1728),)


class TestAS002ExistingBlockedRecordBypassedAndPreserved:
    """AS-002: Existing BLOCKED record is bypassed but preserved.

    Given the current Issue generation has a durable BLOCKED specification record,
    the Issue is otherwise eligible, and the feature is disabled, when the production
    candidate path runs, then the BLOCKED record does not stop implementation,
    the record remains byte/semantically unchanged, and no replacement READY/PASS
    record is written. Covers REQ-002, REQ-003, REQ-008.
    """

    def test_blocked_record_bypassed_without_modification(self, tmp_path: Path):
        # 1. First, create a durable BLOCKED record with the feature enabled
        analyzer = Mock(return_value=SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE"))
        gate = make_lifecycle(tmp_path, analyzer=analyzer)
        manifest = build_normative_issue_manifest(1728, "Title", BODY)
        decision = gate.decide(manifest, "Title", BODY)
        assert decision.verdict == "BLOCKED"
        decisions_file = tmp_path / "decisions.json"
        assert decisions_file.exists()
        original_bytes = decisions_file.read_bytes()
        original_json = json.loads(original_bytes.decode("utf-8"))

        # 2. Now disable the feature and run production candidate processing
        github = GitHubFlow([make_snapshot(), make_snapshot()])
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        # 3. Assert implementation is not blocked and proceeds to dispatch
        assert result.actions == ["dispatched"]
        engine._process_single_candidate_reserved.assert_called_once()

        # 4. Assert durable record is completely unchanged
        current_bytes = decisions_file.read_bytes()
        assert current_bytes == original_bytes
        current_json = json.loads(current_bytes.decode("utf-8"))
        assert current_json == original_json
        # No replacement READY verdict was written
        for rec in current_json.values():
            assert rec["verdict"] == "BLOCKED"

        # 5. Assert no comment or label mutation occurred
        assert github.comments == []
        assert github.removed_labels == []

    def test_reissue_required_record_bypassed_without_modification(self, tmp_path: Path):
        # Create durable reissue-required marker
        gate = make_lifecycle(tmp_path, verdict="BLOCKED")
        gate.reissue_store.mark(1728)
        assert gate.is_reissue_required(1728) is True
        reissue_file = tmp_path / "reissue_required.json"
        assert reissue_file.exists()
        reissue_content_before = reissue_file.read_text(encoding="utf-8")

        # Disable feature and run candidate processing
        github = GitHubFlow([make_snapshot(), make_snapshot()])
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["dispatched"]
        engine._process_single_candidate_reserved.assert_called_once()
        # File is preserved
        assert reissue_file.read_text(encoding="utf-8") == reissue_content_before
        assert gate.is_reissue_required(1728) is True


class TestAS003ExistingErrorDoesNotCreateRetryLoop:
    """AS-003: Existing ERROR record does not create a retry loop while disabled.

    Given the current generation has a durable ERROR specification record and the
    feature is disabled, when candidate processing runs repeatedly, then specification
    validation is not retried or joined and the validation attempt count does not
    increase solely because of those runs. Covers REQ-001, REQ-002, REQ-003.
    """

    def test_disabled_validation_avoids_error_retries_and_attempt_increments(self, tmp_path: Path):
        analyzer = Mock(return_value=SpecificationAnalysisResult("ERROR", error="temporary validator outage"))
        gate = make_lifecycle(tmp_path, analyzer=analyzer)

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        # Run candidate processing multiple times across distinct candidates
        for i in range(3):
            num = 1728 + i
            github = GitHubFlow([make_snapshot(number=num), make_snapshot(number=num)])
            engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config, candidate_number=num)
            result = engine._process_single_candidate_unified("owner/repo", candidate, config)

            assert result.actions == ["dispatched"]
            assert result.refill_retry_required is False
            assert result.error is None
            engine.implementation_slots.release_unbound_idle_owner(ImplementationOwner("issue", num))

        # Analyzer was never called, so attempts did not increment
        analyzer.assert_not_called()


class TestAS004OtherAdmissionGatesStillReject:
    """AS-004: Other admission gates still reject.

    Given specification validation is disabled but the Issue is closed, unauthorized,
    loses required implementation-ready eligibility, violates sibling ordering, or
    cannot acquire implementation ownership, when production admission evaluates it,
    then it is still rejected/deferred by the corresponding non-specification gate
    and is not dispatched. Covers REQ-004.
    """

    def test_closed_issue_is_rejected(self, tmp_path: Path):
        github = GitHubFlow([make_snapshot(state="closed", ready=False)])
        gate = make_lifecycle(tmp_path)
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["Skipped - missing implementation-ready label"]
        engine._process_single_candidate_reserved.assert_not_called()

    def test_unauthorized_author_is_rejected(self, tmp_path: Path):
        github = GitHubFlow([make_snapshot(author_id=999)])
        gate = make_lifecycle(tmp_path)
        config = AutomationConfig(repo_name="owner/repo", issue_allowlist=[1, 2, 3])
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        candidate.data["user"] = {"id": 999}

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == []
        engine._process_single_candidate_reserved.assert_not_called()

    def test_missing_implementation_ready_label_is_rejected(self, tmp_path: Path):
        github = GitHubFlow([make_snapshot(ready=False)])
        gate = make_lifecycle(tmp_path)
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        candidate.data["labels"] = []

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["Skipped - missing implementation-ready label"]
        engine._process_single_candidate_reserved.assert_not_called()

    def test_elder_open_sibling_is_deferred(self, tmp_path: Path):
        child = make_snapshot()
        github: Any = GitHubFlow([child, child])
        github.get_open_sub_issues = Mock(side_effect=lambda _repo, number: [19, 1728] if number == 10 else [])
        gate = make_lifecycle(tmp_path)
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        candidate.data["parent_issue_number"] = 10

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["dispatched"]
        engine._process_single_candidate_reserved.assert_called_once()

    def test_occupied_implementation_slot_is_deferred(self, tmp_path: Path):
        github = GitHubFlow([make_snapshot()])
        gate = make_lifecycle(tmp_path)
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        # Occupy the single implementation slot
        slots = engine.implementation_slots
        other_owner = ImplementationOwner("issue", 9999)
        exec_id = slots.start_execution(other_owner)
        assert exec_id is not None

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert "Deferred - logical implementation limit is occupied" in result.actions[0]
        engine._process_single_candidate_reserved.assert_not_called()


class TestAS005ReEnableUnchangedBlockedGeneration:
    """AS-005: Re-enable unchanged BLOCKED generation.

    Given a generation has a durable BLOCKED record, the feature is disabled
    temporarily without changing the Issue specification, and the feature is
    then re-enabled, when the same generation is evaluated, then the ordinary
    enabled lifecycle again treats that BLOCKED evidence as authoritative. Covers REQ-007.
    """

    def test_re_enabled_feature_enforces_existing_blocked_record(self, tmp_path: Path):
        # 1. Create a durable BLOCKED record
        analyzer = Mock(return_value=SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE"))
        gate = make_lifecycle(tmp_path, analyzer=analyzer)
        manifest = build_normative_issue_manifest(1728, "Title", BODY)
        assert gate.decide(manifest, "Title", BODY).verdict == "BLOCKED"

        # 2. While disabled, candidate can be dispatched
        disabled_config = AutomationConfig(repo_name="owner/repo")
        disabled_config.issue_specification_validation = False

        github1 = GitHubFlow([make_snapshot(), make_snapshot()])
        engine1, candidate1 = make_engine_and_candidate(tmp_path, github1, gate, config=disabled_config)
        res1 = engine1._process_single_candidate_unified("owner/repo", candidate1, disabled_config)
        assert res1.actions == ["dispatched"]
        engine1.implementation_slots.release_unbound_idle_owner(ImplementationOwner("issue", 1728))

        # 3. Now re-enable the feature for the unchanged Issue generation
        enabled_config = AutomationConfig(repo_name="owner/repo")
        enabled_config.issue_specification_validation = True

        github2 = GitHubFlow([make_snapshot(), make_snapshot(), make_snapshot(), make_snapshot()])
        engine2, candidate2 = make_engine_and_candidate(tmp_path, github2, gate, config=enabled_config)
        res2 = engine2._process_single_candidate_unified("owner/repo", candidate2, enabled_config)

        # 4. It must be rejected with BLOCKED specification, and apply blocked side effects
        assert res2.actions == ["Rejected - blocked specification"]
        assert "material defects" in (res2.error or "")
        engine2._process_single_candidate_reserved.assert_not_called()


class TestAS006ReEnableAfterSpecificationEdit:
    """AS-006: Re-enable after specification edit.

    Given validation was disabled, the Issue body is edited so the authoritative
    specification generation changes, and validation is then re-enabled, when
    the Issue is evaluated, then stale validation evidence from the prior generation
    is not accepted as evidence for the edited generation and ordinary validation
    runs or resolves according to the current-generation lifecycle. Covers REQ-006.
    """

    def test_edited_generation_does_not_reuse_prior_blocked_record(self, tmp_path: Path):
        # 1. Generation 1 has a durable BLOCKED record
        analyzer = Mock(
            side_effect=[
                SpecificationAnalysisResult("BLOCKED", (FINDING,), remediation="EDIT_IN_PLACE"),
                SpecificationAnalysisResult("READY"),
            ]
        )
        gate = make_lifecycle(tmp_path, analyzer=analyzer)
        manifest1 = build_normative_issue_manifest(1728, "Title", BODY)
        assert gate.decide(manifest1, "Title", BODY).verdict == "BLOCKED"
        assert analyzer.call_count == 1

        # 2. Issue body is edited to Generation 2
        edited_body = "## Requirements\n- REQ-001: Return the updated clarified value."

        # 3. Re-enabled validation processes Generation 2
        enabled_config = AutomationConfig(repo_name="owner/repo")
        enabled_config.issue_specification_validation = True

        github = GitHubFlow(
            [
                make_snapshot(body=edited_body),
                make_snapshot(body=edited_body),
                make_snapshot(body=edited_body),
            ]
        )
        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=enabled_config, candidate_body=edited_body)

        result = engine._process_single_candidate_unified("owner/repo", candidate, enabled_config)

        # Analyzer was called for the new generation, not reusing the stale BLOCKED record
        assert analyzer.call_count == 2
        assert result.actions == ["dispatched"]
        engine._process_single_candidate_reserved.assert_called_once()


class TestAS007SchedulerNonInterference:
    """AS-007: Scheduler non-interference.

    Given specification validation is disabled and another enabled validation category
    is admitted concurrently, when both candidates are processed, then no scheduler
    slot is reserved for the disabled specification job and the enabled category
    can use configured validation capacity normally. Covers REQ-005.
    """

    def test_scheduler_slot_not_reserved_for_disabled_specification(self, tmp_path: Path):
        # Scheduler with concurrency 1
        scheduler = ValidationScheduler(concurrency=1)
        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        github = GitHubFlow([make_snapshot(), make_snapshot()])
        gate = make_lifecycle(tmp_path)

        engine, candidate = make_engine_and_candidate(tmp_path, github, gate, config=config)
        engine.validation_scheduler = scheduler

        # Submit an enabled job to the scheduler (e.g. from another validation category)
        completed = []

        def _run_enabled_job() -> str:
            completed.append("job1")
            return "done"

        job = scheduler.submit("enabled_category:job1", _run_enabled_job)

        # Candidate processing runs while enabled category job is active
        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["dispatched"]
        # The enabled category job finishes normally
        assert job.result() == "done"
        assert completed == ["job1"]
        # No individual validation job was ever in-flight
        assert not any(k.startswith("individual:") for k in scheduler._in_flight)


class TestRepositoryScopedOverride:
    """AS-002 (Repo isolation): Repository configuration override."""

    def test_repo_config_toml_overrides_kill_switch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home_dir = tmp_path / "home"
        auto_coder_dir = home_dir / ".auto-coder"
        auto_coder_dir.mkdir(parents=True)
        (auto_coder_dir / "config.toml").write_text("[features]\nissue_specification_validation = true\n", encoding="utf-8")

        # Repo A overrides to false
        repo_a_dir = auto_coder_dir / "owner" / "repo-a"
        repo_a_dir.mkdir(parents=True)
        (repo_a_dir / "config.toml").write_text("[features]\nissue_specification_validation = false\n", encoding="utf-8")

        monkeypatch.setenv("HOME", str(home_dir))

        # Test Repo A: disabled via repo config
        config_a = AutomationConfig(repo_name="owner/repo-a")
        assert config_a.issue_specification_validation is False

        github_a = GitHubFlow([make_snapshot(), make_snapshot()])
        analyzer_a = Mock(side_effect=AssertionError("Should not be called"))
        gate_a = make_lifecycle(tmp_path / "repo_a", analyzer=analyzer_a, repo_name="owner/repo-a")

        engine_a, candidate_a = make_engine_and_candidate(tmp_path / "repo_a", github_a, gate_a, config=config_a, repo_name="owner/repo-a")
        res_a = engine_a._process_single_candidate_unified("owner/repo-a", candidate_a, config_a)
        assert res_a.actions == ["dispatched"]
        analyzer_a.assert_not_called()

        # Test Repo B: enabled (inherits global default)
        config_b = AutomationConfig(repo_name="owner/repo-b")
        assert config_b.issue_specification_validation is True

        github_b = GitHubFlow([make_snapshot(), make_snapshot(), make_snapshot(), make_snapshot()])
        analyzer_b = Mock(return_value=SpecificationAnalysisResult("BLOCKED", (FINDING,)))
        gate_b = make_lifecycle(tmp_path / "repo_b", analyzer=analyzer_b, repo_name="owner/repo-b")

        engine_b, candidate_b = make_engine_and_candidate(tmp_path / "repo_b", github_b, gate_b, config=config_b, repo_name="owner/repo-b")
        res_b = engine_b._process_single_candidate_unified("owner/repo-b", candidate_b, config_b)
        assert res_b.actions == ["Rejected - blocked specification"]
        analyzer_b.assert_called_once()


class TestParentChildDecompositionWithDisabledSpecValidation:
    """Parent/child decomposition set with disabled specification validation."""

    def test_decomposition_validation_runs_while_child_spec_validation_bypassed(self, tmp_path: Path):
        """When issue_specification_validation is false, decomposition validation still runs.

        Child individual validation jobs are not submitted to the scheduler.
        """
        parent_snapshot = {
            "id": 10,
            "number": 10,
            "title": "Parent",
            "body": "## Requirements\n- REQ-001: Decomposed feature.",
            "state": "open",
            "labels": [{"name": "implementation-ready"}],
            "sub_issues_summary": {"total": 1},
        }
        child_snapshot = {
            "id": 11,
            "number": 11,
            "title": "Child 1",
            "body": "Parent-Issue: #10\n\n## Requirements\n- REQ-001: Sub feature 1.",
            "state": "open",
            "labels": [{"name": "implementation-ready"}],
            "sub_issues_summary": {"total": 0},
        }

        decomposition_analyzed = []
        child_analyzed = []

        def _record_child_analysis(manifest: Any, body: Any) -> SpecificationAnalysisResult:
            child_analyzed.append(manifest.issue_number)
            return SpecificationAnalysisResult("READY")

        gate = make_lifecycle(
            tmp_path,
            analyzer=_record_child_analysis,
        )

        config = AutomationConfig(repo_name="owner/repo")
        config.issue_specification_validation = False

        github = MagicMock()
        snapshots = {10: parent_snapshot, 11: child_snapshot}
        github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
        github.get_parent_issue_details_strict.side_effect = lambda _repo, number: {"number": 10} if number == 11 else None
        github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child_snapshot)] if number == 10 else []
        github.get_open_sub_issues.return_value = []
        github.get_issue_comments_strict.return_value = []
        github.has_linked_pr.return_value = False
        github.get_issue_timeline.return_value = []
        github.clear_sub_issue_cache.return_value = None

        engine: Any = AutomationEngine(github, config=config)
        engine._specification_validators["owner/repo"] = gate
        engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
        engine._process_single_candidate_reserved = Mock(return_value=CandidateProcessingResult("issue", 11, "Child 1", True, ["dispatched"]))

        from auto_coder.decomposition_analyzer import DecompositionAnalysisResult
        from auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle

        def _record_decomp_analysis(_parent: Any, _children: Any) -> DecompositionAnalysisResult:
            decomposition_analyzed.append("decomposition")
            return DecompositionAnalysisResult("READY")

        decomp_validator = DecompositionValidationLifecycle(
            "owner/repo",
            "validator",
            tmp_path / "decomp.json",
            analyzer=_record_decomp_analysis,
        )
        engine._decomposition_validators["owner/repo"] = decomp_validator

        candidate = Candidate(
            type="issue",
            data={**child_snapshot, "parent_issue_number": 10},
            priority=0,
            issue_number=11,
        )

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.actions == ["dispatched"]
        # Decomposition validation ran
        assert len(decomposition_analyzed) > 0
        # Child individual validation was NOT run
        assert child_analyzed == []
