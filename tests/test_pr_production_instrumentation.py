"""Issue #1946: production PR-processing boundaries emit structured traces.

These tests exercise real production entrypoints (``AutomationEngine.
_process_single_candidate_unified``, ``_PrProcessingStageHandler``,
``_MergeOperationResumeHandler``, ``util.github_action._check_github_actions_status``)
rather than fabricating trace records directly, per this Issue's explicit
contract that a recorder helper exercised only by tests does not satisfy the
scope. GitHub/provider responses are controlled through small scripted
fakes; the diagnostic evidence is read back from the real ``TraceCollector``
singleton.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import (
    PR_PROCESSING_STAGE,
    AutomationEngine,
    _MergeOperationResumeHandler,
    _PrProcessingStageHandler,
)
from auto_coder.ci_observation import (
    CheckExecutionIdentity,
    CheckObservation,
    CIConclusion,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
)
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity
from auto_coder.merge_operation_state import EffectName, MergeOperation, MergeOperationIdentity, OperationStatus


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _no_prs_allowed_config() -> AutomationConfig:
    config = AutomationConfig()
    config.PR_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


class TestPrAdmissionGateVisible:
    """AS-006: PR executions reuse the generic execution-scope wrapper, and a
    pre-dispatch gate is visible with no invented later stages."""

    def test_author_disallowed_pr_records_skip_without_dispatch(self):
        config = _no_prs_allowed_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="pr", data={"number": 5001, "title": "T", "body": "B", "labels": [], "head": {"sha": "a" * 40}}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5001)
        kinds = [(e.kind, e.stage_id, e.outcome) for e in snapshot.events]
        assert (EventKind.EXECUTION_STARTED.value, "pr.execution", None) in kinds
        assert (EventKind.STAGE_RESULT.value, "pr.author-admission", Outcome.SKIPPED.value) in kinds
        assert (EventKind.EXECUTION_FINISHED.value, "execution", Outcome.SKIPPED.value) in kinds
        # No later CI/merge/repair stage was ever reached (REQ-003: a path
        # stopped at an earlier boundary must not claim later stages ran).
        assert not any(e.stage_id.startswith("pr.ci-") or e.stage_id.startswith("pr.merge-") for e in snapshot.events)

        started = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value)
        assert started.origin == "worker"
        assert started.repository == "owner/repo"

    def test_dependency_bot_pr_admission_records_skip_without_dispatch(self):
        """Issue #1995: the common dependency-bot gate is visible with no invented later stages."""
        config = AutomationConfig()  # defaults: IGNORE_DEPENDABOT_PRS=False, AUTO_MERGE_DEPENDABOT_PRS=True
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(
            type="pr",
            data={
                "number": 5318,
                "title": "Bump some-package",
                "body": "",
                "state": "open",
                "mergeable": False,
                "labels": [],
                "head": {"ref": "dependabot/npm_and_yarn/some-package-1.0.0", "sha": "a" * 40},
                "author": "dependabot[bot]",
                "user": {"login": "dependabot[bot]"},
            },
            priority=0,
        )

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5318)
        kinds = [(e.kind, e.stage_id, e.outcome) for e in snapshot.events]
        assert (EventKind.EXECUTION_STARTED.value, "pr.execution", None) in kinds
        assert (EventKind.STAGE_RESULT.value, "pr.dependency-bot-admission", Outcome.SKIPPED.value) in kinds
        assert (EventKind.EXECUTION_FINISHED.value, "execution", Outcome.SKIPPED.value) in kinds
        # No later CI/merge/repair stage was ever reached: a refusal at this
        # gate must not transiently acquire capacity before releasing it.
        assert not any(e.stage_id.startswith("pr.ci-") or e.stage_id.startswith("pr.merge-") for e in snapshot.events)


