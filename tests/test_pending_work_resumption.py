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
import io
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome
from auto_coder.automation_engine import (
    ISSUE_PROCESSING_REFRESH_EFFECT,
    ISSUE_PROCESSING_STAGE,
    STARTUP_RECONCILIATION_EFFECT,
    STARTUP_RECONCILIATION_STAGE,
    AutomationEngine,
    _issue_content_revision,
    _IssueProcessingStageHandler,
    _PrProcessingStageHandler,
    _reconciliation_admission_deferral,
    _reconciliation_request_error,
    _StartupReconciliationHandler,
)
from auto_coder.entity_invalidation import EntityIdentity
from auto_coder.github_pending_work import (
    PendingObligation,
    PendingReason,
    PendingWorkOwnershipError,
    PendingWorkPersistenceError,
    PendingWorkScheduler,
    PendingWorkStore,
    StageOutcome,
    WorkIdentity,
)
from auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.logger_config import setup_logger
from auto_coder.parent_issue_reconciliation import ParentOperationalError, ParentSpecificationError
from auto_coder.pr_processor import PR_PROCESSING_STAGE
from auto_coder.sibling_dependencies import DependencySatisfaction
from auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue, get_ghapi_client
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    DiagnosticTransport,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
    configure_github_request_boundary,
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


@pytest.mark.parametrize("marker", ["directive", "branch"])
def test_startup_owner_lookup_transport_failure_is_retained_and_resumed(tmp_path, monkeypatch, marker):
    """An uncertain owner retains capacity and startup work without stopping the daemon."""
    error = _github_error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE, status=None)
    pr = {"number": 108, "body": "Closes #100" if marker == "directive" else "", "head": {"ref": "issue-100-work" if marker == "branch" else "work"}}

    class StartupGithub:
        failed = True
        enumerations = 0

        def get_open_pull_requests(self, repo):
            return [pr]

        def get_issue_strict(self, repo, number):
            if self.failed:
                raise error
            return {"number": number, "state": "closed"}

        get_issue = get_issue_strict

        def get_issue_details(self, issue):
            return issue

        def get_connected_prs(self, repo, number, strict=False):
            return []

        def get_pull_request(self, repo, number):
            return {"number": number, "state": "open", "merged": False}

        def get_pr_details(self, pull_request):
            return pull_request

        def get_open_entities_strict(self, repo):
            self.enumerations += 1
            return OpenGitHubEntities()

    github = StartupGithub()
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 100)
    assert slots.reserve(owner) is True
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repo: store)
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = slots
    identity = WorkIdentity("owner/repo", "startup", STARTUP_RECONCILIATION_STAGE)

    async def scenario():
        engine._loop = asyncio.get_running_loop()
        engine._startup_reconciliation_event = asyncio.Event()
        engine._shutdown_event = asyncio.Event()
        task = asyncio.create_task(engine._perform_startup_reconciliation("owner/repo"))
        try:
            for _ in range(300):
                if store.get(identity) is not None:
                    break
                await asyncio.sleep(0.01)
            obligation = store.get(identity)
            assert obligation is not None
            assert obligation.reason is PendingReason.INDETERMINATE
            assert obligation.unfinished_effects == (STARTUP_RECONCILIATION_EFFECT,)
            assert obligation.last_error == "GitHub request failed: transport_failure"
            assert not task.done()
            assert engine.startup_reconciled is False
            assert github.enumerations == 0
            assert slots.active_owners() == (owner,)
            assert slots.snapshot().owners[0].implementation_prs == ()
            assert slots.reserve(ImplementationOwner("pr", 108)) is False

            github.failed = False
            handler = _StartupReconciliationHandler(engine, "owner/repo")
            outcome = await asyncio.to_thread(handler.dispatch, obligation)
            assert outcome.error is None
            assert outcome.completed_effects == (STARTUP_RECONCILIATION_EFFECT,)
            assert store.complete_effect(identity, STARTUP_RECONCILIATION_EFFECT) is True
            await asyncio.wait_for(task, timeout=2)
            assert engine.startup_reconciled is True
            assert github.enumerations == 1
            assert store.get(identity) is None
            assert slots.active_owners() == (owner,)
            assert slots.snapshot().owners[0].implementation_prs == (108,)
        finally:
            engine._shutdown_event.set()
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


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


