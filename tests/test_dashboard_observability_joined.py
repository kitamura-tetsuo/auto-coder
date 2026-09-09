"""Issue #1948: joined production-to-dashboard observability coverage.

Issues #1945-#1947 instrumented production Issue/PR boundaries and taught the
dashboard detail view to render observed execution traces, but each of those
stages tested its own layer in isolation: #1945/#1946 read events back from
the real ``TraceCollector`` without mounting a page, and #1947's detail-view
tests fed the mounted page synthetic events written directly into the
collector rather than events produced by real processing. This module is the
"joined" stage: it drives a real production entrypoint, then feeds the
resulting real ``TraceCollector`` snapshot into the real mounted
``/detail/{item_type}/{item_number}`` page, and asserts the business outcome
independently of what the page displays (REQ-002). It also fills origin/
outcome-matrix gaps the per-component instrumentation suites left uncovered
(REQ-003, REQ-004) and adds targeted negative controls (REQ-005, REQ-007).

See ``docs/DASHBOARD_OBSERVABILITY_COVERAGE.md`` for the inventory mapping
each required production origin to its concrete test below (and to tests in
``tests/test_issue_production_instrumentation.py`` /
``tests/test_pr_production_instrumentation.py`` for origins already covered
there).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import FastAPI

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome, StaleJulesPRResult
from auto_coder.automation_engine import (
    ISSUE_PROCESSING_STAGE,
    AutomationEngine,
    _issue_content_revision,
    _ValidationPublicationStageHandler,
)
from auto_coder.dashboard import init_dashboard
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity
from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _skip_all_issues_config() -> AutomationConfig:
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


def _mount_detail_page(repo_name: str = "owner/repo"):
    """Mount the real dashboard detail page and return its callable + the mocked `ui`.

    Mirrors ``tests/test_dashboard_detail.py``'s approach: NiceGUI's ``ui`` is
    mocked (no real web server), but ``init_dashboard``, ``detail_page``, and
    every selection/rendering helper it calls (``dashboard_detail.py``) run
    for real against the real ``TraceCollector`` singleton.
    """
    captured_functions: dict = {}

    def capture_page(path):
        def decorator(func):
            captured_functions[path] = func
            return func

        return decorator

    patcher = patch("auto_coder.dashboard.ui")
    mock_ui = patcher.start()
    mock_ui.page.side_effect = capture_page
    engine = MagicMock(spec=AutomationEngine)
    init_dashboard(FastAPI(), engine, repo_name)
    detail_page = captured_functions["/detail/{item_type}/{item_number}"]
    return detail_page, mock_ui, patcher


def _rendered_mermaid(mock_ui) -> str:
    assert mock_ui.mermaid.called, "detail page did not render a diagram"
    return mock_ui.mermaid.call_args[0][0]


def _rendered_labels(mock_ui) -> list[str]:
    return [str(args[0]) for args, _ in mock_ui.label.call_args_list]


class TestJoinedProductionToView:
    """REQ-002: real production processing reaches the real mounted detail page.

    Each test here runs a real production entrypoint (no fabricated trace
    records), then mounts the real ``/detail/{item_type}/{item_number}`` page
    against the real ``TraceCollector`` snapshot that processing produced,
    and checks both the page's rendering and the business outcome -- kept
    independent so the UI and the emitter cannot share one wrong assumption
    and still pass.
    """

    def test_issue_admission_denial_reaches_detail_view_without_dispatch(self):
        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 2001, "title": "T", "body": "B", "labels": []}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)
        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED

        detail_page, mock_ui, patcher = _mount_detail_page()
        try:
            detail_page(item_type="issue", item_number=2001)
        finally:
            patcher.stop()

        mermaid = _rendered_mermaid(mock_ui)
        assert "author admission" in mermaid
        assert "outcome: skipped" in mermaid
        # No dispatch/implementation node was invented for a candidate that
        # never reached dispatch (REQ-002, mirrors REQ-003 of Issue #1945).
        assert "dispatch" not in mermaid

    def test_pr_pending_work_resumption_reaches_detail_view_as_superseded(self):
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

        detail_page, mock_ui, patcher = _mount_detail_page()
        try:
            detail_page(item_type="pr", item_number=2101)
        finally:
            patcher.stop()

        mermaid = _rendered_mermaid(mock_ui)
        assert "pending-work resume refresh" in mermaid
        assert "outcome: superseded" in mermaid
        # A superseded resumption must never be displayed as a completed
        # re-evaluation or a merge (REQ-006 of Issue #1947).
        assert "merge delivery" not in mermaid

    def test_merge_operation_resumption_reaches_detail_view_as_superseded(self):
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

        detail_page, mock_ui, patcher = _mount_detail_page()
        try:
            detail_page(item_type="pr", item_number=2201)
        finally:
            patcher.stop()

        mermaid = _rendered_mermaid(mock_ui)
        assert "merge-operation resume refresh" in mermaid
        assert "outcome: superseded" in mermaid
        assert "merge delivery" not in mermaid

    @patch("auto_coder.issue_processor.get_commit_log", return_value="No commits")
    @patch("auto_coder.issue_processor.JulesClient")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.issue_processor.render_prompt")
    def test_accepted_handoff_reaches_detail_view_without_pr_publication(self, mock_render, mock_cloud_manager_class, mock_jules_client_class, mock_get_commit_log):
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

        detail_page, mock_ui, patcher = _mount_detail_page()
        try:
            detail_page(item_type="issue", item_number=2301)
        finally:
            patcher.stop()

        mermaid = _rendered_mermaid(mock_ui)
        assert "outcome: accepted_handoff" in mermaid
        # Accepting a remote task is not PR publication or completed
        # implementation (REQ-006 of Issue #1947, REQ-007 of Issue #1948).
        assert "pr-publication" not in mermaid
        assert "outcome: completed" not in mermaid

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
    """REQ-003: production origins not yet covered by the per-component instrumentation suites."""

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
        """The concurrency-admitted adversarial-validation stage records its
        own honest BLOCKED result rather than the calling worker's outcome."""
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
    """REQ-004: outcome-matrix cases not exercised by the per-component instrumentation suites."""

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