class TestPrResumptionSupersededHead:
    """AS-002/AS-007: a changed head since deferral is visible as superseded,
    not fabricated as a re-evaluation or a merge."""

    def test_pending_work_resumption_records_superseded_on_changed_head(self):
        github = MagicMock()
        github.get_pull_request_metadata_strict.return_value = {"raw": True}
        github.get_pr_details.return_value = {"number": 5101, "head": {"sha": "new-head"}}

        config = AutomationConfig()
        engine = AutomationEngine(github, config)
        handler = _PrProcessingStageHandler(engine, "owner/repo")
        obligation = PendingObligation(
            WorkIdentity("owner/repo", "pr:5101", PR_PROCESSING_STAGE, "old-head"),
            PendingReason.THROTTLED,
            0.0,
            ("authoritative-refresh", "pr-processing"),
        )

        outcome = handler.dispatch(obligation)

        assert outcome.superseded is True
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5101)
        superseded_events = [e for e in snapshot.events if e.stage_id == "pr.pending-work-resume-refresh"]
        assert len(superseded_events) == 1
        assert superseded_events[0].outcome == Outcome.SUPERSEDED.value
        assert superseded_events[0].facts["expected_head"] == "old-head"
        assert superseded_events[0].facts["current_head"] == "new-head"
        # The resumption gets its own fresh execution identity (REQ-001) even
        # though it never reaches the ordinary processing entrypoint, and
        # that execution itself is honestly finished as superseded rather
        # than a fabricated merge/re-evaluation (REQ-007).
        started_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started_events) == 1
        assert started_events[0].origin == "pr-pending-work-resumption"
        finished_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value]
        assert len(finished_events) == 1
        assert finished_events[0].outcome == Outcome.SUPERSEDED.value


class TestMergeOperationResumeSupersededHead:
    """AS-004/AS-007: a durable merge-operation resumption discards a stale head as superseded."""

    def test_merge_operation_resumption_records_superseded_on_changed_head(self):
        github = MagicMock()
        github.get_pull_request_metadata_strict.return_value = {"raw": True}
        github.get_pr_details.return_value = {"number": 5201, "head": {"sha": "newer-head"}}

        config = AutomationConfig()
        engine = AutomationEngine(github, config)
        handler = _MergeOperationResumeHandler(engine, "owner/repo")
        identity = MergeOperationIdentity("https://api.github.com", "owner/repo", 5201)
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
            mock_store = MagicMock()
            mock_store_factory.return_value = mock_store
            handler(operation)
            mock_store.supersede.assert_called_once_with(identity)

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5201)
        superseded_events = [e for e in snapshot.events if e.stage_id == "pr.merge-operation-resume-refresh"]
        assert len(superseded_events) == 1
        assert superseded_events[0].outcome == Outcome.SUPERSEDED.value
        assert superseded_events[0].facts["expected_head"] == "stale-head"
        assert superseded_events[0].facts["current_head"] == "newer-head"
        started_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started_events) == 1
        assert started_events[0].origin == "merge-operation-resumption"
        finished_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value]
        assert len(finished_events) == 1
        assert finished_events[0].outcome == Outcome.SUPERSEDED.value