class _BarrierIssueGithub(_FakeIssueGithub):
    """Wire-target recorder that can hold the real production handler entry."""

    def __init__(self, responses):
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def get_issue_dispatch_snapshot_strict(self, repo_name, issue_number):
        self.calls.append(("get_issue_dispatch_snapshot_strict", repo_name, issue_number))
        if self.block:
            self.entered.set()
            assert self.release.wait(5), "barrier-held production handler was not released"
        value = self._responses[issue_number].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


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

    unregistered_scheduler = PendingWorkScheduler(store, repository="owner/repo")
    unregistered_snapshot = next(item for item in unregistered_scheduler.snapshot() if item["stage"] == PR_PROCESSING_STAGE)
    assert unregistered_snapshot["blocked"] == "no registered stage handler"

    github = _FakePrGithub({})
    engine = AutomationEngine(github, AutomationConfig())
    engine.pending_work_scheduler = PendingWorkScheduler(store, repository="owner/repo")
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
    engine.pending_work_scheduler = PendingWorkScheduler(store, repository="owner/repo", poll_interval=0.02)
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
    engine.pending_work_scheduler = PendingWorkScheduler(store, repository="owner/repo", poll_interval=0.02)
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


@pytest.mark.parametrize("active_repository", ["acme/alpha", "acme/beta"])
def test_production_binding_isolates_equal_issue_due_and_overlapping_recovery(tmp_path, monkeypatch, active_repository):
    """REQ-009: shared persistence cannot cross production handler ownership."""
    repositories = ("acme/alpha", "acme/beta")
    passive_repository = next(repo for repo in repositories if repo != active_repository)
    issue = {"number": 5411, "title": "Same", "body": "Same", "labels": [], "user": {"id": 999}}
    revision = _issue_content_revision(issue)
    store = PendingWorkStore(tmp_path / "shared-pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: store)

    github_by_repo = {repo: _BarrierIssueGithub({5411: [dict(issue), dict(issue)]}) for repo in repositories}
    engines = {repo: AutomationEngine(github_by_repo[repo], _skip_all_issues_config()) for repo in repositories}
    for repo, engine in engines.items():
        engine._bind_pending_work_scheduler(repo)
        assert set(engine.pending_work_scheduler._handlers) == {
            "startup-reconciliation",
            "pr-processing",
            "issue-processing",
            "codex-retry-handoff",
            "validation-publication",
            "decomposition-validation-publication",
        }

    def retain_through_production(repo):
        result = CandidateProcessingResult(type="issue", number=5411, title="Same")
        engines[repo]._defer_issue_evaluation(
            repo,
            5411,
            issue,
            _github_error(GitHubApiOutcome.PRIMARY_THROTTLED),
            result,
        )
        return WorkIdentity(repo, "issue:5411", ISSUE_PROCESSING_STAGE, revision)

    identities = {repo: retain_through_production(repo) for repo in repositories}
    foreign_before = store.get(identities[active_repository])

    async def dispatch_one_repository():
        scheduler = engines[passive_repository].pending_work_scheduler
        await scheduler._dispatch_due()
        await asyncio.gather(*tuple(scheduler._tasks))

    asyncio.run(dispatch_one_repository())

    assert github_by_repo[passive_repository].calls == [("get_issue_dispatch_snapshot_strict", passive_repository, 5411)]
    assert github_by_repo[active_repository].calls == []
    assert store.get(identities[passive_repository]) is None
    assert store.get(identities[active_repository]) == foreign_before

    # Clear the retained foreign due row through its owner, then recreate both
    # identities as interrupted work to exercise restart recovery overlap.
    async def dispatch_owner():
        scheduler = engines[active_repository].pending_work_scheduler
        await scheduler._dispatch_due()
        await asyncio.gather(*tuple(scheduler._tasks))

    asyncio.run(dispatch_owner())
    identities = {repo: retain_through_production(repo) for repo in repositories}
    for identity in identities.values():
        store.mark_running(identity)

    active_github = github_by_repo[active_repository]
    active_github.block = True

    async def overlap_recovery():
        active_scheduler = engines[active_repository].pending_work_scheduler
        passive_scheduler = engines[passive_repository].pending_work_scheduler
        active_task = asyncio.create_task(active_scheduler._recover_interrupted())
        assert await asyncio.to_thread(active_github.entered.wait, 2)
        assert store.get(identities[active_repository]).status == "running"

        await passive_scheduler._recover_interrupted()
        assert store.get(identities[passive_repository]) is None
        assert store.get(identities[active_repository]) is not None
        active_github.release.set()
        await asyncio.wait_for(active_task, 2)

    asyncio.run(overlap_recovery())

    assert store.get(identities[active_repository]) is None
    for repo in repositories:
        wire_targets = [call[1:] for call in github_by_repo[repo].calls]
        assert wire_targets == [(repo, 5411), (repo, 5411)]


def test_production_binding_rejects_scp_style_git_remote(tmp_path, monkeypatch):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: store)
    engine = AutomationEngine(_FakeIssueGithub({}), AutomationConfig())
    original_scheduler = engine.pending_work_scheduler

    with pytest.raises(PendingWorkOwnershipError):
        engine._bind_pending_work_scheduler("git@github.com:acme/repo")

    assert engine.pending_work_scheduler is original_scheduler
    assert engine._pending_work_repository_key is None


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
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: store)

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


