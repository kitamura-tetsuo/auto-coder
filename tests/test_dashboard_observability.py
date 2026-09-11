"""Mandatory production-to-mounted-view dashboard observability regressions."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import FastAPI

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome, StaleJulesPRResult
from auto_coder.automation_engine import AutomationEngine, _ValidationPublicationStageHandler
from auto_coder.dashboard import init_dashboard
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity
from auto_coder.reissue_required_store import ReissueRequiredStore
from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _mounted_detail(mock_ui, item_type: str, item_number: int):
    pages = {}

    def page(path):
        def register(function):
            pages[path] = function
            return function

        return register

    mock_ui.page.side_effect = page
    init_dashboard(FastAPI(), MagicMock(spec=AutomationEngine), "owner/repo")
    pages["/detail/{item_type}/{item_number}"](item_type=item_type, item_number=item_number)
    return mock_ui.mermaid.call_args[0][0] if mock_ui.mermaid.called else ""


def _assert_required_stage_visible(diagram: str, display_text: str) -> None:
    assert display_text in diagram, f"required production stage {display_text!r} did not reach the mounted detail view"


@patch("auto_coder.dashboard.ui")
def test_cached_terminal_refusal_reaches_mounted_detail_without_github(mock_ui):
    config = AutomationConfig(repo_name="owner/repo")
    github = MagicMock()
    engine = AutomationEngine(github, config)
    store = ReissueRequiredStore("owner/repo")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.mark(2000)
    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data={"number": 2000}, priority=0), config)
    assert result.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert result.success is False
    assert github.mock_calls == []
    assert engine.implementation_slots is None
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=2000)
    stages = [event for event in snapshot.events if event.kind == EventKind.STAGE_RESULT.value]
    assert [event.stage_id for event in stages] == ["issue.cached-blocked-admission"]
    assert stages[0].outcome == Outcome.BLOCKED.value
    _assert_required_stage_visible(_mounted_detail(mock_ui, "issue", 2000), "cached blocked admission")


def _run_admission_to_view(mock_ui, item_type: str, item_number: int) -> None:
    """A real pre-worker denial is displayed without fabricated downstream work."""
    config = AutomationConfig()
    if item_type == "issue":
        config.ISSUE_ALLOWLIST = []
    else:
        config.PR_ALLOWLIST = []
    candidate = Candidate(
        type=item_type,
        data={"number": item_number, "title": "Denied", "body": "", "labels": [], "head": {"sha": "a" * 40}},
        priority=0,
    )

    result = AutomationEngine(MagicMock(), config)._process_single_candidate_unified("owner/repo", candidate, config)

    assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type=item_type, item_number=item_number)
    assert any(event.kind == EventKind.STAGE_RESULT.value and event.stage_id == f"{item_type}.author-admission" for event in snapshot.events)
    diagram = _mounted_detail(mock_ui, item_type, item_number)
    _assert_required_stage_visible(diagram, "author admission")
    assert f"{item_type}.ci-" not in diagram
    assert f"{item_type}.merge-" not in diagram
    assert f"{item_type}.dispatch" not in diagram


@patch("auto_coder.dashboard.ui")
def test_issue_admission_reaches_mounted_detail_view(mock_ui):
    _run_admission_to_view(mock_ui, "issue", 194801)


@patch("auto_coder.dashboard.ui")
def test_pr_admission_reaches_mounted_detail_view(mock_ui):
    _run_admission_to_view(mock_ui, "pr", 194802)


@patch("auto_coder.dashboard.ui")
def test_missing_producer_emission_is_rejected_by_joined_oracle(mock_ui, monkeypatch):
    """Mutation control: unchanged business denial cannot pass without its emission."""
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []
    collector = get_trace_collector()
    real_record = collector.record_event

    def suppress_required(kind, stage_id, origin, **kwargs):
        if stage_id == "issue.author-admission":
            return None
        return real_record(kind, stage_id, origin, **kwargs)

    monkeypatch.setattr(collector, "record_event", suppress_required)
    candidate = Candidate(type="issue", data={"number": 194805, "title": "Denied", "body": "", "labels": []}, priority=0)

    result = AutomationEngine(MagicMock(), config)._process_single_candidate_unified("owner/repo", candidate, config)

    assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
    snapshot = collector.get_snapshot(repository="owner/repo", item_type="issue", item_number=194805)
    assert not any(event.kind == EventKind.STAGE_RESULT.value and event.stage_id == "issue.author-admission" for event in snapshot.events)
    diagram = _mounted_detail(mock_ui, "issue", 194805)
    with pytest.raises(AssertionError, match="did not reach the mounted detail view"):
        _assert_required_stage_visible(diagram, "author admission")


def _skip_all_issues_config() -> AutomationConfig:
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


class TestJoinedProductionToView:
    """Additional REQ-002 cases: real production processing reaching the
    real mounted detail page for origins the admission-denial cases above
    do not exercise (resumption, handoff, and a second concurrent execution
    for the same item)."""

    @patch("auto_coder.dashboard.ui")
    def test_pr_pending_work_resumption_reaches_detail_view_as_superseded(self, mock_ui):
        from auto_coder.automation_engine import PR_PROCESSING_STAGE, _PrProcessingStageHandler

        github = MagicMock()
        github.get_pull_request_metadata_strict.return_value = {"raw": True}
        github.get_pr_details.return_value = {"number": 2101, "head": {"sha": "new-head"}}
        engine = AutomationEngine(github, AutomationConfig())
        handler = _PrProcessingStageHandler(engine, "owner/repo")
        obligation = PendingObligation(
            WorkIdentity("owner/repo", "pr:2101", PR_PROCESSING_STAGE, "old-head"),
            PendingReason.THROTTLED,
            0.0,
            ("authoritative-refresh", "pr-processing"),
        )

        outcome = handler.dispatch(obligation)
        assert outcome.superseded is True

        diagram = _mounted_detail(mock_ui, "pr", 2101)
        _assert_required_stage_visible(diagram, "pending-work resume refresh")
        assert "outcome: superseded" in diagram
        # A superseded resumption must never be displayed as a completed
        # re-evaluation or a merge (REQ-006 of Issue #1947).
        assert "merge delivery" not in diagram

    @patch("auto_coder.dashboard.ui")
    def test_merge_operation_resumption_reaches_detail_view_as_superseded(self, mock_ui):
        from auto_coder.automation_engine import _MergeOperationResumeHandler
        from auto_coder.merge_operation_state import MergeOperation, MergeOperationIdentity, OperationStatus

        github = MagicMock()
        github.get_pull_request_metadata_strict.return_value = {"raw": True}
        github.get_pr_details.return_value = {"number": 2201, "head": {"sha": "newer-head"}}
        engine = AutomationEngine(github, AutomationConfig())
        handler = _MergeOperationResumeHandler(engine, "owner/repo")
        identity = MergeOperationIdentity("https://api.github.com", "owner/repo", 2201)
        operation = MergeOperation(
            identity=identity,
            expected_head_sha="stale-head",
            merge_method="squash",
            approval_credential_role="auto-coder-bot",
            reviewer_identity="",
            generation=1,
            status=OperationStatus.WAITING,
            resume_reason="throttled",
            not_before=0.0,
            effects={},
        )

        with patch("auto_coder.merge_operation_state.get_merge_operation_store") as mock_store_factory:
            mock_store_factory.return_value = MagicMock()
            handler(operation)

        diagram = _mounted_detail(mock_ui, "pr", 2201)
        _assert_required_stage_visible(diagram, "merge-operation resume refresh")
        assert "outcome: superseded" in diagram
        assert "merge delivery" not in diagram

    @patch("auto_coder.issue_processor.get_commit_log", return_value="No commits")
    @patch("auto_coder.issue_processor.JulesClient")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.issue_processor.render_prompt")
    @patch("auto_coder.dashboard.ui")
    def test_accepted_handoff_reaches_detail_view_without_pr_publication(self, mock_ui, mock_render, mock_cloud_manager_class, mock_jules_client_class, mock_get_commit_log):
        from auto_coder.issue_processor import _process_issue_jules_mode

        mock_jules_client = Mock()
        mock_jules_client.start_session.return_value = "session_789"
        mock_jules_client_class.return_value = mock_jules_client
        mock_cloud_manager = Mock()
        mock_cloud_manager.add_session.return_value = True
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_config = Mock()
        mock_config.MAIN_BRANCH = "main"
        with get_trace_collector().start_execution("owner/repo", "issue", 2301, origin="worker"):
            _process_issue_jules_mode(repo_name="owner/repo", issue_data={"number": 2301, "title": "T", "body": "B"}, config=mock_config, github_client=Mock())

        diagram = _mounted_detail(mock_ui, "issue", 2301)
        assert "outcome: accepted_handoff" in diagram
        # Accepting a remote task is not PR publication or completed
        # implementation (REQ-006 of Issue #1947, REQ-007 of Issue #1948).
        assert "pr-publication" not in diagram
        assert "outcome: completed" not in diagram

    def test_validation_scheduler_job_is_a_distinct_execution_from_the_worker(self):
        class _Decision:
            verdict = "BLOCKED"

        collector = get_trace_collector()
        with collector.start_execution("owner/repo", "issue", 2401, origin="worker") as ambient:
            ambient.set_outcome(Outcome.DEFERRED)
            AutomationEngine._traced_validation_job(
                "owner/repo",
                2401,
                "issue.individual-validation-job",
                "issue#2401 individual validation job",
                {},
                lambda: _Decision(),
            )

        from auto_coder.dashboard_detail import executions_for_item

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=2401)
        executions = executions_for_item(snapshot, "owner/repo", "issue", 2401)
        # The worker's own execution and the validation job's execution are
        # two independently navigable evaluations of the same Issue, not one
        # merged history (REQ-003 of Issue #1947).
        assert len(executions) == 2
        assert len({e.execution_id for e in executions}) == 2


class TestNewOriginCoverage:
    """REQ-003: production origins the inventory names but no runnable test
    actually exercised through real diagnostic-trace emission yet."""

    def test_explicit_single_target_origin_is_recorded(self):
        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 2501, "title": "T", "body": "B", "labels": []}, priority=0)

        engine._process_single_candidate_unified("owner/repo", candidate, config, origin="explicit-single-target")

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=2501)
        started = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started) == 1
        assert started[0].origin == "explicit-single-target"

    def test_validation_publication_resumption_origin_is_recorded(self):
        """The inventory's prior citation (test_validation_publication_resumption.py)
        verifies durable-effect behavior but never touches TraceCollector; this
        drives the same production handler and reads its diagnostic trace back."""
        github = MagicMock()
        github.get_issue_dispatch_snapshot_strict.return_value = {"number": 2601, "title": "T", "body": "B"}
        # A generic MagicMock parent-lookup call returns a non-dict, so
        # ``_get_authoritative_parent_number`` resolves this as standalone.
        engine = AutomationEngine(github, AutomationConfig())

        fake_identity = MagicMock()
        fake_identity.key = "rev-1"
        fake_decision = MagicMock(identity=fake_identity, verdict="BLOCKED")
        fake_validator = MagicMock()
        fake_validator.identity.return_value = fake_identity
        fake_validator.store.get.return_value = fake_decision
        fake_validator.apply_blocked.return_value = None
        engine._get_specification_validator = Mock(return_value=fake_validator)  # type: ignore[method-assign]

        handler = _ValidationPublicationStageHandler(engine, "owner/repo")
        obligation = PendingObligation(
            WorkIdentity("owner/repo", "issue:2601", "validation-publication", "rev-1"),
            PendingReason.THROTTLED,
            0.0,
            ("blocked-comment",),
        )

        with patch("auto_coder.automation_engine.get_pending_work_store") as mock_store_factory:
            mock_store_factory.return_value = MagicMock(get=Mock(return_value=None))
            outcome = handler.dispatch(obligation)

        assert outcome.completed_effects == ("blocked-comment",)
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=2601)
        started = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started) == 1
        assert started[0].origin == "validation-publication-resumption"
        finished = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value]
        assert len(finished) == 1
        assert finished[0].outcome == Outcome.COMPLETED.value

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.run_adversarial_validation")
    @patch("auto_coder.pr_processor.isolated_pr_head_worktree")
    @patch("auto_coder.pr_processor._checkout_pr_branch")
    @patch("auto_coder.pr_processor._merge_pr")
    def test_asynchronous_pr_adversarial_validation_origin_is_recorded(
        self,
        mock_merge_pr,
        mock_checkout,
        mock_worktree,
        mock_run_validation,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        """The inventory's prior citation
        (test_take_pr_actions_preserves_structured_adversarial_failure) mocks
        out ``_handle_pr_merge`` entirely, so it never touches the real
        ``pr.adversarial-validation`` emission this test drives for real
        (admitted through the real ``AdversarialValidationScheduler``)."""
        from auto_coder.adversarial_validation_scheduler import AdversarialValidationScheduler
        from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult
        from auto_coder.github_app_reviewer import ReviewPublicationResult
        from auto_coder.pr_processor import _handle_pr_merge

        mock_checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
        mock_worktree.return_value.__enter__.return_value = "/tmp/worktree"
        mock_run_validation.return_value = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Found specification violation",
            findings=[
                AdversarialValidationFinding(
                    violated_requirement="Spec requires idempotency",
                    counterexample="Given state S, action A twice produces duplicate X",
                    test_gap="Only a single call is exercised",
                    suggested_regression_scenario="Call action twice and verify state",
                )
            ],
        )

        config = AutomationConfig()
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        pr_data = {"number": 2701, "body": "Fixes #99", "labels": [], "head": {"ref": "feature-branch", "sha": "abc123456789"}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []
        scheduler = AdversarialValidationScheduler(concurrency=2)

        with (
            patch("auto_coder.pr_processor.publish_adversarial_review", return_value=ReviewPublicationResult(True, "REQUEST_CHANGES", "")),
            get_trace_collector().start_execution("owner/repo", "pr", 2701, origin="worker"),
        ):
            actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {}, adversarial_validation_scheduler=scheduler)

        mock_merge_pr.assert_not_called()
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=2701)
        adv_events = [e for e in snapshot.events if e.stage_id == "pr.adversarial-validation"]
        assert len(adv_events) == 1
        assert adv_events[0].outcome == Outcome.BLOCKED.value
        assert adv_events[0].facts["examined_head"] == "abc123456789"
        assert adv_events[0].facts["reason"] == "needs_fix"
        # A blocked finding must never be silently reported as a passing
        # validation or a completed merge (REQ-005, REQ-007).
        assert not any(e.stage_id == "pr.merge-delivery" for e in snapshot.events)


class TestOutcomeMatrixCoverage:
    """REQ-004: outcome-matrix cases the inventory claims but no runnable
    test previously exercised."""

    def _observe(self, pr_number: int, mock_observe_ci, snapshot):
        from auto_coder.util.github_action import _check_github_actions_status

        mock_observe_ci.return_value = snapshot
        github_client = MagicMock(token="tok")
        with get_trace_collector().start_execution("owner/repo", "pr", pr_number, origin="worker"):
            result = _check_github_actions_status("owner/repo", {"number": pr_number, "head": {"sha": "a" * 40}}, AutomationConfig(), github_client)
        events = [e for e in get_trace_collector().get_snapshot(item_type="pr", item_number=pr_number).events if e.stage_id == "pr.ci-observation"]
        assert len(events) == 1
        return result, events[0]

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_known_empty_ci_availability_is_deferred_not_a_pass_or_failure(self, mock_observe_ci, _mock_api):
        from auto_coder.ci_observation import CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject

        subject = ObservationSubject("https://api.github.com", "owner/repo", 2801, "a" * 40)
        snapshot = CIObservationSnapshot(subject, ObservationRequest("github-actions", "checks+workflows"), "cycle-1", 0, ObservationAvailability.KNOWN_EMPTY)
        result, event = self._observe(2801, mock_observe_ci, snapshot)

        # No current CI observations is neither a pass nor a failure verdict;
        # it stays an explicit deferred/in-progress read (REQ-004 of #1946).
        assert result.success is False
        assert result.in_progress is True
        assert event.facts["availability"] == "known_empty"
        assert event.outcome == Outcome.DEFERRED.value

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_partial_ci_availability_is_unknown_not_a_pass_or_failure(self, mock_observe_ci, _mock_api):
        from auto_coder.ci_observation import CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject

        subject = ObservationSubject("https://api.github.com", "owner/repo", 2802, "a" * 40)
        snapshot = CIObservationSnapshot(subject, ObservationRequest("github-actions", "checks+workflows"), "cycle-1", 0, ObservationAvailability.PARTIAL)
        result, event = self._observe(2802, mock_observe_ci, snapshot)

        assert result.success is False
        assert event.facts["availability"] == "partial"
        assert event.outcome == Outcome.UNKNOWN.value

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_throttled_ci_availability_is_unknown_not_a_pass_or_failure(self, mock_observe_ci, _mock_api):
        from auto_coder.ci_observation import CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject

        subject = ObservationSubject("https://api.github.com", "owner/repo", 2803, "a" * 40)
        snapshot = CIObservationSnapshot(subject, ObservationRequest("github-actions", "checks+workflows"), "cycle-1", 0, ObservationAvailability.THROTTLED, unavailable_reason="GitHub CI request was throttled")
        result, event = self._observe(2803, mock_observe_ci, snapshot)

        assert result.success is False
        assert event.facts["availability"] == "throttled"
        assert event.outcome == Outcome.UNKNOWN.value

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_superseded_ci_availability_is_reported_as_superseded(self, mock_observe_ci, _mock_api):
        from auto_coder.ci_observation import CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject

        subject = ObservationSubject("https://api.github.com", "owner/repo", 2804, "a" * 40)
        snapshot = CIObservationSnapshot(subject, ObservationRequest("github-actions", "checks+workflows"), "cycle-1", 0, ObservationAvailability.SUPERSEDED)
        result, event = self._observe(2804, mock_observe_ci, snapshot)

        # A superseded read is reported as such -- never silently reused as
        # the current pass/fail verdict for a newer head (REQ-004 of #1946).
        assert result.success is False
        assert event.facts["availability"] == "superseded"
        assert event.outcome == Outcome.SUPERSEDED.value

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.quota_selector.rank_high_score_backends_by_quota")
    @patch("auto_coder.llm_backend_config.get_llm_config")
    @pytest.mark.parametrize(
        "backend_type,dispatch_target",
        [
            ("claude-routine", "auto_coder.issue_processor._process_issue_claude_routine_mode"),
            ("codex-cloud", "auto_coder.issue_processor._process_issue_codex_cloud_mode"),
        ],
    )
    def test_ordinary_cloud_selects_claude_routine_and_codex_cloud(self, mock_get_llm_config, mock_rank, mock_label_manager, backend_type, dispatch_target):
        from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration

        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx
        mock_rank.side_effect = lambda candidates, *_a, **_k: list(candidates)
        mock_get_llm_config.return_value = LLMBackendConfiguration(
            backend_cloud_order=["backend-1"],
            backends={"backend-1": BackendConfig(name="backend-1", backend_type=backend_type)},
        )

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)
        issue_number = 2901 if backend_type == "claude-routine" else 2902
        candidate = Candidate(type="issue", priority=100, data={"number": issue_number, "title": "Simple bug", "labels": [{"name": "bug"}]})

        with patch(dispatch_target, return_value=[f"{backend_type} handoff accepted"]) as mock_dispatch:
            result = engine._process_single_candidate_unified("owner/repo", candidate, config, jules_mode=True)

        assert result.success is True
        mock_dispatch.assert_called_once()
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=issue_number)
        selection_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch.selection"]
        assert len(selection_events) == 1
        assert selection_events[0].facts["backend_type"] == backend_type
        assert selection_events[0].facts["candidate_pool"] == "cloud"
        route_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch-route"]
        assert route_events[0].facts["route"] == "cloud"

    @patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
    @patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
    @patch("auto_coder.pr_processor._check_github_actions_status")
    @patch("auto_coder.pr_processor.has_unresolved_review_threads", return_value=False)
    @patch("auto_coder.pr_processor.get_detailed_checks_from_history")
    @patch("auto_coder.pr_processor._is_jules_pr", return_value=True)
    @patch("auto_coder.pr_processor._close_stale_jules_pr")
    @patch("auto_coder.pr_processor._send_jules_error_feedback", return_value=["Sent Jules error feedback"])
    def test_corrective_work_accepted_without_claiming_a_repair(
        self,
        mock_send_feedback,
        mock_close_stale,
        mock_is_jules,
        mock_detailed,
        mock_threads,
        mock_checks,
        mock_mergeable,
        mock_exit_in_progress,
    ):
        from auto_coder.pr_processor import _handle_pr_merge

        mock_checks.return_value = GitHubActionsStatusResult(success=False, ids=[1])
        mock_detailed.return_value = DetailedChecksResult(success=False, failed_checks=[{"name": "build"}])
        mock_close_stale.return_value = StaleJulesPRResult(closed=False)

        config = AutomationConfig()
        pr_data = {"number": 3001, "body": "Fixes #99", "labels": [], "head": {"ref": "feature", "sha": "a" * 40}}
        client = MagicMock()
        client.get_pr_review_threads_strict.return_value = []

        with get_trace_collector().start_execution("owner/repo", "pr", 3001, origin="worker"):
            actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

        assert any("Jules will handle fixing" in a for a in actions)
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=3001)
        repair_events = [e for e in snapshot.events if e.stage_id == "pr.repair-delegation"]
        assert len(repair_events) == 1
        assert repair_events[0].outcome == Outcome.ACCEPTED_HANDOFF.value
        assert repair_events[0].facts["backend"] == "jules"
        # Accepting the continuation is not proof of a repair, passing CI, or
        # a merge (REQ-006 of Issue #1946, REQ-007 of Issue #1948).
        assert not any(e.stage_id == "pr.merge-delivery" for e in snapshot.events)
        assert not any(e.stage_id == "pr.cleanup" for e in snapshot.events)

    def test_queued_validation_is_distinguishable_from_disabled_and_blocked(self):
        """A running validation job (STAGE_STARTED, no result yet) is
        neither a disabled bypass nor a completed blocked verdict."""
        release = threading.Event()

        def _slow_ready():
            release.wait(timeout=5)

            class _Decision:
                verdict = "READY"

            return _Decision()

        collector = get_trace_collector()
        with collector.start_execution("owner/repo", "issue", 3101, origin="worker"):
            worker = threading.Thread(
                target=AutomationEngine._traced_validation_job,
                args=("owner/repo", 3101, "issue.individual-validation-job", "issue#3101 individual validation job", {}, _slow_ready),
            )
            worker.start()
            try:
                # Poll briefly for the job's own execution-started event; it
                # must appear before the job's result does, since the result
                # is gated on `release`.
                for _ in range(200):
                    snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=3101)
                    started = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value and e.stage_id == "issue.individual-validation-job"]
                    if started:
                        break
                    threading.Event().wait(0.01)
                assert started, "validation job never recorded its own execution-started event"
                results = [e for e in snapshot.events if e.kind == EventKind.STAGE_RESULT.value and e.stage_id == "issue.individual-validation-job"]
                # Queued/running: started, no result yet -- distinct from a
                # disabled bypass (immediate SKIPPED) and a blocked verdict
                # (STAGE_RESULT with Outcome.BLOCKED).
                assert results == []
            finally:
                release.set()
                worker.join(timeout=5)

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=3101)
        finished = [e for e in snapshot.events if e.kind == EventKind.STAGE_RESULT.value and e.stage_id == "issue.individual-validation-job"]
        assert finished[0].facts["verdict"] == "READY"


class TestAdditionalNegativeAndMutationControls:
    """REQ-005/REQ-007: negative controls not yet exercised by the mutation
    control above (which only covers a single-item admission denial)."""

    def test_concurrent_items_never_share_or_swap_execution_identity(self):
        """Two different Issues processed concurrently through the real
        worker entrypoint keep fully separate execution identities; neither
        item's trace can be swapped for the other's (REQ-005)."""
        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        results: dict[int, ExplicitTargetOutcome] = {}

        def _run(number: int) -> None:
            candidate = Candidate(type="issue", data={"number": number, "title": "T", "body": "B", "labels": []}, priority=0)
            results[number] = engine._process_single_candidate_unified("owner/repo", candidate, config).target_outcome

        threads = [threading.Thread(target=_run, args=(n,)) for n in (3301, 3302)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert results == {3301: ExplicitTargetOutcome.SKIPPED, 3302: ExplicitTargetOutcome.SKIPPED}
        snap_a = get_trace_collector().get_snapshot(item_type="issue", item_number=3301)
        snap_b = get_trace_collector().get_snapshot(item_type="issue", item_number=3302)
        exec_a = {e.execution_id for e in snap_a.events}
        exec_b = {e.execution_id for e in snap_b.events}
        assert exec_a.isdisjoint(exec_b)
        assert all(e.item_number == 3301 for e in snap_a.events)
        assert all(e.item_number == 3302 for e in snap_b.events)

    @patch("auto_coder.dashboard.ui")
    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_unavailable_ci_evidence_is_never_rendered_as_success_or_failure(self, mock_observe_ci, _mock_api, mock_ui):
        """Unavailable CI evidence reaches the mounted detail view as an
        explicit 'unknown' outcome, never coerced to true/false (REQ-005 of
        Issue #1947, REQ-005 of Issue #1948)."""
        from auto_coder.ci_observation import CIObservationSnapshot, ObservationAvailability, ObservationRequest, ObservationSubject
        from auto_coder.util.github_action import _check_github_actions_status

        subject = ObservationSubject("https://api.github.com", "owner/repo", 3401, "a" * 40)
        request = ObservationRequest("github-actions", "checks+workflows")
        mock_observe_ci.return_value = CIObservationSnapshot(subject, request, "cycle-1", 0, ObservationAvailability.UNAVAILABLE, unavailable_reason="throttled")

        with get_trace_collector().start_execution("owner/repo", "pr", 3401, origin="worker"):
            _check_github_actions_status("owner/repo", {"number": 3401, "head": {"sha": "a" * 40}}, AutomationConfig(), MagicMock(token="tok"))

        diagram = _mounted_detail(mock_ui, "pr", 3401)
        assert "outcome: unknown" in diagram
        assert "outcome: true" not in diagram
        assert "outcome: false" not in diagram
        assert "outcome: completed" not in diagram
        assert "outcome: failed" not in diagram


@patch("auto_coder.dashboard.ui")
def test_codex_app_server_failure_remains_deferred_in_detail_view(mock_ui, tmp_path):
    """An unavailable account read cannot submit work or become a handoff."""
    from pathlib import Path

    from auto_coder.cloud_run import CloudRunRepository
    from auto_coder.exceptions import AutoCoderUsageLimitError
    from auto_coder.issue_processor import _process_issue_codex_cloud_mode
    from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration

    backend_config = LLMBackendConfiguration(backends={"codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud", environment_id="env-test")})
    github = MagicMock()
    collector = get_trace_collector()
    with (
        patch.object(Path, "home", return_value=tmp_path),
        patch("auto_coder.codex_cloud_client.get_llm_config", return_value=backend_config),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
        patch("auto_coder.codex_usage_checker.read_account_data", side_effect=TimeoutError()),
        patch("auto_coder.codex_cloud_client.CommandExecutor.run_command") as submit,
        collector.start_execution("owner/repo", "issue", 3501, origin="worker"),
    ):
        with pytest.raises(AutoCoderUsageLimitError):
            _process_issue_codex_cloud_mode("owner/repo", {"number": 3501, "title": "Fix", "body": "", "labels": []}, AutomationConfig(), github, "codex-cloud")
        assert CloudRunRepository("owner/repo").get(3501, 0) is None
    submit.assert_not_called()
    github.add_comment_to_issue.assert_not_called()
    snapshot = collector.get_snapshot(repository="owner/repo", item_type="issue", item_number=3501)
    events = [event for event in snapshot.events if event.stage_id == "issue.dispatch.codex-cloud"]
    assert len(events) == 1
    assert events[0].outcome == Outcome.DEFERRED.value
    assert events[0].facts == {"backend": "codex-cloud", "reason": "usage limit", "issue_number": 3501}
    diagram = _mounted_detail(mock_ui, "issue", 3501)
    _assert_required_stage_visible(diagram, "Codex Cloud dispatch")
    assert "deferred" in diagram.lower()


