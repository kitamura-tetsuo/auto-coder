"""Issue #1945: production Issue-processing boundaries emit structured traces.

These tests exercise the real production entrypoints (``AutomationEngine.
_process_single_candidate`` / ``_process_single_candidate_unified``,
``_IssueProcessingStageHandler``, ``issue_processor`` dispatch functions)
rather than fabricating trace records, per this Issue's explicit contract
that a recorder helper exercised only by tests does not satisfy the scope.
GitHub/provider responses are controlled through small scripted fakes; the
diagnostic evidence is read back from the real ``TraceCollector`` singleton.
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import (
    ISSUE_PROCESSING_STAGE,
    AutomationEngine,
    _issue_content_revision,
    _IssueProcessingStageHandler,
)
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _skip_all_issues_config() -> AutomationConfig:
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


class _FakeIssueGithub:
    """A minimal stand-in for the GitHubClient Issue-snapshot adapter methods."""

    def __init__(self, responses):
        self._responses = {number: list(values) for number, values in responses.items()}
        self.calls: list[tuple] = []

    def get_issue_dispatch_snapshot_strict(self, repo_name, issue_number):
        self.calls.append(("get_issue_dispatch_snapshot_strict", repo_name, issue_number))
        value = self._responses[issue_number].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_direct_sub_issues_strict(self, repo_name, issue_number):
        return []


class TestPreAdmissionGateVisible:
    """AS-001: a pre-worker gate is visible with no invented later stages."""

    def test_author_disallowed_issue_records_skip_without_dispatch(self):
        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 501, "title": "T", "body": "B", "labels": []}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=501)
        kinds = [(e.kind, e.stage_id, e.outcome) for e in snapshot.events]
        assert (EventKind.EXECUTION_STARTED.value, "issue.execution", None) in kinds
        assert (EventKind.STAGE_RESULT.value, "issue.author-admission", Outcome.SKIPPED.value) in kinds
        assert (EventKind.EXECUTION_FINISHED.value, "execution", Outcome.SKIPPED.value) in kinds
        # No dispatch/implementation stage was ever reached (REQ-003: reached
        # work that returns early must not invent later stages).
        assert not any(e.stage_id.startswith("issue.dispatch") for e in snapshot.events)

        started = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value)
        assert started.origin == "worker"
        assert started.repository == "owner/repo"


class TestDispatchRouteRecorded:
    """AS-003: every actual dispatch route is recorded with its actual origin."""

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    def test_difficult_label_routes_to_high_score_cloud(self, mock_jules_mode, mock_high_score_cloud, mock_label_manager):
        mock_high_score_cloud.return_value = ["High score cloud action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
            "number": number,
            "body": "",
            "labels": [{"name": "implementation-ready"}, {"name": "difficult"}],
        }
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)
        candidate = Candidate(type="issue", priority=100, data={"number": 601, "title": "Difficult problem", "labels": [{"name": "difficult"}]})

        result = engine._process_single_candidate_unified("owner/repo", candidate, config, jules_mode=True)

        assert result.success is True
        mock_jules_mode.assert_not_called()

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=601)
        route_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch-route"]
        assert len(route_events) == 1
        assert route_events[0].outcome == Outcome.COMPLETED.value
        assert route_events[0].facts["route"] == "high-score-cloud"

        finished = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value)
        assert finished.outcome == Outcome.COMPLETED.value

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.issue_processor._process_issue_high_score_cloud")
    @patch("auto_coder.issue_processor._process_issue_jules_mode")
    def test_non_difficult_issue_routes_to_cloud(self, mock_jules_mode, mock_high_score_cloud, mock_label_manager):
        mock_jules_mode.return_value = ["Jules action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)
        candidate = Candidate(type="issue", priority=100, data={"number": 602, "title": "Simple bug", "labels": [{"name": "bug"}]})

        result = engine._process_single_candidate_unified("owner/repo", candidate, config, jules_mode=True)

        assert result.success is True
        mock_high_score_cloud.assert_not_called()

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=602)
        route_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch-route"]
        assert len(route_events) == 1
        assert route_events[0].facts["route"] == "cloud"

    @patch("auto_coder.automation_engine.LabelManager")
    @patch("auto_coder.automation_engine.AutomationEngine._take_issue_actions")
    def test_local_mode_records_local_route(self, mock_take_actions, mock_label_manager):
        mock_take_actions.return_value = ["Local action"]
        mock_ctx = MagicMock()
        mock_ctx.__bool__.return_value = True
        mock_label_manager.return_value.__enter__.return_value = mock_ctx

        mock_github = MagicMock()
        mock_github.get_item_type_strict.return_value = "issue"
        mock_github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "body": "", "labels": [{"name": "implementation-ready"}]}
        mock_github.get_all_sub_issues.return_value = []

        config = AutomationConfig()
        engine = AutomationEngine(mock_github, config)
        candidate = Candidate(type="issue", priority=100, data={"number": 603, "title": "Simple bug", "labels": [{"name": "bug"}]})

        result = engine._process_single_candidate_unified("owner/repo", candidate, config, jules_mode=False)

        assert result.success is True
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=603)
        route_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch-route"]
        assert len(route_events) == 1
        assert route_events[0].facts["route"] == "local"


class TestDurableResumptionCreatesAnotherExecution:
    """AS-004: a durably deferred evaluation resumes with a fresh execution identity."""

    def test_resumption_origin_and_identity_differ_from_a_fresh_evaluation(self):
        config = _skip_all_issues_config()

        # A fresh top-level evaluation of the same Issue (worker origin).
        engine_a = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 701, "title": "T", "body": "B", "labels": []}, priority=0)
        engine_a._process_single_candidate_unified("owner/repo", candidate, config)

        # A resumption of a durably deferred obligation for the same Issue,
        # through the real production stage handler.
        fresh_issue = {"number": 701, "title": "T", "body": "B", "labels": [], "user": {"id": 999}}
        github = _FakeIssueGithub({701: [fresh_issue]})
        engine_b = AutomationEngine(github, config)
        handler = _IssueProcessingStageHandler(engine_b, "owner/repo")
        revision = _issue_content_revision(fresh_issue)
        obligation = PendingObligation(WorkIdentity("owner/repo", "issue:701", ISSUE_PROCESSING_STAGE, revision), PendingReason.THROTTLED, 0.0, ("authoritative-refresh", "issue-processing"))

        handler.dispatch(obligation)

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=701)
        started_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started_events) == 2
        origins = {e.origin for e in started_events}
        assert origins == {"worker", "issue-pending-work-resumption"}
        execution_ids = {e.execution_id for e in started_events}
        assert len(execution_ids) == 2, "resumption must not reuse the original execution identity"


class TestValidationJobsGetTheirOwnExecutionIdentity:
    """AS-002: async validation jobs get their own execution identity."""

    def test_traced_validation_job_does_not_borrow_ambient_worker_scope(self):
        collector = get_trace_collector()

        class _Decision:
            verdict = "READY"

        with collector.start_execution("owner/repo", "issue", 801, origin="worker") as ambient_handle:
            ambient_handle.set_outcome(Outcome.DEFERRED)
            decision = AutomationEngine._traced_validation_job(
                "owner/repo",
                900,
                "issue.decomposition-validation-job",
                "issue#900 decomposition validation job",
                {"parent_number": 900, "member_issue_numbers": [901, 902]},
                lambda: _Decision(),
            )

        assert decision.verdict == "READY"
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=900)
        job_started = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value)
        assert job_started.execution_id != ambient_handle.scope.execution_id
        assert job_started.item_number == 900
        job_result = next(e for e in snapshot.events if e.kind == EventKind.STAGE_RESULT.value)
        assert job_result.outcome == Outcome.COMPLETED.value
        assert job_result.facts["member_issue_numbers"] == [901, 902]
        assert job_result.facts["verdict"] == "READY"

    def test_disabled_decomposition_validation_is_distinguishable_from_blocked(self):
        config = AutomationConfig()
        engine = AutomationEngine(MagicMock(), config)
        engine._is_issue_decomposition_validation_enabled = Mock(return_value=False)  # type: ignore[method-assign]
        engine._is_issue_specification_validation_enabled = Mock(return_value=False)  # type: ignore[method-assign]
        parent = {"number": 950, "title": "Parent", "body": ""}
        children = [{"number": 951, "title": "Child", "body": ""}]

        with get_trace_collector().start_execution("owner/repo", "issue", 950, origin="worker"):
            engine._schedule_parent_validations("owner/repo", (parent, children), config)

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=950)
        job_events = [e for e in snapshot.events if e.stage_id == "issue.decomposition-validation-job"]
        assert len(job_events) == 1
        assert job_events[0].outcome == Outcome.SKIPPED.value
        assert job_events[0].kind == EventKind.STAGE_RESULT.value


class TestNoFabricatedPrPublication:
    """AS-005: a rejected/failed PR-publication attempt is never reported as completed."""

    def test_validation_failure_records_blocked_not_completed(self):
        from auto_coder.issue_processor import _create_pr_for_issue

        github_client = MagicMock()
        config = AutomationConfig()

        with (
            patch("auto_coder.issue_processor.validate_issue_references", side_effect=ValueError("bad reference")),
            get_trace_collector().start_execution("owner/repo", "issue", 1001, origin="worker"),
        ):
            message = _create_pr_for_issue(
                repo_name="owner/repo",
                issue_data={"number": 1001, "title": "T", "body": "B"},
                work_branch="issue-1001",
                base_branch="main",
                llm_response="did stuff",
                github_client=github_client,
                config=config,
            )

        assert message.startswith("Validation failed")
        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=1001)
        pub_events = [e for e in snapshot.events if e.stage_id == "issue.pr-publication"]
        assert len(pub_events) == 1
        assert pub_events[0].outcome == Outcome.BLOCKED.value


class TestDispatchOutcomesAreHonest:
    """AS-003: cloud submissions distinguish accepted handoff from failure."""

    @patch("auto_coder.issue_processor.get_commit_log")
    @patch("auto_coder.issue_processor.JulesClient")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.issue_processor.render_prompt")
    def test_jules_dispatch_records_accepted_handoff(self, mock_render, mock_cloud_manager_class, mock_jules_client_class, mock_get_commit_log):
        from auto_coder.issue_processor import _process_issue_jules_mode

        mock_jules_client = Mock()
        mock_jules_client.start_session.return_value = "session_123"
        mock_jules_client_class.return_value = mock_jules_client
        mock_cloud_manager = Mock()
        mock_cloud_manager.add_session.return_value = True
        mock_cloud_manager_class.return_value = mock_cloud_manager
        mock_get_commit_log.return_value = "No commits"

        mock_github_client = Mock()
        mock_config = Mock()
        mock_config.MAIN_BRANCH = "main"
        issue_data = {"number": 1101, "title": "T", "body": "B"}

        with get_trace_collector().start_execution("owner/repo", "issue", 1101, origin="worker"):
            _process_issue_jules_mode(repo_name="owner/repo", issue_data=issue_data, config=mock_config, github_client=mock_github_client)

        snapshot = get_trace_collector().get_snapshot(item_type="issue", item_number=1101)
        dispatch_events = [e for e in snapshot.events if e.stage_id == "issue.dispatch.jules"]
        assert len(dispatch_events) == 1
        assert dispatch_events[0].outcome == Outcome.ACCEPTED_HANDOFF.value
        assert dispatch_events[0].facts["backend"] == "jules"
        assert dispatch_events[0].facts["session_id"] == "session_123"


class TestRecorderFailureIsNonInterfering:
    """AS-006: an injected diagnostic-recorder failure never changes the business outcome."""

    def test_author_gate_outcome_unaffected_by_recorder_failure(self, monkeypatch):
        class _ExplodingCollector:
            def start_execution(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

            def record_event(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

        monkeypatch.setattr("auto_coder.automation_engine.get_trace_collector", lambda: _ExplodingCollector())

        config = _skip_all_issues_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="issue", data={"number": 1201, "title": "T", "body": "B", "labels": []}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED

    def test_dispatch_recording_failure_does_not_block_business_dispatch(self, monkeypatch):
        from auto_coder.issue_processor import _process_issue_jules_mode

        class _ExplodingCollector:
            def record_event(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

        monkeypatch.setattr("auto_coder.issue_processor.get_trace_collector", lambda: _ExplodingCollector())

        with (
            patch("auto_coder.issue_processor.get_commit_log", return_value="No commits"),
            patch("auto_coder.issue_processor.JulesClient") as mock_jules_client_class,
            patch("auto_coder.issue_processor.CloudManager") as mock_cloud_manager_class,
            patch("auto_coder.issue_processor.render_prompt"),
        ):
            mock_jules_client = Mock()
            mock_jules_client.start_session.return_value = "session_456"
            mock_jules_client_class.return_value = mock_jules_client
            mock_cloud_manager = Mock()
            mock_cloud_manager.add_session.return_value = True
            mock_cloud_manager_class.return_value = mock_cloud_manager

            mock_github_client = Mock()
            mock_config = Mock()
            mock_config.MAIN_BRANCH = "main"
            issue_data = {"number": 1202, "title": "T", "body": "B"}

            actions = _process_issue_jules_mode(repo_name="owner/repo", issue_data=issue_data, config=mock_config, github_client=mock_github_client)

        assert any("Started Jules session" in action for action in actions)