@pytest.mark.parametrize("reason", ["request_in_flight", "admission_queue"])
def test_wrapped_reconciliation_admission_deferral_is_durably_retained(tmp_path, monkeypatch, reason):
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: store)

    context = GitHubRequestContext("op", "attempt", "parent", "https://api.github.com", "GET", "read", "/repos/{repo}/issues/{number}/parent", "owner/repo", "issue:7", strict_read=True)
    deferred = GitHubRequestDeferred(context, reason, retry_at=time.time() + 20)

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
    assert reason in (result.target_reason or "")
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue_data))
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at


def test_reconciliation_deferral_classification_never_parses_messages():
    misleading = ParentOperationalError("request_in_flight; definitely_not_sent")

    assert _reconciliation_admission_deferral(misleading) is None


@pytest.mark.parametrize("declared_parent", [True, False])
def test_native_parent_transport_failure_defers_candidate_without_dispatch(tmp_path, monkeypatch, declared_parent):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": ("Parent-Issue: #6\n" if declared_parent else "") + "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    error = _github_error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE, status=None)
    engine.github.get_parent_issue_details_strict = MagicMock(side_effect=error)
    # Exercise the real reconciliation wrapper, both at initial admission and
    # through the later native-parent lookup boundary.
    engine._reconcile_parent_issue = AutomationEngine._reconcile_parent_issue.__get__(engine)
    if not declared_parent:

        def read_parent(repo, number, snapshot):
            engine._reconcile_parent_issue(repo, number, snapshot)

        engine._get_authoritative_parent_number = read_parent

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=dict(issue), priority=0), engine.config)

    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.success is False
    assert result.refill_retry_required is True
    assert result.error == "GitHub request failed: transport_failure"
    assert "delivery=indeterminate_after_possible_send" in result.target_reason
    obligation = store.get(WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue)))
    assert obligation is not None
    assert obligation.reason is PendingReason.INDETERMINATE
    assert obligation.throttle_attempts == 0
    engine.pending_work_scheduler.wake.assert_called_once_with()
    engine.github.get_parent_issue_details_strict.assert_called_once_with("owner/repo", 7)
    implementation.assert_not_called()


def test_reconciliation_transport_classification_requires_explicit_typed_cause():
    misleading = ParentOperationalError("GitHub request failed: transport_failure")
    assert _reconciliation_request_error(misleading) is None
    transport = _github_error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE, status=None)
    misleading.__context__ = transport
    assert _reconciliation_request_error(misleading) is None
    misleading.__cause__ = transport
    assert _reconciliation_request_error(misleading) is transport


def test_unwrapped_parent_operational_failure_does_not_stop_candidate_worker(tmp_path, monkeypatch):
    issue = {"id": 70, "number": 7, "title": "T", "body": "## Requirements\nREQ-001: Preserve behavior.", "labels": [], "state": "open", "user": {"id": 1}}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(side_effect=ParentOperationalError("native parent unavailable"))
    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("owner/repo", Candidate(type="issue", data=issue, priority=0), engine.config)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert result.error == "Parent-Issue reconciliation is temporarily unavailable: native parent unavailable"
    assert store.get(WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))) is None
    implementation.assert_not_called()


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
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: store)
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
    engine.pending_work_scheduler = MagicMock()
    return engine, store