@pytest.mark.parametrize("route", ["cloud", "high-score-cloud"])
@pytest.mark.parametrize("submission", ["quota", "rejected", "indeterminate", "accepted", "ineligible", "ineligible-existing"])
@patch("auto_coder.dashboard.ui")
def test_cloud_submission_slot_cleanup_reaches_detail_view(mock_ui, tmp_path, monkeypatch, route, submission):
    from auto_coder.cloud_run import CloudRun, CloudRunRepository
    from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult
    from auto_coder.exceptions import AutoCoderUsageLimitError
    from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
    from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
    from auto_coder.specification_analyzer import SpecificationAnalysisResult
    from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle

    monkeypatch.setenv("HOME", str(tmp_path))
    labels = [{"name": "implementation-ready"}]
    if route == "high-score-cloud":
        labels.append({"name": "difficult"})
    issue = {"number": 1982, "title": "Implement", "body": "## Requirements\nREQ-001: Return a value.", "state": "open", "labels": labels}
    github = MagicMock(token="token")
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_: dict(issue)
    github.get_item_type_strict.return_value = "issue"
    github.get_parent_issue_details_strict.return_value = None
    github.get_direct_sub_issues_strict.return_value = []
    github.get_all_sub_issues.return_value = []
    github.get_open_sub_issues.return_value = []
    github.get_parent_issue_details.return_value = None
    github.get_labels.return_value = []
    github.try_add_labels.return_value = True
    github.get_issue_details.return_value = dict(issue)
    config = AutomationConfig()
    engine = AutomationEngine(github, config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test", tmp_path / "spec.json", lambda *_: SpecificationAnalysisResult("READY"))
    backend_config = LLMBackendConfiguration(
        backends={"codex-cloud": BackendConfig(name="codex-cloud", backend_type="codex-cloud", environment_id="env-test")},
        backend_cloud_order=["codex-cloud"],
        backend_with_high_score_cloud_order=["codex-cloud"],
    )
    collector = get_trace_collector()
    ineligible = submission.startswith("ineligible")
    if submission == "ineligible-existing":
        assert CloudRunRepository("owner/repo").save(CloudRun("owner/repo", 1982, 0, "codex-cloud", submission_outcome="indeterminate"))
    with (
        patch("auto_coder.llm_backend_config.get_llm_config", return_value=backend_config),
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", side_effect=lambda values, _: [] if ineligible else values),
        patch("auto_coder.codex_cloud_client.CodexCloudClient") as client_type,
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
        patch("auto_coder.issue_processor.get_current_attempt", return_value=0),
        patch("auto_coder.issue_processor._take_issue_actions") as fallback,
        collector.start_execution("owner/repo", "issue", 1982, origin="worker"),
    ):
        client = client_type.return_value
        client.environment_id = "env-test"
        if submission == "quota" or ineligible:
            client.submit_task.side_effect = AutoCoderUsageLimitError("quota unavailable")
        else:
            outcome = {"rejected": CodexSubmissionOutcome.DEFINITELY_NOT_SUBMITTED, "indeterminate": CodexSubmissionOutcome.INDETERMINATE, "accepted": CodexSubmissionOutcome.ACCEPTED}[submission]
            client.submit_task.return_value = CodexSubmissionResult(outcome, "task-a" if submission == "accepted" else "", diagnostic="test submission")
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config, jules_mode=route == "cloud")
    if ineligible:
        client.submit_task.assert_not_called()
    else:
        client.submit_task.assert_called_once()
    fallback.assert_not_called()
    owner = ImplementationOwner("issue", 1982)
    assert slots.active_execution_ids(owner) == ()
    rejected = submission in ("quota", "rejected") or ineligible
    released = rejected and submission != "ineligible-existing"
    assert slots.active_owners() == (() if released else (owner,)), result
    assert result.cloud_submission_not_started is rejected
    if rejected:
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert result.success is False
        github.add_comment_to_issue.assert_not_called()
        events = collector.get_snapshot(repository="owner/repo", item_type="issue", item_number=1982).events
        cleanup = [event for event in events if event.stage_id == "issue.cloud-submission-slot-release"]
        assert len(cleanup) == 1
        assert cleanup[0].facts["slot_released"] is released
        assert cleanup[0].outcome == (Outcome.COMPLETED.value if released else Outcome.DEFERRED.value)
        _mounted_detail(mock_ui, "issue", 1982)
        # Validation owns a newer execution. Navigate to the worker evidence.
        older = next(call.kwargs["on_click"] for call in mock_ui.button.call_args_list if call.kwargs.get("icon") == "arrow_downward")
        older()
        _assert_required_stage_visible(mock_ui.mermaid.return_value.classes.return_value.set_content.call_args[0][0], "Cloud submission slot cleanup")
        assert (slots.start_execution(ImplementationOwner("issue", 1983)) is not None) is released
    else:
        assert slots.start_execution(ImplementationOwner("issue", 1983)) is None
        assert slots.has_provider_sessions(owner) is (submission == "accepted")


