"""Issue #1946: production PR-processing boundaries emit structured traces.

These tests exercise real production entrypoints (``AutomationEngine.
_process_single_candidate_unified``, ``_PrProcessingStageHandler``,
``_MergeOperationResumeHandler``, ``pr_processor._merge_pr``,
``github_ci_observer.observe_ci``) rather than fabricating trace records,
mirroring the contract established for Issue processing in
``tests/test_issue_production_instrumentation.py`` (Issue #1945). GitHub/
provider responses are controlled through small scripted fakes; the
diagnostic evidence is read back from the real ``TraceCollector`` singleton.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine, _MergeOperationResumeHandler, _PrProcessingStageHandler
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity
from auto_coder.pr_processor import PR_PROCESSING_STAGE
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
)


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


def _skip_all_prs_config() -> AutomationConfig:
    config = AutomationConfig()
    config.PR_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


class _FakePrGithub:
    """A minimal stand-in for the GitHubClient PR-snapshot adapter methods."""

    def __init__(self, responses):
        self._responses = {number: list(values) for number, values in responses.items()}

    def get_pull_request_metadata_strict(self, repo_name, pr_number):
        value = self._responses[pr_number].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_pr_details(self, raw_pr):
        return raw_pr


class TestPRAuthorAdmissionRecorded:
    """REQ-001/REQ-003: a pre-worker PR gate is visible with no invented later stages."""

    def test_author_disallowed_pr_records_skip_without_dispatch(self):
        config = _skip_all_prs_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="pr", data={"number": 5001, "title": "T", "body": "B", "labels": [], "head": {"sha": "a" * 40}}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5001)
        kinds = [(e.kind, e.stage_id, e.outcome) for e in snapshot.events]
        assert (EventKind.EXECUTION_STARTED.value, "pr.execution", None) in kinds
        assert (EventKind.STAGE_RESULT.value, "pr.author-admission", Outcome.SKIPPED.value) in kinds
        assert (EventKind.EXECUTION_FINISHED.value, "execution", Outcome.SKIPPED.value) in kinds
        # No dispatch stage was ever reached (REQ-003: a path stopped at an
        # earlier boundary must not claim later stages ran).
        assert not any(e.stage_id.startswith("pr.dispatch") for e in snapshot.events)

        started = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value)
        assert started.origin == "worker"


class TestDurableResumptionCreatesAnotherExecution:
    """AS-004/AS-006: a durably deferred PR evaluation resumes with a fresh execution identity."""

    def test_resumption_origin_and_identity_differ_from_a_fresh_evaluation(self):
        config = _skip_all_prs_config()

        # A fresh top-level evaluation of the same PR (worker origin).
        engine_a = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="pr", data={"number": 5101, "title": "T", "body": "B", "labels": [], "head": {"sha": "a" * 40}}, priority=0)
        engine_a._process_single_candidate_unified("owner/repo", candidate, config)

        # A resumption of a durably deferred obligation for the same PR,
        # through the real production stage handler.
        head_sha = "a" * 40
        fresh_pr = {"number": 5101, "title": "T", "body": "B", "labels": [], "head": {"sha": head_sha}, "user": {"id": 999}}
        github = _FakePrGithub({5101: [fresh_pr]})
        engine_b = AutomationEngine(github, config)
        handler = _PrProcessingStageHandler(engine_b, "owner/repo")
        obligation = PendingObligation(WorkIdentity("owner/repo", "pr:5101", PR_PROCESSING_STAGE, head_sha), PendingReason.THROTTLED, 0.0, ("authoritative-refresh", "pr-processing"))

        handler.dispatch(obligation)

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5101)
        started_events = [e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value]
        assert len(started_events) == 2
        origins = {e.origin for e in started_events}
        assert origins == {"worker", "pr-pending-work-resumption"}
        execution_ids = {e.execution_id for e in started_events}
        assert len(execution_ids) == 2, "resumption must not reuse the original execution identity"


class TestPendingWorkResumptionGatesAreVisible:
    """REQ-001/REQ-007: strict-refresh failures and superseded-head exits before common processing are their own diagnostic evaluation."""

    def test_changed_head_is_recorded_as_superseded_not_a_fresh_processing_run(self):
        config = AutomationConfig()
        current_pr = {"number": 5201, "head": {"sha": "new" + "0" * 37}}
        github = _FakePrGithub({5201: [current_pr]})
        engine = AutomationEngine(github, config)
        handler = _PrProcessingStageHandler(engine, "owner/repo")
        obligation = PendingObligation(WorkIdentity("owner/repo", "pr:5201", PR_PROCESSING_STAGE, "old" + "0" * 37), PendingReason.THROTTLED, 0.0, ())

        outcome = handler.dispatch(obligation)

        assert outcome.superseded is True
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5201)
        kinds = [e.kind for e in snapshot.events]
        assert kinds == [EventKind.EXECUTION_STARTED.value, EventKind.STAGE_RESULT.value, EventKind.EXECUTION_FINISHED.value]
        assert snapshot.events[0].origin == "pr-pending-work-resumption"
        finished = snapshot.events[-1]
        assert finished.outcome == Outcome.SUPERSEDED.value
        # No later processing stage was invented for a resumption stopped at
        # the strict head-refresh gate (REQ-001, REQ-007).
        assert not any(e.stage_id.startswith("pr.dispatch") for e in snapshot.events)

    def test_metadata_refresh_failure_is_recorded_as_failed(self):
        config = AutomationConfig()
        error = GitHubRequestError(
            GitHubRequestOutcome(
                context=GitHubRequestContext(operation_id="op", attempt_id="attempt", subsystem="test", api_origin="https://api.github.com", method="GET", kind="read", endpoint_template="/pulls/{n}"),
                status=500,
                classification=GitHubApiOutcome.REMOTE_ERROR,
                provenance=RequestProvenance.NETWORK,
                delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
                metadata=GitHubResponseMetadata(),
                elapsed_ms=1.0,
                message="server error",
            )
        )
        github = _FakePrGithub({5202: [error]})
        engine = AutomationEngine(github, config)
        handler = _PrProcessingStageHandler(engine, "owner/repo")
        obligation = PendingObligation(WorkIdentity("owner/repo", "pr:5202", PR_PROCESSING_STAGE, ""), PendingReason.THROTTLED, 0.0, ())

        outcome = handler.dispatch(obligation)

        assert outcome.error is error
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5202)
        finished = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value)
        assert finished.outcome == Outcome.FAILED.value


class TestMergeOperationResumptionSupersededIsVisible:
    """REQ-007: a changed-head merge-operation resumption is visible as superseded, not a merge."""

    def test_changed_head_records_superseded_and_calls_store_supersede(self):
        from auto_coder.merge_operation_state import MergeOperation, MergeOperationIdentity, OperationStatus

        config = AutomationConfig()
        identity = MergeOperationIdentity("https://api.github.com", "owner/repo", 5301)
        operation = MergeOperation(
            identity=identity,
            expected_head_sha="old" + "0" * 37,
            merge_method="squash",
            approval_credential_role="",
            reviewer_identity="",
            generation=1,
            status=OperationStatus.WAITING,
            resume_reason="",
            not_before=0.0,
        )
        current_pr = {"number": 5301, "head": {"sha": "new" + "0" * 37}}
        github = _FakePrGithub({5301: [current_pr]})
        engine = AutomationEngine(github, config)
        handler = _MergeOperationResumeHandler(engine, "owner/repo")

        mock_store = MagicMock()
        with patch("auto_coder.merge_operation_state.get_merge_operation_store", return_value=mock_store):
            handler(operation)

        mock_store.supersede.assert_called_once_with(identity)
        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5301)
        finished = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value)
        assert finished.outcome == Outcome.SUPERSEDED.value
        started = next(e for e in snapshot.events if e.kind == EventKind.EXECUTION_STARTED.value)
        assert started.origin == "merge-operation-resumption"


class TestMergeDeliveryRecordsHonestOutcome:
    """AS-004/AS-005: merge success and cleanup require their own confirmed evidence."""

    def _patch_merge_operation_store(self, tmp_path):
        from auto_coder.merge_operation_state import MergeOperationStore

        store = MergeOperationStore(db_path=tmp_path / "merge_ops.db")
        return patch("auto_coder.merge_operation_state.get_merge_operation_store", return_value=store)

    @patch("auto_coder.merge_operation_adapter.get_ghapi_client")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.pr_processor._get_allowed_merge_methods")
    def test_confirmed_merge_records_completed_delivery_and_cleanup(self, mock_get_allowed_methods, mock_github_client_class, mock_get_ghapi_client, mock_get_ghapi_client_adapter, tmp_path):
        from auto_coder.automation_config import AutomationConfig as _Config
        from auto_coder.pr_processor import _merge_pr

        config = _Config()
        config.MERGE_METHOD = "--squash"

        mock_instance = MagicMock()
        mock_instance.token = "fake-token"
        mock_github_client_class.get_instance.return_value = mock_instance

        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api
        mock_get_ghapi_client_adapter.return_value = mock_api

        head_sha = "e" * 40
        mock_pr_info = {"number": 5401, "user": {"login": "some-developer"}, "head": {"ref": "feature/x", "sha": head_sha}}
        mock_api.pulls.get.return_value = mock_pr_info
        mock_api.pulls.merge.return_value = {"merged": True, "sha": "mergedsha"}

        with (
            self._patch_merge_operation_store(tmp_path),
            patch("auto_coder.pr_processor._close_linked_issues") as mock_close,
            patch("auto_coder.pr_processor._archive_jules_session") as mock_archive,
            get_trace_collector().start_execution("owner/repo", "pr", 5401, origin="worker"),
        ):
            result = _merge_pr("owner/repo", 5401, {}, config)

        assert result is True
        mock_close.assert_called_once()
        mock_archive.assert_called_once()

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5401)
        merge_events = [e for e in snapshot.events if e.stage_id == "pr.merge-delivery"]
        assert len(merge_events) == 1
        assert merge_events[0].outcome == Outcome.COMPLETED.value
        cleanup_events = [e for e in snapshot.events if e.stage_id == "pr.cleanup"]
        assert len(cleanup_events) == 1
        assert cleanup_events[0].outcome == Outcome.COMPLETED.value

    @patch("auto_coder.merge_operation_adapter.get_ghapi_client")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("auto_coder.pr_processor.GitHubClient")
    @patch("auto_coder.pr_processor._get_allowed_merge_methods")
    def test_dependabot_conflict_records_skipped_not_a_failure_or_merge(self, mock_get_allowed_methods, mock_github_client_class, mock_get_ghapi_client, mock_get_ghapi_client_adapter, tmp_path):
        from auto_coder.automation_config import AutomationConfig as _Config
        from auto_coder.pr_processor import _merge_pr

        config = _Config()
        config.MERGE_METHOD = "--squash"

        mock_instance = MagicMock()
        mock_instance.token = "fake-token"
        mock_github_client_class.get_instance.return_value = mock_instance

        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api
        mock_get_ghapi_client_adapter.return_value = mock_api

        head_sha = "f" * 40
        mock_api.pulls.get.return_value = {
            "number": 5402,
            "user": {"login": "dependabot[bot]"},
            "head": {"ref": "dependabot/pip/requests-2.32.0", "sha": head_sha},
            "mergeable": False,
        }
        mock_api.pulls.merge.side_effect = GitHubRequestError(
            GitHubRequestOutcome(
                context=GitHubRequestContext(operation_id="op", attempt_id="attempt", subsystem="test", api_origin="https://api.github.com", method="PUT", kind="mutation", endpoint_template="/pulls/{n}/merge"),
                status=405,
                classification=GitHubApiOutcome.REMOTE_ERROR,
                provenance=RequestProvenance.NETWORK,
                delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
                metadata=GitHubResponseMetadata(),
                elapsed_ms=1.0,
                message="Method Not Allowed",
            )
        )
        mock_get_allowed_methods.return_value = []

        with (
            self._patch_merge_operation_store(tmp_path),
            patch("auto_coder.pr_processor._resolve_pr_merge_conflicts") as mock_resolve,
            patch("auto_coder.pr_processor._close_linked_issues"),
            patch("auto_coder.pr_processor._archive_jules_session"),
            # Force needs_approval=False deterministically so this reaches
            # the dependency-bot merge-conflict branch under test, rather
            # than an indeterminate auto-approval from an unmocked reviewer
            # identity (a separate, already-covered pr.approval-delivery path).
            patch("auto_coder.pr_processor.resolve_reviewer_app_identity", side_effect=RuntimeError("no reviewer identity configured")),
            get_trace_collector().start_execution("owner/repo", "pr", 5402, origin="worker"),
        ):
            result = _merge_pr("owner/repo", 5402, {}, config)

        assert result is False
        mock_resolve.assert_not_called()

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5402)
        merge_events = [e for e in snapshot.events if e.stage_id == "pr.merge-delivery"]
        assert len(merge_events) == 1
        # A dependency-bot conflict is skipped, never fabricated into a
        # completed merge or a plain failure (REQ-006).
        assert merge_events[0].outcome == Outcome.SKIPPED.value


class TestCIObservationAvailabilityDistinctFromVerdict:
    """AS-001: CI observation availability stays distinct and honest across reads."""

    def test_known_then_unavailable_then_known_stay_distinct(self):
        from auto_coder.execution_trace import ItemType
        from auto_coder.github_ci_observer import ci_read_phase, observe_ci

        mock_api = MagicMock()
        mock_api.checks.list_for_ref.return_value = {"check_runs": []}
        mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

        collector = get_trace_collector()
        with collector.start_execution("owner/repo", ItemType.PR.value, 5501, origin="worker"):
            with ci_read_phase("test-known"):
                observe_ci(mock_api, "token", "owner/repo", 5501, "a" * 40)

            error = GitHubRequestError(
                GitHubRequestOutcome(
                    context=GitHubRequestContext(operation_id="op", attempt_id="attempt", subsystem="test", api_origin="https://api.github.com", method="GET", kind="read", endpoint_template="/checks"),
                    status=500,
                    classification=GitHubApiOutcome.REMOTE_ERROR,
                    provenance=RequestProvenance.NETWORK,
                    delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
                    metadata=GitHubResponseMetadata(),
                    elapsed_ms=1.0,
                    message="server error",
                )
            )
            mock_api.checks.list_for_ref.side_effect = error
            with ci_read_phase("test-unavailable"):
                observe_ci(mock_api, "token", "owner/repo", 5501, "a" * 40)

            mock_api.checks.list_for_ref.side_effect = None
            mock_api.checks.list_for_ref.return_value = {"check_runs": []}
            with ci_read_phase("test-known-again"):
                observe_ci(mock_api, "token", "owner/repo", 5501, "a" * 40)

        snapshot = get_trace_collector().get_snapshot(item_type="pr", item_number=5501)
        ci_events = [e for e in snapshot.events if e.stage_id == "pr.ci-observation"]
        assert len(ci_events) == 3
        assert ci_events[0].outcome == Outcome.COMPLETED.value
        assert ci_events[0].facts["availability"] == "known_empty"
        # The unavailable read never claims a new failure or reuses the
        # earlier success as current evidence -- it stands on its own.
        assert ci_events[1].outcome == Outcome.DEFERRED.value
        assert ci_events[1].facts["availability"] == "unavailable"
        assert ci_events[2].outcome == Outcome.COMPLETED.value
        assert ci_events[2].facts["availability"] == "known_empty"


class TestRecorderFailureIsNonInterfering:
    """REQ-008: a diagnostic-recorder failure never changes the business decision."""

    def test_author_gate_outcome_unaffected_by_recorder_failure(self, monkeypatch):
        class _ExplodingCollector:
            def start_execution(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

            def record_event(self, *args, **kwargs):
                raise RuntimeError("injected recorder failure")

        monkeypatch.setattr("auto_coder.automation_engine.get_trace_collector", lambda: _ExplodingCollector())

        config = _skip_all_prs_config()
        engine = AutomationEngine(MagicMock(), config)
        candidate = Candidate(type="pr", data={"number": 5601, "title": "T", "body": "B", "labels": [], "head": {"sha": "a" * 40}}, priority=0)

        result = engine._process_single_candidate_unified("owner/repo", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        assert result.target_reason == "PR author is not in the allowlist"