class TestCiObservationAvailabilityIsNotABoolean:
    """AS-001: CI observation availability is preserved distinctly, not collapsed to a boolean."""

    def _snapshot(self, availability: ObservationAvailability, **kwargs) -> CIObservationSnapshot:
        subject = ObservationSubject("https://api.github.com", "owner/repo", 5301, "a" * 40)
        request = ObservationRequest("github-actions", "checks+workflows")
        return CIObservationSnapshot(subject, request, "cycle-1", 0, availability, **kwargs)

    def _pr_data(self) -> dict:
        return {"number": 5301, "head": {"sha": "a" * 40}}

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_unavailable_observation_is_not_reported_as_failure(self, mock_observe_ci, _mock_api):
        from auto_coder.util.github_action import _check_github_actions_status

        mock_observe_ci.return_value = self._snapshot(ObservationAvailability.UNAVAILABLE, unavailable_reason="GitHub CI request failed (unavailable)")
        github_client = MagicMock(token="tok")

        with get_trace_collector().start_execution("owner/repo", "pr", 5301, origin="worker"):
            result = _check_github_actions_status("owner/repo", self._pr_data(), AutomationConfig(), github_client)

        assert result.success is False
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5301)
        events = [e for e in snapshot.events if e.stage_id == "pr.ci-observation"]
        assert len(events) == 1
        # Unavailable evidence is neither a failure nor a pass: it stays an
        # explicit UNKNOWN outcome (REQ-004), and the availability value
        # itself is preserved as a fact rather than being discarded.
        assert events[0].outcome == Outcome.UNKNOWN.value
        assert events[0].facts["availability"] == "unavailable"

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_known_success_observation_records_completed(self, mock_observe_ci, _mock_api):
        from auto_coder.util.github_action import _check_github_actions_status

        fact = CheckObservation(CheckExecutionIdentity("app-1", "check-1"), CIConclusion.SUCCESS, "build")
        mock_observe_ci.return_value = self._snapshot(ObservationAvailability.KNOWN, facts=(fact,))
        github_client = MagicMock(token="tok")

        with get_trace_collector().start_execution("owner/repo", "pr", 5301, origin="worker"):
            result = _check_github_actions_status("owner/repo", self._pr_data(), AutomationConfig(), github_client)

        assert result.success is True
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5301)
        events = [e for e in snapshot.events if e.stage_id == "pr.ci-observation"]
        assert len(events) == 1
        assert events[0].outcome == Outcome.COMPLETED.value
        assert events[0].facts["availability"] == "known"

    @patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock())
    @patch("auto_coder.util.github_action.observe_ci")
    def test_sequence_known_then_unavailable_then_known_does_not_overwrite(self, mock_observe_ci, _mock_api):
        """AS-001: known -> unavailable -> known again; the middle read never
        claims a new failure or reuses the earlier/later success as its own."""
        from auto_coder.util.github_action import _check_github_actions_status

        fact = CheckObservation(CheckExecutionIdentity("app-1", "check-1"), CIConclusion.SUCCESS, "build")
        sequence = [
            self._snapshot(ObservationAvailability.KNOWN, facts=(fact,)),
            self._snapshot(ObservationAvailability.UNAVAILABLE, unavailable_reason="throttled"),
            self._snapshot(ObservationAvailability.KNOWN, facts=(fact,)),
        ]
        mock_observe_ci.side_effect = sequence
        github_client = MagicMock(token="tok")

        with get_trace_collector().start_execution("owner/repo", "pr", 5301, origin="worker"):
            for _ in sequence:
                _check_github_actions_status("owner/repo", self._pr_data(), AutomationConfig(), github_client)

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5301)
        events = [e for e in snapshot.events if e.stage_id == "pr.ci-observation"]
        assert [e.outcome for e in events] == [Outcome.COMPLETED.value, Outcome.UNKNOWN.value, Outcome.COMPLETED.value]


class TestRecorderFailureIsNonInterfering:
    """AS-006/REQ-008: an injected diagnostic-recorder failure never changes the business outcome."""

    def test_author_gate_outcome_unaffected_by_recorder_failure(self, monkeypatch):
        class _ExplodingCollector:
            def start_execution(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

            def record_event(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

        monkeypatch.setattr("auto_coder.automation_engine.get_trace_collector", lambda: _ExplodingCollector())

        config = _no_prs_allowed_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="pr", data={"number": 5401, "title": "T", "body": "B", "labels": [], "head": {"sha": "a" * 40}}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED

    def test_ci_observation_recording_failure_does_not_block_status_check(self, monkeypatch):
        from auto_coder.util.github_action import _check_github_actions_status

        class _ExplodingCollector:
            def record_event(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

        monkeypatch.setattr("auto_coder.util.github_action.get_trace_collector", lambda: _ExplodingCollector())

        subject = ObservationSubject("https://api.github.com", "owner/repo", 5501, "a" * 40)
        request = ObservationRequest("github-actions", "checks+workflows")
        fact = CheckObservation(CheckExecutionIdentity("app-1", "check-1"), CIConclusion.SUCCESS, "build")
        snapshot = CIObservationSnapshot(subject, request, "cycle-1", 0, ObservationAvailability.KNOWN, facts=(fact,))

        with (
            patch("auto_coder.util.github_action.get_ghapi_client", return_value=MagicMock()),
            patch("auto_coder.util.github_action.observe_ci", return_value=snapshot),
        ):
            github_client = MagicMock(token="tok")
            result = _check_github_actions_status("owner/repo", {"number": 5501, "head": {"sha": "a" * 40}}, AutomationConfig(), github_client)

        assert result.success is True