@pytest.mark.parametrize("declaration, expected", [("Blocked-By:", Outcome.COMPLETED), ("Blocked-By: #205", Outcome.DEFERRED)])
@patch("auto_coder.dashboard.ui")
def test_standalone_dependency_gate_reaches_mounted_detail_view(mock_ui, tmp_path, declaration, expected):
    from auto_coder.automation_config import CandidateProcessingResult
    from auto_coder.implementation_slots import ImplementationSlotRepository
    from auto_coder.specification_analyzer import SpecificationAnalysisResult
    from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
    from auto_coder.util.gh_cache import GitHubClient

    issue = {
        "number": 1998,
        "id": 199800,
        "title": "Resume deferred work",
        "body": declaration + "\n\n## Objective\n\nResume eligible work.\n\n## Requirements\nREQ-001: Resume eligible work.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
        "user": {"id": 1},
        "created_at": "2020-01-01T00:00:00Z",
    }
    github = MagicMock(spec=GitHubClient)
    github.token = "test-token"
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda *_: dict(issue)
    github.get_parent_issue_details_strict.return_value = None
    github.get_direct_sub_issues_strict.return_value = []
    github.get_open_sub_issues_strict.return_value = []
    github.get_open_entities_strict.return_value = SimpleNamespace(issues=[SimpleNamespace(number=1998)])
    github.get_issue_comments_strict.return_value = []
    github.get_connected_prs.return_value = []
    github.get_parent_issue_number_strict.return_value = None
    github.get_issue_hierarchy_generation_strict.return_value = "standalone-generation"
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = [1]
    engine = AutomationEngine(github, config)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    analyzer = Mock(return_value=SpecificationAnalysisResult("READY"))
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test/model", tmp_path / "spec.json", analyzer)
    with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult("issue", 1998, issue["title"], True, ["implementation reached"])) as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config)

    analyzer.assert_called_once()
    if expected is Outcome.COMPLETED:
        assert result.success is True, result.error
        dispatch.assert_called_once()
        assert result.actions == ["implementation reached"]
    else:
        dispatch.assert_not_called()
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert result.actions == ["Deferred - unresolved sibling dependency reconciliation"]
    github.add_sub_issue_strict.assert_not_called()
    github.add_comment_to_issue.assert_not_called()
    github.remove_labels.assert_not_called()
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=1998)
    events = [event for event in snapshot.events if event.stage_id == "issue.sibling-dependency-gate"]
    assert len(events) == 1
    assert events[0].outcome == expected.value
    diagram = _mounted_detail(mock_ui, "issue", 1998)
    # The validation producer is a later, independently navigable execution
    # for this Issue. Follow-latest therefore displays its real READY result;
    # the worker's dependency-gate evidence remains available as the older
    # execution rather than being copied into the producer's scope.
    _assert_required_stage_visible(diagram, "individual validation job")
    assert "outcome: completed" in diagram

    if expected is Outcome.COMPLETED:
        # Re-enter the real worker path with the same authoritative input. The
        # lifecycle must reuse the durable decision without another analyzer
        # call, and the producer evidence must say so explicitly.
        repeated_engine = AutomationEngine(github, config)
        repeated_engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "repeated-slots.json")
        repeated_engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test/model", tmp_path / "spec.json", analyzer)
        with patch.object(
            repeated_engine,
            "_process_single_candidate_reserved",
            return_value=CandidateProcessingResult("issue", 1998, issue["title"], True, ["implementation reached"]),
        ):
            repeated = repeated_engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config)
        assert repeated.success is True
        analyzer.assert_called_once()

        issue["body"] = issue["body"].replace("Resume eligible work.", "Change the established purpose.", 1)
        local_engine = AutomationEngine(github, config)
        local_engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "local-slots.json")
        local_engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test/model", tmp_path / "spec.json", analyzer)
        local_only = local_engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config)
        assert local_only.target_outcome is ExplicitTargetOutcome.BLOCKED
        analyzer.assert_called_once()
        repeated_snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="issue", item_number=1998)
        producer_results = [event for event in repeated_snapshot.events if event.stage_id == "issue.individual-validation-job" and event.kind == EventKind.STAGE_RESULT.value]
        assert [event.facts["evaluation_source"] for event in producer_results] == ["model", "stored-decision-reuse", "local-only"]