def test_refill_deferral_persistence_failure_pauses_repository(tmp_path, monkeypatch):
    """#2446 REQ-002/007: failed timed handoff is a repository fault."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Faulted",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    sibling = {**issue, "id": 80, "number": 8, "title": "Sibling"}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    retained_identity = WorkIdentity("owner/repo", "issue:99", ISSUE_PROCESSING_STAGE, "retained")
    store.defer(
        retained_identity,
        _admission_deferral(),
        (ISSUE_PROCESSING_REFRESH_EFFECT,),
    )
    deferred = _admission_deferral()
    engine.github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(7), OpenGitHubIssue(8)]))
    engine.github.get_issue_details = MagicMock(side_effect=lambda value: value)
    engine.github.get_parent_issue_number_strict = MagicMock(return_value=None)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = [
        dict(issue),
        dict(sibling),
        deferred,
    ]
    monkeypatch.setattr(
        store,
        "defer",
        MagicMock(side_effect=PendingWorkPersistenceError("pending store is unwritable")),
    )

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True

    assert implementation.call_count == 0
    assert store.get(retained_identity) is not None
    assert engine._refill_admission_paused("owner/repo", 8)
    assert engine.get_status()["refill_faults"] == [
        {
            "repository": "owner/repo",
            "target": None,
            "phase": "pending_work_persistence",
            "exception_class": "PendingWorkPersistenceError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]


def test_refill_enumeration_persistence_read_failure_is_visible(tmp_path, monkeypatch):
    """#2446 REQ-008: an unreadable admission store is an operational fault."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Faulted",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(7)]))
    monkeypatch.setattr(
        store,
        "all_pending",
        MagicMock(side_effect=PendingWorkPersistenceError("pending store unreadable")),
    )

    console = io.StringIO()
    log_file = tmp_path / "refill-fault.log"
    setup_logger(log_level="INFO", log_file=str(log_file), stream=console)
    try:
        assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    finally:
        setup_logger(log_level="INFO")
    assert engine.get_status()["refill_faults"] == [
        {
            "repository": "owner/repo",
            "target": None,
            "phase": "refill_enumeration_persistence",
            "exception_class": "PendingWorkPersistenceError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]
    for diagnostics in (console.getvalue(), log_file.read_text()):
        assert "ERROR" in diagnostics
        assert "PendingWorkPersistenceError" in diagnostics
        assert "Traceback" in diagnostics


def test_intervention_pause_keeps_changed_pending_issue_unfinished(tmp_path, monkeypatch):
    """#2446 REQ-004: pending entry checks the pause before strict refresh."""
    issue = {
        "number": 7,
        "title": "Original",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    identity = WorkIdentity(
        "owner/repo",
        "issue:7",
        ISSUE_PROCESSING_STAGE,
        _issue_content_revision(issue),
    )
    obligation = store.defer(
        identity,
        _admission_deferral(),
        (ISSUE_PROCESSING_REFRESH_EFFECT,),
        governor_deadline=0,
    )
    engine._record_refill_fault(
        "owner/repo",
        7,
        "candidate_dispatch",
        RuntimeError("unknown effect"),
        "intervention_required",
    )
    scheduler = PendingWorkScheduler(store, repository="owner/repo")
    scheduler.register_handler(ISSUE_PROCESSING_STAGE, _IssueProcessingStageHandler(engine, "owner/repo"))
    engine.github.get_issue_dispatch_snapshot_strict.reset_mock()

    asyncio.run(scheduler._claim_and_run(obligation, scheduler._handlers[ISSUE_PROCESSING_STAGE].dispatch))

    engine.github.get_issue_dispatch_snapshot_strict.assert_not_called()
    retained = store.get(identity)
    assert retained is not None
    assert retained.unfinished_effects == (ISSUE_PROCESSING_REFRESH_EFFECT,)
    assert engine._refill_admission_paused("owner/repo", 7)


def test_refill_enumeration_deferral_resumes_only_at_durable_deadline(tmp_path, monkeypatch):
    """#2446 REQ-007: initial refill refusal enters timed pending work."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Deferred",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(7)]))
    engine.github.get_parent_issue_number_strict = MagicMock(return_value=None)
    deadline = time.time() + 600
    context = GitHubRequestContext(
        "op",
        "attempt",
        "issue-dispatch-snapshot",
        "https://api.github.com",
        "GET",
        "read",
        "/repos/{repo}/issues/{number}",
        "owner/repo",
        "issue:7",
        strict_read=True,
    )
    deferred = GitHubRequestDeferred(context, "request_in_flight", retry_at=deadline)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = [
        deferred,
        dict(issue),
    ]

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, "")
    obligation = store.get(identity)
    assert obligation is not None
    assert obligation.not_before >= deadline
    assert engine.github.get_issue_dispatch_snapshot_strict.call_count == 1

    # A duplicate refill opportunity consults the retained deadline before
    # another strict target read.
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert engine.github.get_issue_dispatch_snapshot_strict.call_count == 1

    scheduler = PendingWorkScheduler(store, repository="owner/repo", clock=lambda: deadline + 1)
    scheduler.register_handler(ISSUE_PROCESSING_STAGE, _IssueProcessingStageHandler(engine, "owner/repo"))
    engine.pending_work_scheduler = scheduler
    completed = CandidateProcessingResult(type="issue", number=7, success=True, target_outcome=ExplicitTargetOutcome.SUCCESS)
    engine._process_single_candidate = MagicMock(return_value=completed)

    async def resume():
        await scheduler._dispatch_due()
        if scheduler._tasks:
            await asyncio.gather(*tuple(scheduler._tasks))

    asyncio.run(resume())

    assert engine.github.get_issue_dispatch_snapshot_strict.call_count == 2
    engine._process_single_candidate.assert_called_once()
    assert store.get(identity) is None


@pytest.mark.parametrize(
    "reason",
    [
        "request_in_flight",
        "admission_queue",
        "mutation_spacing",
        "request_rolling_window",
        "mutation_minute_window",
        "mutation_hour_window",
        "governor_initialization_contention",
        "governor_transaction_contention",
        "rate_limit_cooldown",
    ],
)
def test_initial_dispatch_snapshot_retains_supported_admission_deferral(tmp_path, monkeypatch, reason):
    """The first production strict read returns Deferred instead of escaping."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    context = GitHubRequestContext(
        "op",
        "attempt",
        "issue-dispatch-snapshot",
        "https://api.github.com",
        "GET",
        "read",
        "/repos/{repo}/issues/{number}",
        "owner/repo",
        "issue:7",
        strict_read=True,
    )
    deferred = GitHubRequestDeferred(context, reason, retry_at=time.time() + 20)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = deferred

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert result.success is False
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    assert obligation.unfinished_effects == (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE)
    assert f"reason={reason}" in (result.target_reason or "")
    assert "api_origin=https://api.github.com" in (result.target_reason or "")
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_initial_dispatch_snapshot_retains_real_governor_cooldown(tmp_path, monkeypatch):
    """A wire-observed cooldown is retained at the real strict-reader boundary."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda _repository: store)
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3", wait_budget=0)
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "20"}, json={"message": "slow down"}, request=request)

    def client(*_args, **_kwargs):
        return httpx.Client(
            transport=DiagnosticTransport(
                httpx.MockTransport(handler),
                admission_hook=governor.admit_blocking,
                observation_hook=governor.observe,
            )
        )

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", client)
    configure_github_request_boundary(governor.admit_blocking, governor.observe)
    try:
        cooldown_started_after = time.time()
        with pytest.raises(GitHubRequestError) as throttled:
            get_ghapi_client("token")("/seed")
        assert throttled.value.outcome.classification is GitHubApiOutcome.SECONDARY_THROTTLED

        GitHubClient.reset_singleton()
        github = GitHubClient.get_instance("token")
        engine = AutomationEngine(github, AutomationConfig())
        # Engine construction installs its default hooks; restore the controlled
        # production boundary whose prior response established the cooldown.
        configure_github_request_boundary(governor.admit_blocking, governor.observe)
        engine._is_issue_author_allowed = MagicMock(return_value=True)
        engine.pending_work_scheduler = MagicMock()

        with patch.object(engine, "_process_single_candidate_reserved") as implementation:
            result = engine._process_single_candidate_unified(
                "owner/repo",
                Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
                engine.config,
                origin="capacity-refill-intake",
            )

        identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
        obligation = store.get(identity)
        assert sends == ["/seed"]
        assert result.success is False
        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert result.refill_retry_required is True
        assert obligation is not None
        assert obligation.reason is PendingReason.ADMISSION_DEFERRED
        assert obligation.not_before >= cooldown_started_after + 20
        assert "reason=rate_limit_cooldown" in (result.target_reason or "")
        engine.pending_work_scheduler.wake.assert_called_once_with()
        implementation.assert_not_called()
    finally:
        configure_github_request_boundary()
        governor.close()


def test_initial_dispatch_snapshot_does_not_reclassify_unsupported_refusal(tmp_path, monkeypatch):
    """An unusable Governor state remains a failure, not timed pending work."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    context = GitHubRequestContext(
        "op",
        "attempt",
        "issue-dispatch-snapshot",
        "https://api.github.com",
        "GET",
        "read",
        "/repos/{repo}/issues/{number}",
        "owner/repo",
        "issue:7",
        strict_read=True,
    )
    deferred = GitHubRequestDeferred(context, "governor_state_unavailable", retry_at=time.time() + 20)
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = deferred

    with (
        patch.object(engine, "_process_single_candidate_reserved") as implementation,
        pytest.raises(GitHubRequestDeferred) as raised,
    ):
        engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    assert raised.value is deferred
    assert store.get(identity) is None
    engine.pending_work_scheduler.wake.assert_not_called()
    implementation.assert_not_called()


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


def test_capacity_dispatch_retains_bare_native_parent_admission_deferral(tmp_path, monkeypatch):
    """A refusal after candidate selection must not escape the refill worker."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    deferred = _admission_deferral()
    engine._get_authoritative_parent_number = MagicMock(side_effect=deferred)

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    assert "request_in_flight" in (result.target_reason or "")
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_generation_serialized_reentry_retains_wrapped_native_parent_deferral(tmp_path, monkeypatch):
    """The second lookup under the owner lock uses the same durable boundary."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    deferred = _admission_deferral()
    wrapped = ParentOperationalError("native parent unavailable after serialization")
    wrapped.__cause__ = deferred
    engine._get_authoritative_parent_number = MagicMock(side_effect=[None, wrapped])

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert engine._get_authoritative_parent_number.call_count == 2
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_generation_serialized_snapshot_retains_bare_admission_deferral(tmp_path, monkeypatch):
    """A later strict read under owner serialization uses durable retention."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 7)
    assert slots.reserve(owner) is True
    engine.implementation_slots = slots
    engine._get_implementation_slots = MagicMock(return_value=slots)
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._standalone_relationship_is_current = MagicMock(return_value=True)
    deferred = _admission_deferral()
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = [dict(issue), deferred]

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert engine.github.get_issue_dispatch_snapshot_strict.call_count == 2
    assert result.success is False
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    assert obligation.unfinished_effects == (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE)
    assert "reason=request_in_flight" in (result.target_reason or "")
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


@pytest.mark.parametrize("refused_read", ["current-admission", "final-dispatch"])
def test_later_strict_snapshot_retains_bare_admission_deferral(tmp_path, monkeypatch, refused_read):
    """Generic error handling cannot flatten later typed snapshot refusals."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._standalone_relationship_is_current = MagicMock(return_value=True)
    engine._reconcile_sibling_dependencies = MagicMock(return_value=DependencySatisfaction.SATISFIED)
    deferred = _admission_deferral()
    refused_call = {"current-admission": 3, "final-dispatch": 4}[refused_read]
    calls = 0

    def strict_snapshot(_repo_name, _item_number):
        nonlocal calls
        calls += 1
        if calls == refused_call:
            raise deferred
        return dict(issue)

    engine.github.get_issue_dispatch_snapshot_strict.side_effect = strict_snapshot

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert calls == refused_call
    assert result.success is False
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    assert obligation.unfinished_effects == (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE)
    assert "reason=request_in_flight" in (result.target_reason or "")
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_future_retained_deadline_blocks_duplicate_common_dispatch(tmp_path, monkeypatch):
    """An automatic duplicate cannot refresh a retained target before its deadline."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    retained = store.defer(identity, _admission_deferral(), (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE))

    result = engine._process_single_candidate_unified(
        "owner/repo",
        Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
        engine.config,
        origin="worker",
    )

    unchanged = store.get(identity)
    assert unchanged is not None
    assert unchanged.not_before == retained.not_before
    assert unchanged.unfinished_effects == retained.unfinished_effects
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.refill_retry_required is True
    assert f"retry_at={retained.not_before}" in (result.target_reason or "")
    engine.github.get_issue_dispatch_snapshot_strict.assert_not_called()

    assert store.manual_retry(identity, now=time.time() - 1) is not None
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = _admission_deferral()
    resumed = engine._process_single_candidate_unified(
        "owner/repo",
        Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
        engine.config,
        origin="worker",
    )
    assert resumed.target_outcome is ExplicitTargetOutcome.DEFERRED
    engine.github.get_issue_dispatch_snapshot_strict.assert_called_once_with("owner/repo", 7)


def test_future_retained_deadline_blocks_capacity_refill_before_strict_read(tmp_path, monkeypatch):
    """An unrelated capacity wake cannot bypass the retained target deadline."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[OpenGitHubIssue(7)]))
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine._process_single_candidate = MagicMock()
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    retained = store.defer(identity, _admission_deferral(), (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE))

    completed = asyncio.run(engine._refill_normal_implementation_slots("owner/repo"))

    unchanged = store.get(identity)
    assert completed is True
    assert unchanged is not None
    assert unchanged.not_before == retained.not_before
    assert unchanged.unfinished_effects == retained.unfinished_effects
    engine.github.get_issue_dispatch_snapshot_strict.assert_not_called()
    engine._process_single_candidate.assert_not_called()


def test_future_retained_deadline_blocks_invalidation_worker_before_strict_read(tmp_path, monkeypatch):
    """A duplicate durable notification cannot bypass pending-work timing."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._defer_observed_dependency_wait = MagicMock(return_value=False)
    engine._process_single_candidate = MagicMock()
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    retained = store.defer(identity, _admission_deferral(), (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE))

    async def scenario():
        await engine.invalidate_entity("owner/repo", "issue", 7)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0, "issue"))
        try:
            for _ in range(200):
                deferred = engine.invalidations.get_deferred(EntityIdentity("owner/repo", "issue", 7))
                if deferred is not None and engine.active_workers.get(0) is None:
                    return deferred
                await asyncio.sleep(0.01)
            raise AssertionError("invalidation was not retained at the pending-work deadline")
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    invalidation = asyncio.run(scenario())
    unchanged = store.get(identity)
    assert unchanged is not None
    assert unchanged.not_before == retained.not_before
    assert unchanged.unfinished_effects == retained.unfinished_effects
    assert invalidation.retry_not_before == retained.not_before
    engine.github.get_issue_dispatch_snapshot_strict.assert_not_called()
    engine._defer_observed_dependency_wait.assert_not_called()
    engine._process_single_candidate.assert_not_called()


@pytest.mark.parametrize(
    ("later_failure", "expected_reason"),
    [
        pytest.param(
            _github_error(GitHubApiOutcome.AUTHENTICATION_FAILURE, status=401),
            PendingReason.AUTHENTICATION,
            id="authentication",
        ),
        pytest.param(ValueError("malformed Issue snapshot"), PendingReason.EVALUATION_FAILED, id="malformed-response"),
    ],
)
def test_due_resumption_preserves_effects_after_later_snapshot_failure(tmp_path, monkeypatch, later_failure, expected_reason):
    """A failed common evaluation is not an acknowledgement of retained effects."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._get_authoritative_parent_number = MagicMock(return_value=None)
    engine._standalone_relationship_is_current = MagicMock(return_value=True)
    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    retained = store.defer(identity, _admission_deferral(), (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE))
    assert store.manual_retry(identity, now=time.time() - 1) is not None
    calls = 0

    def strict_snapshot(_repo_name, _item_number):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise later_failure
        return dict(issue)

    engine.github.get_issue_dispatch_snapshot_strict.side_effect = strict_snapshot
    handler = _IssueProcessingStageHandler(engine, "owner/repo")
    scheduler = PendingWorkScheduler(store, repository="owner/repo")

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        asyncio.run(scheduler._run_claimed(retained, handler.dispatch))

    unfinished = store.get(identity)
    assert calls == 4
    assert unfinished is not None
    assert unfinished.reason is expected_reason
    assert unfinished.not_before == 0
    assert unfinished.unfinished_effects == retained.unfinished_effects
    assert unfinished.status == "waiting"
    assert store.due(time.time() + 3600) == []
    implementation.assert_not_called()


def test_ordinary_worker_reports_retained_issue_as_deferred(tmp_path, monkeypatch):
    """A successfully retained refusal is pending work, not a worker failure."""
    issue = {
        "id": 70,
        "number": 7,
        "title": "Carried title",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine.config.jules_mode = False
    deferred = _admission_deferral()
    engine.github.get_issue_dispatch_snapshot_strict.side_effect = deferred
    monkeypatch.setattr("auto_coder.automation_engine.is_item_closed_on_github", lambda *_args: False)
    console = io.StringIO()
    log_file = tmp_path / "worker.log"
    setup_logger(log_level="INFO", log_file=str(log_file), stream=console)

    async def scenario():
        await engine.queue.put(Candidate(type="issue", data=dict(issue), priority=0, issue_number=7))
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0, "issue"))
        try:
            await asyncio.wait_for(engine.queue.join(), 5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    try:
        with patch("auto_coder.automation_engine.get_trace_logger") as trace_logger:
            asyncio.run(scenario())
    finally:
        setup_logger(log_level="INFO")

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert obligation is not None
    for output in (console.getvalue(), log_file.read_text()):
        assert "Worker 0 deferred issue #7" in output
        assert "repository=owner/repo" in output
        assert "stage=issue-processing" in output
        assert "reason=request_in_flight" in output
        assert "api_origin=https://api.github.com" in output
        assert f"retry_at={obligation.not_before}" in output
        assert "failed to process issue #7" not in output
        assert "successfully processed issue #7" not in output
    worker_events = [call for call in trace_logger.return_value.log.call_args_list if len(call.args) > 1 and call.args[1] == "Worker 0 deferred issue #7"]
    assert len(worker_events) == 1
    assert worker_events[0].kwargs["details"]["outcome"] == "deferred"


def test_final_ownership_freshness_retains_native_parent_deferral(tmp_path, monkeypatch):
    issue = {
        "id": 70,
        "number": 7,
        "title": "T",
        "body": "## Requirements\nREQ-001: Preserve behavior.",
        "labels": [{"name": "implementation-ready"}],
        "state": "open",
        "user": {"id": 1},
    }
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    engine._reconcile_sibling_dependencies = MagicMock(return_value=DependencySatisfaction.SATISFIED)
    engine.github.get_open_sub_issues_strict = MagicMock(return_value=[])
    engine._preflight_explicit_issue_relationships = MagicMock(side_effect=lambda _repo, _number: dict(issue))
    deferred = _admission_deferral()
    original_relationship_check = engine._standalone_relationship_is_current
    relationship_checks = 0

    def defer_on_final_relationship_check(repo_name, issue_number, snapshot):
        nonlocal relationship_checks
        relationship_checks += 1
        if relationship_checks == 1:
            return True
        return original_relationship_check(repo_name, issue_number, snapshot)

    engine._standalone_relationship_is_current = MagicMock(side_effect=defer_on_final_relationship_check)

    def native_parent(_repo_name, _issue_number, _snapshot):
        if relationship_checks >= 2:
            raise deferred
        return None

    engine._get_authoritative_parent_number = MagicMock(side_effect=native_parent)

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert relationship_checks == 2
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED, result.error
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_retained_owner_family_recheck_retains_admission_deferral(tmp_path, monkeypatch):
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
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 7)
    execution_id = slots.start_execution(owner)
    assert execution_id is not None
    assert slots.record_provider_session(owner, "provider-session") is True
    slots.finish_execution(owner, execution_id)
    engine.implementation_slots = slots
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    engine._preflight_explicit_issue_relationships = MagicMock(return_value=dict(issue))
    deferred = _admission_deferral()
    engine._fetch_authoritative_decomposition_set = MagicMock(side_effect=[family, deferred])

    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified(
            "owner/repo",
            Candidate(type="issue", data=dict(issue), priority=0, issue_number=7),
            engine.config,
            origin="capacity-refill-intake",
        )

    identity = WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue))
    obligation = store.get(identity)
    assert engine._fetch_authoritative_decomposition_set.call_count == 2
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert obligation is not None
    assert obligation.reason is PendingReason.ADMISSION_DEFERRED
    assert obligation.not_before >= deferred.retry_at
    assert slots.has_provider_sessions(owner) is True
    engine.pending_work_scheduler.wake.assert_called_once_with()
    implementation.assert_not_called()


