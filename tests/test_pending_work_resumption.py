"""Issue #1922: resume deferred Issue/PR evaluation through current authoritative state.

These tests exercise the real production entrypoints (``AutomationEngine.
_process_single_candidate`` / ``_process_single_candidate_unified``) rather
than mocking them, per the issue's explicit warning that mocking the
top-level candidate-processing function would establish neither typed
propagation nor a successful production resumption. GitHub responses are
controlled through small scripted fakes that stand in for the GitHubClient
adapter methods (below the point where ``GitHubRequestError`` is raised),
not through a preselected processing result.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import (
    ISSUE_PROCESSING_STAGE,
    AutomationEngine,
    _issue_content_revision,
    _IssueProcessingStageHandler,
    _PrProcessingStageHandler,
    _reconciliation_admission_deferral,
)
from auto_coder.github_pending_work import (
    PendingObligation,
    PendingReason,
    PendingWorkScheduler,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
)
from auto_coder.github_request_governor import GitHubRequestDeferred
from auto_coder.parent_issue_reconciliation import ParentOperationalError, ParentSpecificationError
from auto_coder.pr_processor import PR_PROCESSING_STAGE
from auto_coder.sibling_dependencies import DependencySatisfaction
from auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
)


def _github_error(classification, *, delivery=DeliveryCertainty.HTTP_RESPONSE_RECEIVED, retry_after=0.0, status=403):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("op", "attempt", "test", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}"),
        None if classification is GitHubApiOutcome.REFUSED else status,
        classification,
        RequestProvenance.NETWORK,
        delivery,
        GitHubResponseMetadata(retry_after_seconds=retry_after),
        1,
    )
    if classification is GitHubApiOutcome.REFUSED:
        return GitHubRequestRefused(outcome)
    return GitHubRequestError(outcome)


class _FakePrGithub:
    """A minimal stand-in for the GitHubClient PR-metadata adapter methods."""

    def __init__(self, responses):
        # responses: pr_number -> list of (dict | Exception), popped in order
        self._responses = {number: list(values) for number, values in responses.items()}
        self.calls: list[tuple] = []

    def get_pull_request_metadata_strict(self, repo_name, pr_number):
        self.calls.append(("get_pull_request_metadata_strict", repo_name, pr_number))
        value = self._responses[pr_number].pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_pr_details(self, raw):
        head = raw.get("head") or {}
        user = raw.get("user") or {}
        return {
            "number": raw.get("number"),
            "title": raw.get("title", ""),
            "body": raw.get("body", ""),
            "head": {"ref": head.get("ref", "branch"), "sha": head.get("sha")},
            "author_id": user.get("id"),
            "user": user,
        }


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


def _skip_all_prs_config() -> AutomationConfig:
    config = AutomationConfig()
    config.PR_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


def _skip_all_issues_config() -> AutomationConfig:
    config = AutomationConfig()
    config.ISSUE_ALLOWLIST = []  # deterministic SKIPPED outcome without further GitHub calls
    return config


# ---------------------------------------------------------------------------
# REQ-002 / REQ-007: registering the stage handlers actually consumes durable
# obligations instead of leaving them retained forever.
# ---------------------------------------------------------------------------


def test_pr_processing_scheduler_snapshot_no_longer_reports_missing_handler(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    identity = WorkIdentity("owner/repo", "pr:1", PR_PROCESSING_STAGE, "sha1")
    store.defer(identity, _github_error(GitHubApiOutcome.SECONDARY_THROTTLED), ("authoritative-refresh", "pr-processing"))

    unregistered_scheduler = PendingWorkScheduler(store)
    unregistered_snapshot = next(item for item in unregistered_scheduler.snapshot() if item["stage"] == PR_PROCESSING_STAGE)
    assert unregistered_snapshot["blocked"] == "no registered stage handler"

    github = _FakePrGithub({})
    engine = AutomationEngine(github, AutomationConfig())
    engine.pending_work_scheduler = PendingWorkScheduler(store)
    engine.pending_work_scheduler.register_handler(PR_PROCESSING_STAGE, _PrProcessingStageHandler(engine, "owner/repo"))

    registered_snapshot = next(item for item in engine.pending_work_scheduler.snapshot() if item["stage"] == PR_PROCESSING_STAGE)
    assert registered_snapshot["blocked"] is None


def test_pr_processing_resumes_automatically_without_new_webhook(tmp_path):
    """AS-001-style: a deferred PR evaluation resumes on its own eligible wait,
    through the real _process_single_candidate entrypoint, with no operator
    action or new webhook."""
    store = PendingWorkStore(tmp_path / "pending.db")
    fresh_pr = {"number": 42, "head": {"ref": "work", "sha": "headsha1"}, "body": "", "title": "t", "user": {"id": 999}}
    github = _FakePrGithub({42: [fresh_pr]})
    engine = AutomationEngine(github, _skip_all_prs_config())
    engine.pending_work_scheduler = PendingWorkScheduler(store, poll_interval=0.02)
    engine.pending_work_scheduler.register_handler(PR_PROCESSING_STAGE, _PrProcessingStageHandler(engine, "owner/repo"))

    identity = WorkIdentity("owner/repo", "pr:42", PR_PROCESSING_STAGE, "headsha1")
    store.defer(identity, _github_error(GitHubApiOutcome.REFUSED, delivery=DeliveryCertainty.DEFINITELY_NOT_SENT), ("authoritative-refresh", "pr-processing"), governor_deadline=time.time())

    async def scenario():
        shutdown = asyncio.Event()
        task = asyncio.create_task(engine.pending_work_scheduler.run(shutdown))
        try:
            for _ in range(300):
                if store.get(identity) is None:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert store.get(identity) is None, "obligation was never resumed and completed"
    assert ("get_pull_request_metadata_strict", "owner/repo", 42) in github.calls
    # No compensating/duplicate GitHub traffic (REQ-008): exactly one refresh.
    assert github.calls.count(("get_pull_request_metadata_strict", "owner/repo", 42)) == 1


def test_issue_processing_resumes_automatically_without_new_webhook(tmp_path):
    store = PendingWorkStore(tmp_path / "pending.db")
    fresh_issue = {"number": 7, "title": "Title", "body": "Body", "labels": [], "user": {"id": 999}}
    github = _FakeIssueGithub({7: [fresh_issue]})
    engine = AutomationEngine(github, _skip_all_issues_config())
    engine.pending_work_scheduler = PendingWorkScheduler(store, poll_interval=0.02)
    engine.pending_work_scheduler.register_handler(ISSUE_PROCESSING_STAGE, _IssueProcessingStageHandler(engine, "owner/repo"))

    revision = _issue_content_revision(fresh_issue)
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, revision)
    store.defer(identity, _github_error(GitHubApiOutcome.PRIMARY_THROTTLED), ("authoritative-refresh", "issue-processing"), governor_deadline=time.time())

    async def scenario():
        shutdown = asyncio.Event()
        task = asyncio.create_task(engine.pending_work_scheduler.run(shutdown))
        try:
            for _ in range(300):
                if store.get(identity) is None:
                    break
                await asyncio.sleep(0.01)
        finally:
            shutdown.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert store.get(identity) is None, "obligation was never resumed and completed"
    assert ("get_issue_dispatch_snapshot_strict", "owner/repo", 7) in github.calls


# ---------------------------------------------------------------------------
# REQ-003 / REQ-006: stale observations must not authorize a newer generation,
# and a stale handler must not fabricate a re-evaluation of the new state.
# ---------------------------------------------------------------------------


def _fail_if_called(*_args, **_kwargs):
    raise AssertionError("a stale-revision obligation must not authorize a new evaluation")


def test_pr_stage_handler_supersedes_when_head_changed_while_waiting():
    github = _FakePrGithub({42: [{"number": 42, "head": {"ref": "work", "sha": "headsha2"}, "body": "", "title": "t", "user": {"id": 1}}]})
    engine = AutomationEngine(github, AutomationConfig())
    engine._process_single_candidate = _fail_if_called
    handler = _PrProcessingStageHandler(engine, "owner/repo")
    obligation = PendingObligation(WorkIdentity("owner/repo", "pr:42", PR_PROCESSING_STAGE, "headsha1"), PendingReason.THROTTLED, 0.0, ("authoritative-refresh", "pr-processing"))

    outcome = handler.dispatch(obligation)

    assert outcome == StageOutcome(superseded=True)


def test_issue_stage_handler_supersedes_when_body_changed_while_waiting():
    github = _FakeIssueGithub({7: [{"number": 7, "title": "Title", "body": "A materially different body", "labels": []}]})
    engine = AutomationEngine(github, AutomationConfig())
    engine._process_single_candidate = _fail_if_called
    handler = _IssueProcessingStageHandler(engine, "owner/repo")
    stale_revision = _issue_content_revision({"title": "Title", "body": "Original body"})
    obligation = PendingObligation(WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, stale_revision), PendingReason.ADMISSION_DEFERRED, 0.0, ("authoritative-refresh", "issue-processing"))

    outcome = handler.dispatch(obligation)

    assert outcome == StageOutcome(superseded=True)


# ---------------------------------------------------------------------------
# REQ-005 / REQ-006: a second operational failure during resumption re-defers
# rather than silently dropping or completing the obligation.
# ---------------------------------------------------------------------------


def test_pr_stage_handler_redefers_on_second_operational_failure():
    second_failure = _github_error(GitHubApiOutcome.SECONDARY_THROTTLED)
    github = _FakePrGithub({42: [second_failure]})
    engine = AutomationEngine(github, AutomationConfig())
    handler = _PrProcessingStageHandler(engine, "owner/repo")
    obligation = PendingObligation(WorkIdentity("owner/repo", "pr:42", PR_PROCESSING_STAGE, "headsha1"), PendingReason.THROTTLED, 0.0, ("authoritative-refresh", "pr-processing"), throttle_attempts=1)

    outcome = handler.dispatch(obligation)

    assert outcome.error is second_failure
    assert outcome.superseded is False
    assert outcome.completed_effects == ()


def test_issue_stage_handler_redefers_on_second_operational_failure():
    second_failure = _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE)
    github = _FakeIssueGithub({7: [second_failure]})
    engine = AutomationEngine(github, AutomationConfig())
    handler = _IssueProcessingStageHandler(engine, "owner/repo")
    obligation = PendingObligation(WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, "rev"), PendingReason.ADMISSION_DEFERRED, 0.0, ("authoritative-refresh", "issue-processing"))

    outcome = handler.dispatch(obligation)

    assert outcome.error is second_failure


# ---------------------------------------------------------------------------
# REQ-001 / REQ-002: production evaluation preserves a typed GitHub failure as
# a durable obligation rather than a plain error, at the Issue hierarchy
# observation boundary (mirrors pr_processor.process_pull_request's own
# GitHubRequestError handling for PRs).
# ---------------------------------------------------------------------------


def test_process_single_candidate_defers_issue_hierarchy_observation_failure(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: store)

    class _ThrottledDirectChildGithub:
        def get_direct_sub_issues_strict(self, repo_name, issue_number):
            raise _github_error(GitHubApiOutcome.PRIMARY_THROTTLED)

    engine = AutomationEngine(_ThrottledDirectChildGithub(), AutomationConfig())
    issue_data = {"number": 7, "title": "T", "body": "B", "labels": []}
    candidate = Candidate(type="issue", data=issue_data, priority=0, issue_number=7)

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)

    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue_data))
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.reason is PendingReason.THROTTLED
    # Never a semantic success, empty collection, or unrelated verdict (REQ-001).
    assert result.success is False
    assert result.error is not None


def test_wrapped_reconciliation_admission_deferral_is_durably_retained(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: store)

    context = GitHubRequestContext("op", "attempt", "parent", "https://api.github.com", "GET", "read", "/repos/{repo}/issues/{number}/parent", "owner/repo", "issue:7", strict_read=True)
    deferred = GitHubRequestDeferred(context, "request_in_flight", retry_at=time.time() + 20)

    class _ParentGithub(GitHubClient):
        def __init__(self):
            pass

        def get_issue_dispatch_snapshot_strict(self, _repo, _number):
            return {"id": 70, "number": 7, "title": "T", "body": "Parent-Issue: #6", "labels": [], "state": "open"}

    engine = AutomationEngine(_ParentGithub(), AutomationConfig())

    def fail_reconciliation(*_args):
        try:
            raise deferred
        except GitHubRequestDeferred as cause:
            raise ParentOperationalError("cannot read native parent") from cause

    monkeypatch.setattr(engine, "_reconcile_parent_issue", fail_reconciliation)
    issue_data = {"number": 7, "title": "T", "body": "Parent-Issue: #6", "labels": []}
    result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue_data, priority=0, issue_number=7), engine.config)

    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert "request_in_flight" in (result.target_reason or "")
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue_data))
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at


def test_reconciliation_deferral_classification_never_parses_messages():
    misleading = ParentOperationalError("request_in_flight; definitely_not_sent")

    assert _reconciliation_admission_deferral(misleading) is None


def test_reconciliation_deferral_classification_ignores_unrelated_suppressed_context():
    deferred = _admission_deferral()
    independent = None
    try:
        raise deferred
    except GitHubRequestDeferred:
        try:
            raise RuntimeError("independent programming failure") from None
        except RuntimeError as programming_error:
            independent = programming_error
            wrapped = ParentOperationalError("relationship adapter failed")
            wrapped.__cause__ = programming_error

    assert independent is not None
    assert independent.__context__ is deferred
    assert independent.__suppress_context__ is True
    assert _reconciliation_admission_deferral(wrapped) is None


def _admission_deferral() -> GitHubRequestDeferred:
    context = GitHubRequestContext(
        "op",
        "attempt",
        "relationship-reconciliation",
        "https://api.github.com",
        "GET",
        "read",
        "/repos/{repo}/issues/{number}/dependencies",
        "owner/repo",
        "issue:7",
        strict_read=True,
    )
    return GitHubRequestDeferred(context, "request_in_flight", retry_at=time.time() + 20)


def _admitted_issue_engine(monkeypatch, tmp_path, issue):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: store)
    GitHubClient.reset_singleton()
    github = GitHubClient.get_instance("token")
    github.get_issue_dispatch_snapshot_strict = MagicMock(return_value=dict(issue))
    github.get_direct_sub_issues_strict = MagicMock(return_value=[])
    engine = AutomationEngine(github, AutomationConfig())
    engine._is_issue_author_allowed = MagicMock(return_value=True)
    engine._reconcile_parent_issue = MagicMock(return_value=dict(issue))
    engine._reconcile_validation_snapshot = MagicMock(return_value=dict(issue))
    engine._is_issue_specification_validation_enabled = MagicMock(return_value=False)
    engine._is_issue_decomposition_validation_enabled = MagicMock(return_value=False)
    validator = MagicMock()
    validator.is_reissue_required.return_value = False
    validator.identity.return_value = "current-identity"
    engine._get_specification_validator = MagicMock(return_value=validator)
    decomposition_validator = MagicMock()
    decomposition_validator.is_reissue_required.return_value = False
    engine._get_decomposition_validator = MagicMock(return_value=decomposition_validator)
    engine.pending_work_scheduler.wake = MagicMock()
    return engine, store


def test_sibling_gate_retains_wrapped_admission_deferral(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "## Requirements\n- REQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._standalone_relationship_is_current = MagicMock(return_value=True)
    deferred = _admission_deferral()
    engine._reconcile_sibling_dependencies = MagicMock(side_effect=ParentOperationalError("dependency read unavailable"))
    engine._reconcile_sibling_dependencies.side_effect.__cause__ = deferred

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(issue), priority=0, issue_number=7), engine.config)

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    assert result.target_reason == ("Deferred reconciliation for owner/repo issue #7: " "stage=issue-processing; reason=request_in_flight; " f"api_origin=https://api.github.com; retry_at={obligation.not_before}; " "delivery=definitely_not_sent")
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_family_recheck_retains_wrapped_admission_deferral(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "Parent-Issue: #6\n## Requirements\n- REQ-001: Preserve behavior.",
        "labels": [],
        "state": "open",
        "user": {"id": 1},
    }
    parent = {**issue, "id": 60, "number": 6, "body": "## Objective\nCoordinate work.", "labels": [{"name": "implementation-ready"}]}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    engine._reconcile_sibling_dependencies = MagicMock(return_value=DependencySatisfaction.SATISFIED)
    deferred = _admission_deferral()
    wrapped = ParentOperationalError("family read unavailable")
    wrapped.__cause__ = deferred
    # The unified wrapper first refreshes admission policy for this family;
    # the second successful read is the implementation path's initial family
    # evidence, and the refusal interrupts its final dispatch-time recheck.
    family = (parent, [dict(issue)])
    engine._fetch_authoritative_decomposition_set = MagicMock(side_effect=[family, family, wrapped])

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(issue), priority=0, issue_number=7), engine.config)

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert "request_in_flight" in (result.target_reason or "")
    assert "https://api.github.com" in (result.target_reason or "")
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_early_live_parent_family_refresh_retains_wrapped_admission_deferral(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "Parent-Issue: #6\n## Requirements\n- REQ-001: Preserve behavior.",
        "labels": [],
        "state": "open",
        "user": {"id": 1},
    }
    parent = {**issue, "id": 60, "number": 6, "body": "## Objective\nCoordinate work.", "labels": [{"name": "implementation-ready"}]}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    deferred = _admission_deferral()
    wrapped = ParentOperationalError("early family refresh unavailable")
    wrapped.__cause__ = deferred
    # The unified admission wrapper establishes the first family observation;
    # the typed refusal then interrupts the implementation path's early live
    # parent refresh, before validation or implementation can run.
    engine._fetch_authoritative_decomposition_set = MagicMock(side_effect=[(parent, [dict(issue)]), wrapped])

    with (
        patch.object(engine, "_process_single_candidate_reserved") as implementation,
        patch("auto_coder.automation_engine.logger.warning") as warning,
    ):
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(issue), priority=0, issue_number=7), engine.config)

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    expected_reason = "Deferred reconciliation for owner/repo issue #7: " "stage=issue-processing; reason=request_in_flight; " f"api_origin=https://api.github.com; retry_at={obligation.not_before}; " "delivery=definitely_not_sent"
    assert result.target_reason == expected_reason
    assert result.actions == [expected_reason]
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    warning.assert_called_once_with(
        "Deferred GitHub reconciliation repository={} issue={} stage={} reason={} api_origin={} retry_at={} delivery={}",
        "owner/repo",
        7,
        ISSUE_PROCESSING_STAGE,
        "request_in_flight",
        "https://api.github.com",
        obligation.not_before,
        DeliveryCertainty.DEFINITELY_NOT_SENT.value,
    )
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_early_live_parent_family_refresh_preserves_definitive_refusal_type(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "Parent-Issue: #6\n## Requirements\nREQ-001: Preserve behavior.",
        "labels": [],
        "state": "open",
        "user": {"id": 1},
    }
    parent = {**issue, "id": 60, "number": 6, "body": "## Objective\nCoordinate work.", "labels": [{"name": "implementation-ready"}]}
    engine, _store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    refusal = ParentSpecificationError("sibling declaration conflicts with its native parent")
    # Admission sees a valid family. Ordinary processing then observes the
    # newly contradictory sibling while refreshing the live family.
    engine._fetch_authoritative_decomposition_set = MagicMock(side_effect=[(parent, [dict(issue)]), refusal])

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
        )

    assert result.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert result.definitive_parent_refusal is True
    assert result.error == "Parent-Issue reconciliation blocked processing: sibling declaration conflicts with its native parent"
    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    implementation.assert_not_called()


def test_inherited_family_refresh_preserves_definitive_refusal_type(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "Parent-Issue: #6\n## Requirements\nREQ-001: Preserve behavior.",
        "labels": [],
        "state": "open",
        "user": {"id": 1},
    }
    parent = {**issue, "id": 60, "number": 6, "body": "## Objective\nCoordinate work.", "labels": [{"name": "implementation-ready"}]}
    family = (parent, [dict(issue)])
    engine, _store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    refusal = ParentSpecificationError("sibling declaration conflicts with its native parent")
    # Admission and the early live-parent refresh see a consistent family.
    # The inherited-authority refresh then observes the sibling contradiction.
    engine._fetch_authoritative_decomposition_set = MagicMock(side_effect=[family, family, refusal])

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
        )

    assert engine._fetch_authoritative_decomposition_set.call_count == 3
    assert result.target_outcome is ExplicitTargetOutcome.BLOCKED
    assert result.definitive_parent_refusal is True
    assert result.error == "Parent-Issue reconciliation blocked processing: sibling declaration conflicts with its native parent"
    assert result.actions == ["Blocked - invalid Parent-Issue relationship metadata"]
    implementation.assert_not_called()


def test_capacity_refill_durably_retains_wrapped_admission_deferral(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "Parent-Issue: #6\n## Requirements\n- REQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(7)]))
    deferred = _admission_deferral()
    wrapped = ParentOperationalError("refill reconciliation unavailable")
    wrapped.__cause__ = deferred
    engine._reconcile_parent_issue = MagicMock(side_effect=wrapped)
    engine._process_single_candidate = MagicMock()

    with patch("auto_coder.automation_engine.logger.warning") as warning:
        first_completed = asyncio.run(engine._refill_normal_implementation_slots("owner/repo"))
        second_completed = asyncio.run(engine._refill_normal_implementation_slots("owner/repo"))

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert first_completed is True
    assert second_completed is True
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    warning.assert_called_once_with(
        "Deferred GitHub reconciliation repository={} issue={} stage={} reason={} api_origin={} retry_at={} delivery={}",
        "owner/repo",
        7,
        ISSUE_PROCESSING_STAGE,
        "request_in_flight",
        "https://api.github.com",
        obligation.not_before,
        DeliveryCertainty.DEFINITELY_NOT_SENT.value,
    )
    engine.pending_work_scheduler.wake.assert_called_once_with()
    engine._reconcile_parent_issue.assert_called_once_with("owner/repo", 7, issue)
    engine._process_single_candidate.assert_not_called()