@pytest.mark.parametrize("known_session", [True, False])
@patch("auto_coder.dashboard.ui")
def test_claude_pr_slot_admission_reaches_detail_view(mock_ui, tmp_path, known_session):
    from auto_coder.automation_config import CandidateProcessingResult
    from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

    config = AutomationConfig()
    config.PR_ALLOWLIST = [1]
    github = MagicMock()
    github.get_connected_prs.return_value = []
    github.get_issue.return_value = {"number": 1993, "state": "open"}
    github.get_issue_details.side_effect = lambda issue: issue
    github.get_pull_request.return_value = {"number": 2027, "state": "open"}
    github.get_pr_details.side_effect = lambda pr: pr
    engine = AutomationEngine(github, config)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    owner = ImplementationOwner("issue", 1993)
    assert slots.reserve(owner)
    assert slots.record_provider_session(owner, "session_recorded")
    session = "session_recorded" if known_session else "session_unknown"
    pr = {"number": 2027, "title": "Dashboard", "body": f"https://claude.ai/code/{session}", "user": {"id": 1, "login": "developer"}, "labels": []}
    with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult("pr", 2027, "Dashboard", True, ["processing reached"])) as dispatch:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("pr", pr, 0), config)
    if known_session:
        dispatch.assert_called_once()
        assert result.success is True
    else:
        dispatch.assert_not_called()
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert result.capacity_deferred is True
    assert slots.active_owners() == (owner,)
    assert slots.snapshot().normal_usage == 1
    snapshot = get_trace_collector().get_snapshot(repository="owner/repo", item_type="pr", item_number=2027)
    events = [event for event in snapshot.events if event.stage_id == "pr.implementation-admission"]
    assert len(events) == 1
    assert events[0].outcome == ("completed" if known_session else "deferred")
    assert events[0].facts["owner"] == ("issue:1993" if known_session else "pr:2027")
    diagram = _mounted_detail(mock_ui, "pr", 2027)
    _assert_required_stage_visible(diagram, "implementation admission")
    assert ("outcome: completed" if known_session else "outcome: deferred") in diagram