def test_retained_owner_parent_generation_preflight_isolates_transport_failure(tmp_path, monkeypatch):
    """Reproduce the retained-owner -> preflight -> family -> native-parent stack."""
    issue = {"id": 70, "number": 7, "title": "T", "body": "Parent-Issue: #6\n## Requirements\nREQ-001: Preserve behavior.", "labels": [], "state": "open", "user": {"id": 1}}
    parent = {**issue, "id": 60, "number": 6, "body": "## Objective\nCoordinate work.", "labels": [{"name": "implementation-ready"}]}
    engine, store = _admitted_issue_engine(monkeypatch, tmp_path, issue)
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    owner = ImplementationOwner("issue", 7)
    execution_id = slots.start_execution(owner)
    assert execution_id is not None
    assert slots.record_provider_session(owner, "provider-session") is True
    slots.finish_execution(owner, execution_id)
    engine.implementation_slots = slots
    engine._get_authoritative_parent_number = MagicMock(return_value=6)
    engine.github.get_open_issue_declarations = MagicMock(return_value=[])
    engine.github.get_issue_dispatch_snapshot_strict = MagicMock(side_effect=lambda _repo, number: dict(parent if number == 6 else issue))
    error = _github_error(GitHubApiOutcome.TRANSPORT_FAILURE, delivery=DeliveryCertainty.INDETERMINATE, status=None)

    def native_parent(_repo, number):
        if number == 6:
            raise error
        return dict(parent)

    engine.github.get_parent_issue_details_strict = MagicMock(side_effect=native_parent)
    engine._reconcile_parent_issue = AutomationEngine._reconcile_parent_issue.__get__(engine)
    engine._reconcile_declared_family = MagicMock()
    family_reads = 0

    def family(repo, number):
        nonlocal family_reads
        family_reads += 1
        if family_reads == 1:
            return dict(parent), [dict(issue)]
        return AutomationEngine._fetch_authoritative_decomposition_set(engine, repo, number)

    engine._fetch_authoritative_decomposition_set = family
    with patch.object(engine, "_process_single_candidate_reserved") as implementation:
        result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), engine.config, origin="capacity-refill-intake")

    assert family_reads == 2
    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    assert result.error == "GitHub request failed: transport_failure"
    assert result.refill_retry_required is True
    obligation = store.get(WorkIdentity("owner/repo", "issue:7", ISSUE_PROCESSING_STAGE, _issue_content_revision(issue)))
    assert obligation is not None
    assert obligation.reason is PendingReason.INDETERMINATE
    assert slots.has_provider_sessions(owner) is True
    assert slots.active_owners() == (owner,)
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
