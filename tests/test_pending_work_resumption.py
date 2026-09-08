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

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import (
    ISSUE_PROCESSING_STAGE,
    AutomationEngine,
    _issue_content_revision,
    _IssueProcessingStageHandler,
    _PrProcessingStageHandler,
)
from auto_coder.github_pending_work import (
    PendingObligation,
    PendingReason,
    PendingWorkScheduler,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
)
from auto_coder.pr_processor import PR_PROCESSING_STAGE
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