class TestNegativeAndMutationControls:
    """REQ-005/REQ-007: the joined checks reject fabricated or coerced evidence."""

    def test_suppressed_production_emission_is_detected_not_silently_accepted(self, monkeypatch):
        """A joined coverage check that reads the collector must fail when a
        required boundary's emission is suppressed -- proving the check is
        sensitive to a missing instrumentation call, not merely to whether a
        file was touched (REQ-005: 'a fixture supplying the expected new
        stage without executing its production emission is not evidence
        against missing instrumentation' -- the converse also holds: the
        absence of that emission must fail the check)."""
        import auto_coder.automation_engine as automation_engine_module

        real_record_event = TraceCollector.record_event

        def _suppress_author_admission(self, kind, stage_id, *args, **kwargs):
            if stage_id == "issue.author-admission":
                return None
            return real_record_event(self, kind, stage_id, *args, **kwargs)

        monkeypatch.setattr(TraceCollector, "record_event", _suppress_author_admission)

        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 3201, "title": "T", "body": "B", "labels": []}, priority=0)
        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED  # business behavior is unaffected (REQ-008 of #1945)
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=3201)
        with pytest.raises(AssertionError):
            assert any(e.stage_id == "issue.author-admission" for e in snapshot.events)
        del automation_engine_module  # imported only to document the patched module

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

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_unavailable_ci_evidence_is_never_rendered_as_success_or_failure(self, mock_observe_ci, _mock_api):
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

        detail_page, mock_ui, patcher = _mount_detail_page()
        try:
            detail_page(item_type="pr", item_number=3401)
        finally:
            patcher.stop()

        mermaid = _rendered_mermaid(mock_ui)
        assert "outcome: unknown" in mermaid
        assert "outcome: true" not in mermaid
        assert "outcome: false" not in mermaid
        assert "outcome: completed" not in mermaid
        assert "outcome: failed" not in mermaid
