"""
Main automation engine for Auto-Coder.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, cast

import httpx

from . import fix_to_pass_tests_runner as fix_to_pass_tests_runner_module
from .adversarial_validation_scheduler import AdversarialValidationScheduler
from .automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ExplicitTargetOutcome, ProcessResult, PRProcessingOutcome
from .backend_manager import LLMBackendManager, get_llm_backend_manager, run_llm_prompt
from .candidate_queue import CandidateQueue
from .decomposition_analyzer import DecompositionIssue
from .decomposition_validation_lifecycle import DecompositionDecision, DecompositionValidationLifecycle
from .deployment_channel import repository_dispatch_authority
from .entity_invalidation import ClaimedInvalidation, DurableInvalidationQueue, EntityIdentity, issue_stabilization_deadline
from .exceptions import AutoCoderRetryableBackendError
from .execution_trace import EventKind, Outcome, current_scope, get_trace_collector
from .fix_to_pass_tests_runner import fix_to_pass_tests
from .git_branch import extract_number_from_branch, git_commit_with_retry, git_pull
from .git_commit import git_push
from .git_info import get_current_branch
from .github_ci_observer import ci_read_phase_method, end_ci_read_phase
from .github_pending_work import PendingObligation, PendingWorkScheduler, StageOutcome, WorkIdentity, get_pending_work_store
from .github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from .health_monitor import get_health_monitor, heartbeat, install_asyncio_diagnostics
from .implementation_slots import (
    ImplementationHierarchyConflict,
    ImplementationHierarchyUnavailable,
    ImplementationOwner,
    ImplementationOwnerResolutionError,
    ImplementationSlotRepository,
)
from .issue_context import get_linked_issues_context
from .issue_processor import create_feature_issues
from .jules_client import invalidate_jules_sessions_cache
from .jules_engine import check_and_resume_or_archive_sessions, check_and_start_recurrent_jules_tasks
from .label_manager import LabelManager
from .llm_backend_config import active_repo_context
from .logger_config import get_logger
from .merge_operation_scheduler import get_merge_operation_scheduler
from .merge_operation_state import MergeOperation
from .parent_issue_reconciliation import ParentDeclarationStatus, ParentOperationalError, ParentSpecificationError, parse_parent_declaration
from .pr_processor import PR_PROCESSING_STAGE
from .pr_processor import _create_pr_analysis_prompt as _engine_pr_prompt
from .pr_processor import _get_pr_diff as _pr_get_diff
from .pr_processor import _should_skip_waiting_for_jules, process_pull_request
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .requirement_contract import REQUIREMENT_CONTRACT_PARSER_VERSION, build_normative_issue_manifest
from .shutdown_context import install_admission_check, reset_admission_check
from .sibling_dependencies import (
    BlockedByDeclarationStatus,
    DependencySatisfaction,
    GraphValidity,
    IssueEvidence,
    IssueState,
    IssueType,
    evaluate_family_graph,
    parse_blocked_by_declaration,
)
from .specification_analyzer import IndividualRelationshipContext
from .specification_validation_lifecycle import (
    DIAGNOSTIC_EFFECT,
    READINESS_WITHDRAWAL_EFFECT,
    VALIDATION_PUBLICATION_STAGE,
    SpecificationValidationLifecycle,
    ValidationDecision,
    configured_provider_identity,
    validation_publication_identity,
)
from .test_log_utils import extract_important_errors
from .test_result import TestResult
from .trace_logger import get_trace_logger
from .update_manager import check_for_updates_and_restart
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, GitHubClient, InvalidSubIssueRelationshipError, get_ghapi_client, is_implementation_ready, parse_parent_issue_number, parse_parent_issue_url_number, resolve_authoritative_item_type
from .util.github_action import check_and_handle_closed_state, get_github_actions_logs_from_url, is_item_closed_on_github
from .util.github_cache import get_github_cache
from .util.github_request_outcome import GitHubRequestError, configure_github_request_boundary
from .utils import CommandExecutor, get_target_container, log_action
from .validation_scheduler import ValidationAdmissionDeferred, ValidationJob, ValidationScheduler

logger = get_logger(__name__)

JULES_SESSION_LIST_REFRESH_INTERVAL_SECONDS = 60 * 60
MAINTENANCE_INTERVAL_SECONDS = 60
CAPACITY_STATE_CHECK_INTERVAL_SECONDS = 1
REFILL_RETRY_INTERVAL_SECONDS = 60
INVALID_REQUIREMENT_CONTRACT_MARKER_PREFIX = "auto-coder-invalid-requirement-contract"
INVALID_DEPENDENCY_MARKER_PREFIX = "auto-coder-invalid-sibling-dependency"
STARTUP_RECONCILIATION_STAGE = "startup-reconciliation"
STARTUP_RECONCILIATION_EFFECT = "startup-scan"
# Pending-work stage for an Issue hierarchy/readiness evaluation interrupted by
# a GitHub operational failure. See PR_PROCESSING_STAGE in pr_processor.py for
# the equivalent PR-side stage.
ISSUE_PROCESSING_STAGE = "issue-processing"
ISSUE_PROCESSING_REFRESH_EFFECT = "authoritative-refresh"


def _issue_content_revision(issue_data: Dict[str, Any]) -> str:
    """Stable fingerprint of the Issue inputs an evaluation decision relied on.

    Used to detect a changed title/body between deferral and resumption
    (REQ-003): a resumed evaluation must not authorize an action using
    observations that were current for a different revision of the Issue.
    """
    title = str(issue_data.get("title") or "")
    body = str(issue_data.get("body") or "")
    return hashlib.sha256(f"{title}\x1f{body}".encode("utf-8", "surrogatepass")).hexdigest()


_TARGET_OUTCOME_TO_TRACE_OUTCOME = {
    ExplicitTargetOutcome.SUCCESS: Outcome.COMPLETED,
    ExplicitTargetOutcome.DEFERRED: Outcome.DEFERRED,
    ExplicitTargetOutcome.SKIPPED: Outcome.SKIPPED,
    ExplicitTargetOutcome.BLOCKED: Outcome.BLOCKED,
    ExplicitTargetOutcome.FAILED: Outcome.FAILED,
}


def _record_issue_stage_result(
    item_number: int,
    stage_id: str,
    label: str,
    outcome: Outcome,
    facts: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an Issue stage-result event using whatever execution scope is active.

    Uses the ambient ``ExecutionScope`` bound by the enclosing candidate
    evaluation (REQ-002); when none is bound the event is retained as
    legacy/unscoped by ``TraceCollector`` rather than inventing one. A
    diagnostic-recorder failure is caught here and never propagates into the
    admission/dispatch decision it is describing (REQ-008).
    """
    try:
        get_trace_collector().record_event(
            EventKind.STAGE_RESULT,
            stage_id=stage_id,
            origin=stage_id,
            label=label,
            outcome=outcome,
            facts=facts,
        )
    except Exception:
        logger.opt(exception=True).debug("Diagnostic trace recording failed for issue#{} stage {}; continuing", item_number, stage_id)


def _record_pr_stage_result(
    pr_number: int,
    stage_id: str,
    label: str,
    outcome: Outcome,
    facts: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a PR stage-result event for a boundary reached before any execution scope opens.

    ``_PrProcessingStageHandler`` and ``_MergeOperationResumeHandler`` can
    discover a superseded/stale head and discard the obligation before ever
    calling ``AutomationEngine._process_single_candidate`` (REQ-001 of Issue
    #1946: strict-refresh failures or superseded-head exits before common
    processing must still emit their own execution-scoped evidence). A
    diagnostic-recorder failure here is caught and never affects the
    resumption decision it describes (REQ-008).
    """
    try:
        get_trace_collector().record_event(
            EventKind.STAGE_RESULT,
            stage_id=stage_id,
            origin=stage_id,
            label=label,
            outcome=outcome,
            facts=facts,
        )
    except Exception:
        logger.opt(exception=True).debug("Diagnostic trace recording failed for pr#{} stage {}; continuing", pr_number, stage_id)


def _map_candidate_result_outcome(result: "CandidateProcessingResult") -> Outcome:
    """Derive an honest execution outcome from what the boundary actually reported.

    Only fields the producing code already set are consulted (REQ-007): an
    explicit ``target_outcome`` is authoritative when present, otherwise the
    ordinary success/error/deferral flags decide. Anything this mapping
    cannot classify from real evidence stays UNKNOWN rather than defaulting
    to a successful outcome (REQ-001).
    """
    target_outcome = result.target_outcome
    if isinstance(target_outcome, str):
        try:
            target_outcome = ExplicitTargetOutcome(target_outcome)
        except ValueError:
            target_outcome = None
    if target_outcome is not None and target_outcome in _TARGET_OUTCOME_TO_TRACE_OUTCOME:
        return _TARGET_OUTCOME_TO_TRACE_OUTCOME[target_outcome]
    if result.capacity_deferred or result.refill_retry_required:
        return Outcome.DEFERRED
    if result.error:
        return Outcome.FAILED
    if result.success:
        return Outcome.COMPLETED
    return Outcome.UNKNOWN


class _StartupReconciliationHandler:
    """Retries the durable startup-recovery obligation via the pending-work scheduler.

    Startup recovery has no partial, non-idempotent mutation of its own: it
    only reads authoritative GitHub state and durably (re)invalidates the
    entities it discovers, through the same path used for webhooks. An
    obligation left 'running' by a controller that stopped mid-dispatch is
    therefore simply retried the same way as a fresh dispatch.
    """

    def __init__(self, engine: "AutomationEngine", repo_name: str) -> None:
        self._engine = engine
        self._repo_name = repo_name

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        return self._run()

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        return self._run()

    def _run(self) -> StageOutcome:
        engine = self._engine
        loop = engine._loop
        assert loop is not None, "startup reconciliation obligation dispatched before the engine loop started"
        try:
            asyncio.run_coroutine_threadsafe(engine._attempt_startup_reconciliation(self._repo_name), loop).result()
        except GitHubRequestError as exc:
            return StageOutcome(error=exc)
        assert engine._startup_reconciliation_event is not None
        loop.call_soon_threadsafe(engine._startup_reconciliation_event.set)
        return StageOutcome(completed_effects=(STARTUP_RECONCILIATION_EFFECT,))


class _PrProcessingStageHandler:
    """Resumes a PR evaluation deferred by a GitHub operational failure.

    Dispatch always re-establishes the PR's current head before authorizing
    anything: it never resends whatever decision was in flight when the
    original ``GitHubRequestError`` was raised. Resumption goes through
    ``AutomationEngine._process_single_candidate`` -- the same production
    entrypoint the ordinary worker pool uses -- so LabelManager ownership,
    author allow-listing, and every other admission gate apply identically to
    a resumed evaluation and a freshly discovered one (REQ-004).
    """

    def __init__(self, engine: "AutomationEngine", repo_name: str) -> None:
        self._engine = engine
        self._repo_name = repo_name

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def _run(self, obligation: PendingObligation) -> StageOutcome:
        entity = obligation.identity.entity
        pr_number: Optional[int] = None
        if entity.startswith("pr:"):
            try:
                pr_number = int(entity.split(":", 1)[1])
            except ValueError:
                pr_number = None
        if pr_number is None:
            logger.warning("Malformed PR pending-work identity {!r}; discarding obligation", entity)
            return StageOutcome(superseded=True)
        # Opening this resumption's own execution scope here -- before the
        # strict-refresh read and the superseded-head check -- gives it a
        # fresh execution identity even when it never reaches
        # ``_process_single_candidate`` (REQ-001 of Issue #1946). When that
        # call is reached, ``_process_single_candidate_unified`` detects it
        # is already nested in this same (repository, "pr", pr_number) scope
        # and continues it rather than opening a second one.
        try:
            handle_cm = get_trace_collector().start_execution(
                repository=self._repo_name,
                item_type="pr",
                item_number=pr_number,
                origin="pr-pending-work-resumption",
                stage_id="pr.pending-work-resume",
                label=f"pr#{pr_number} pending-work resumption",
            )
        except Exception:
            logger.opt(exception=True).debug("Diagnostic trace recording failed opening execution scope for pr#{}; continuing untraced", pr_number)
            outcome, _result = self._run_impl(obligation, pr_number)
            return outcome
        with handle_cm as handle:
            outcome, result = self._run_impl(obligation, pr_number)
            try:
                if result is not None:
                    handle.set_outcome(_map_candidate_result_outcome(result))
                elif outcome.superseded:
                    handle.set_outcome(Outcome.SUPERSEDED)
                elif outcome.error is not None:
                    handle.set_outcome(Outcome.DEFERRED)
                else:
                    handle.set_outcome(Outcome.UNKNOWN)
            except Exception:
                logger.opt(exception=True).debug("Diagnostic trace recording failed finishing execution scope for pr#{}; continuing", pr_number)
            return outcome

    def _run_impl(self, obligation: PendingObligation, pr_number: int) -> tuple[StageOutcome, Optional["CandidateProcessingResult"]]:
        engine = self._engine
        try:
            raw_pr = engine.github.get_pull_request_metadata_strict(self._repo_name, pr_number)
        except GitHubRequestError as exc:
            _record_pr_stage_result(pr_number, "pr.strict-refresh", f"pr#{pr_number} strict refresh", Outcome.DEFERRED, {"reason": str(exc), "phase": "pending-work-resumption"})
            return StageOutcome(error=exc), None
        pr_data = engine.github.get_pr_details(raw_pr)
        current_head = str((pr_data.get("head") or {}).get("sha") or "")
        # A changed head since deferral means the retained observations no
        # longer describe the PR being resumed. Discard this obligation and
        # let normal invalidation/webhook handling evaluate the new head on
        # its own terms rather than fabricating a re-evaluation here
        # (REQ-003, REQ-006).
        if obligation.identity.revision and current_head != obligation.identity.revision:
            _record_pr_stage_result(
                pr_number,
                "pr.pending-work-resume-refresh",
                f"pr#{pr_number} pending-work resume refresh",
                Outcome.SUPERSEDED,
                {"expected_head": obligation.identity.revision, "current_head": current_head},
            )
            return StageOutcome(superseded=True), None
        result = engine._process_single_candidate(self._repo_name, Candidate(type="pr", data=pr_data, priority=0), origin="pr-pending-work-resumption")
        if result.target_outcome is ExplicitTargetOutcome.DEFERRED:
            # The resumed evaluation hit another operational failure and has
            # already re-persisted its own obligation through the same defer
            # path; nothing further to apply here.
            return StageOutcome(), result
        return StageOutcome(completed_effects=obligation.unfinished_effects), result


class _MergeOperationResumeHandler:
    """Resumes a durable merge operation once its own retry deadline has passed.

    ``merge_operation_scheduler.MergeOperationScheduler`` owns only timing:
    it hands this handler the due ``MergeOperation`` and nothing more. This
    handler re-establishes the PR's current head before doing anything else
    and, when it still matches the operation's expected head, resumes
    through the very same ``AutomationEngine._process_single_candidate``
    entrypoint every other PR evaluation goes through (REQ-005, REQ-006).
    Normal processing re-validates CI/review/thread/mergeability conditions
    on its own before reaching ``pr_processor._merge_pr``, which is what
    actually advances the operation's still-unfinished effect through
    ``merge_operation_adapter`` -- this handler never calls GitHub itself and
    never re-implements that validation.
    """

    def __init__(self, engine: "AutomationEngine", repo_name: str) -> None:
        self._engine = engine
        self._repo_name = repo_name

    def __call__(self, operation: MergeOperation) -> None:
        pr_number = operation.identity.pr_number
        # As with ``_PrProcessingStageHandler``, this resumption's own scope
        # is opened here so a strict-refresh failure or a superseded-head
        # exit -- both before ``_process_single_candidate`` is ever called --
        # still carries a fresh execution identity (REQ-001 of Issue #1946).
        try:
            handle_cm = get_trace_collector().start_execution(
                repository=self._repo_name,
                item_type="pr",
                item_number=pr_number,
                origin="merge-operation-resumption",
                stage_id="pr.merge-operation-resume",
                label=f"pr#{pr_number} merge-operation resumption",
            )
        except Exception:
            logger.opt(exception=True).debug("Diagnostic trace recording failed opening execution scope for pr#{}; continuing untraced", pr_number)
            self._run_impl(operation, pr_number)
            return
        with handle_cm as handle:
            outcome = self._run_impl(operation, pr_number)
            try:
                handle.set_outcome(outcome)
            except Exception:
                logger.opt(exception=True).debug("Diagnostic trace recording failed finishing execution scope for pr#{}; continuing", pr_number)

    def _run_impl(self, operation: MergeOperation, pr_number: int) -> Outcome:
        engine = self._engine
        try:
            raw_pr = engine.github.get_pull_request_metadata_strict(self._repo_name, pr_number)
        except GitHubRequestError as exc:
            logger.info("Could not refresh PR #{} for merge-operation resumption: {}", pr_number, exc)
            _record_pr_stage_result(pr_number, "pr.strict-refresh", f"pr#{pr_number} strict refresh", Outcome.DEFERRED, {"reason": str(exc), "phase": "merge-operation-resumption"})
            return Outcome.DEFERRED
        pr_data = engine.github.get_pr_details(raw_pr)
        current_head = str((pr_data.get("head") or {}).get("sha") or "")
        if current_head and current_head != operation.expected_head_sha:
            # A newer head invalidates this operation's own execution
            # permission; normal invalidation/webhook handling evaluates the
            # new head on its own terms rather than this handler fabricating
            # a re-evaluation for it (REQ-006).
            from .merge_operation_state import get_merge_operation_store

            get_merge_operation_store().supersede(operation.identity)
            _record_pr_stage_result(
                pr_number,
                "pr.merge-operation-resume-refresh",
                f"pr#{pr_number} merge-operation resume refresh",
                Outcome.SUPERSEDED,
                {"expected_head": operation.expected_head_sha, "current_head": current_head},
            )
            return Outcome.SUPERSEDED
        result = engine._process_single_candidate(self._repo_name, Candidate(type="pr", data=pr_data, priority=0), origin="merge-operation-resumption")
        return _map_candidate_result_outcome(result)


class _IssueProcessingStageHandler:
    """Resumes an Issue hierarchy/readiness evaluation deferred by a GitHub failure.

    See ``_PrProcessingStageHandler`` for the equivalent PR-side contract;
    this handler applies the same freshness and admission requirements to
    Issue evaluation.
    """

    def __init__(self, engine: "AutomationEngine", repo_name: str) -> None:
        self._engine = engine
        self._repo_name = repo_name

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def _run(self, obligation: PendingObligation) -> StageOutcome:
        engine = self._engine
        entity = obligation.identity.entity
        issue_number: Optional[int] = None
        if entity.startswith("issue:"):
            try:
                issue_number = int(entity.split(":", 1)[1])
            except ValueError:
                issue_number = None
        if issue_number is None:
            logger.warning("Malformed Issue pending-work identity {!r}; discarding obligation", entity)
            return StageOutcome(superseded=True)
        try:
            fresh_issue = engine.github.get_issue_dispatch_snapshot_strict(self._repo_name, issue_number)
        except GitHubRequestError as exc:
            return StageOutcome(error=exc)
        if not isinstance(fresh_issue, dict) or fresh_issue.get("number") != issue_number or "pull_request" in fresh_issue:
            # No longer an authoritative open Issue snapshot; nothing left for
            # this obligation to authorize.
            return StageOutcome(superseded=True)
        current_revision = _issue_content_revision(fresh_issue)
        if obligation.identity.revision and current_revision != obligation.identity.revision:
            return StageOutcome(superseded=True)
        candidate = Candidate(type="issue", data=fresh_issue, priority=0, issue_number=issue_number)
        result = engine._process_single_candidate(self._repo_name, candidate, origin="issue-pending-work-resumption")
        if result.target_outcome is ExplicitTargetOutcome.DEFERRED:
            return StageOutcome()
        return StageOutcome(completed_effects=obligation.unfinished_effects)


class _ValidationPublicationStageHandler:
    """Resumes an Issue specification/decomposition BLOCKED publication.

    This is the effect-level counterpart to ``_IssueProcessingStageHandler``:
    it owns exactly the two independently-trackable effects a BLOCKED
    decision requires (the diagnostic comment and, when applicable, the
    parent readiness withdrawal), durably completed one at a time via
    ``PendingWorkStore.complete_effect`` from inside
    ``SpecificationValidationLifecycle.apply_blocked``/``apply_inherited_blocked``
    (Issue #1923, REQ-001, REQ-002, REQ-007).

    Recovery re-derives the current decision from scratch -- issue snapshot,
    hierarchy, and the durably-saved decision keyed by a freshly recomputed
    identity -- exactly as a fresh evaluation would (REQ-005). A revision
    mismatch (edited title/body, changed hierarchy, or a new readiness
    submission) supersedes the retained obligation rather than authorizing
    stale effects (REQ-003).
    """

    def __init__(self, engine: "AutomationEngine", repo_name: str) -> None:
        self._engine = engine
        self._repo_name = repo_name

    def dispatch(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def recover(self, obligation: PendingObligation) -> StageOutcome:
        return self._run(obligation)

    def _run(self, obligation: PendingObligation) -> StageOutcome:
        repo_name = self._repo_name
        entity = obligation.identity.entity
        issue_number: Optional[int] = None
        if entity.startswith("issue:"):
            try:
                issue_number = int(entity.split(":", 1)[1])
            except ValueError:
                issue_number = None
        if issue_number is None:
            logger.warning("Malformed validation-publication pending-work identity {!r}; discarding obligation", entity)
            return StageOutcome(superseded=True)
        try:
            handle_cm = get_trace_collector().start_execution(
                repository=repo_name,
                item_type="issue",
                item_number=issue_number,
                origin="validation-publication-resumption",
                stage_id="issue.validation-publication-resume",
                label=f"issue#{issue_number} validation-publication resumption",
            )
        except Exception:
            logger.opt(exception=True).debug("Diagnostic trace recording failed opening execution scope for issue#{}; continuing untraced", issue_number)
            return self._run_impl(obligation, issue_number)
        with handle_cm as handle:
            outcome = self._run_impl(obligation, issue_number)
            try:
                if outcome.error is not None:
                    handle.set_outcome(Outcome.DEFERRED)
                elif outcome.superseded:
                    handle.set_outcome(Outcome.SUPERSEDED)
                elif outcome.completed_effects:
                    handle.set_outcome(Outcome.COMPLETED)
                else:
                    handle.set_outcome(Outcome.DEFERRED)
            except Exception:
                logger.opt(exception=True).debug("Diagnostic trace recording failed finishing execution scope for issue#{}; continuing", issue_number)
            return outcome

    def _run_impl(self, obligation: PendingObligation, issue_number: int) -> StageOutcome:
        engine = self._engine
        repo_name = self._repo_name
        try:
            fresh_issue = engine.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
        except GitHubRequestError as exc:
            return StageOutcome(error=exc)
        if not isinstance(fresh_issue, dict) or fresh_issue.get("number") != issue_number or "pull_request" in fresh_issue:
            return StageOutcome(superseded=True)
        title = str(fresh_issue.get("title") or "")
        body = str(fresh_issue.get("body") or "")
        validator = engine._get_specification_validator(repo_name)
        parent_number = engine._get_authoritative_parent_number(repo_name, issue_number, fresh_issue)
        relationship_context = None
        authoritative_set: Optional[tuple[Dict[str, Any], List[Dict[str, Any]]]] = None
        if parent_number is not None:
            try:
                authoritative_set = engine._fetch_authoritative_decomposition_set(repo_name, parent_number)
            except GitHubRequestError as exc:
                return StageOutcome(error=exc)
            if authoritative_set is not None:
                relationship_context = engine._child_review_context(*authoritative_set, issue_number)
        fresh_identity = validator.identity(issue_number, title, body, relationship_context)
        if fresh_identity.key != obligation.identity.revision:
            # An edited Issue, a changed hierarchy, or a new readiness
            # submission produces a different identity; the retained
            # obligation described a now-obsolete result (REQ-003, REQ-005).
            return StageOutcome(superseded=True)
        decision = validator.store.get(fresh_identity)
        if decision is None or decision.verdict != "BLOCKED":
            return StageOutcome(superseded=True)
        try:
            if parent_number is not None:

                def _set_is_current() -> bool:
                    latest = engine._fetch_authoritative_decomposition_set(repo_name, parent_number)
                    if latest is None or not engine._is_open_issue(latest[0]) or not is_implementation_ready(latest[0]) or issue_number not in {child.get("number") for child in latest[1]}:
                        return False
                    relationship = engine._child_review_context(*latest, issue_number)
                    return validator.identity(issue_number, title, body, relationship) == decision.identity

                side_effect_error = validator.apply_inherited_blocked(engine.github, decision, parent_number, _set_is_current)
            else:
                side_effect_error = validator.apply_blocked(engine.github, decision, lambda: engine._standalone_validation_is_current(repo_name, decision))
        except GitHubRequestError as exc:
            return StageOutcome(error=exc)
        if side_effect_error:
            logger.warning("Validation publication effects remain incomplete for Issue #{}: {}", issue_number, side_effect_error)
            return StageOutcome()
        current = get_pending_work_store().get(obligation.identity)
        if current is None:
            # Every effect this obligation named has been durably completed
            # via complete_effect() from inside apply_blocked/apply_inherited_blocked.
            return StageOutcome(completed_effects=obligation.unfinished_effects)
        return StageOutcome()


class EngineLifecycle(str, Enum):
    """Observable lifecycle of the long-running automation daemon."""

    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FORCED = "forced"


class AutomationEngine:
    """Main automation engine that orchestrates GitHub and LLM integration."""

    def __init__(
        self,
        github_client: GitHubClient,
        config: Optional[AutomationConfig] = None,
    ) -> None:
        """Initialize automation engine."""
        self.github = github_client
        self.github_request_governor = GitHubRequestGovernor()
        # The boundary installs the waiting admission form: this controller runs
        # several workers against one origin, so its own pacing deferrals must
        # delay a request rather than fail it.
        configure_github_request_boundary(self.github_request_governor.admit_blocking, self.github_request_governor.observe)
        self.pending_work_scheduler = PendingWorkScheduler(get_pending_work_store())
        self.merge_operation_scheduler = get_merge_operation_scheduler()
        self.config = config or AutomationConfig()
        self.cmd = CommandExecutor()
        self.queue: asyncio.Queue[Candidate] = CandidateQueue()
        invalidation_path = Path(os.environ.get("AUTO_CODER_INVALIDATION_DB", "~/.auto-coder/entity-invalidations.sqlite3")).expanduser()
        self.invalidations = DurableInvalidationQueue(invalidation_path)
        self._invalidation_drain_lock = asyncio.Lock()
        self._invalidation_wake_event: Optional[asyncio.Event] = None
        self._refill_lock = asyncio.Lock()
        self.startup_reconciled = False
        self.startup_reconciliation_error: Optional[str] = None
        self._startup_reconciliation_event: Optional[asyncio.Event] = None
        self.active_workers: Dict[int, Optional[Candidate]] = {}
        self.open_prs_snapshot: List[Dict[str, Any]] = []
        self.open_issues_snapshot: List[Dict[str, Any]] = []
        self._wake_up_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pr_merged_or_closed: bool = False
        self.implementation_slots: Optional[ImplementationSlotRepository] = None
        self._specification_validators: Dict[str, SpecificationValidationLifecycle] = {}
        self._decomposition_validators: Dict[str, DecompositionValidationLifecycle] = {}
        self.validation_scheduler = ValidationScheduler(self.config.validation_concurrency)
        self.adversarial_validation_scheduler = AdversarialValidationScheduler(self.config.adversarial_validation_concurrency)
        self._lifecycle = EngineLifecycle.RUNNING
        self._lifecycle_lock = threading.Lock()
        self._dependency_family_locks: Dict[tuple[str, int], threading.RLock] = {}
        self._dependency_family_locks_guard = threading.Lock()
        self._shutdown_event: Optional[asyncio.Event] = None
        self._force_stop_event: Optional[asyncio.Event] = None
        self._critical_operations: Dict[asyncio.Task[Any], str] = {}
        # Full Jules discovery is deliberately delayed after startup.  Claiming
        # a cycle advances this deadline before any HTTP work begins, so a
        # failed listing cannot cause a hot retry on the next loop iteration.
        self._next_jules_session_list_refresh = time.monotonic() + JULES_SESSION_LIST_REFRESH_INTERVAL_SECONDS

        # Note: Report directories are created per repository,
        # so we do not create one here (created in _save_report)

    @property
    def lifecycle(self) -> EngineLifecycle:
        """Return the current daemon lifecycle state safely across worker threads."""
        with self._lifecycle_lock:
            return self._lifecycle

    @property
    def is_draining(self) -> bool:
        return self.lifecycle is not EngineLifecycle.RUNNING

    def request_graceful_shutdown(self, reason: str) -> bool:
        """Stop admission and request a drain. Return true for the first request."""
        with self._lifecycle_lock:
            if self._lifecycle is not EngineLifecycle.RUNNING:
                return False
            self._lifecycle = EngineLifecycle.DRAINING
        logger.warning(f"Graceful shutdown requested by {reason}; entering draining state")
        operations = list(self._critical_operations.values())
        logger.warning(f"Waiting for {len(operations)} local critical operation(s): {operations or ['none']}")
        if self._loop is not None and self._shutdown_event is not None:
            self._loop.call_soon_threadsafe(self._shutdown_event.set)
        if self._wake_up_event is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake_up_event.set)
        return True

    def request_force_stop(self, reason: str) -> None:
        """Abandon the graceful wait after an explicit second interrupt."""
        with self._lifecycle_lock:
            self._lifecycle = EngineLifecycle.FORCED
        logger.error(f"Forced shutdown requested by {reason}; local work may require restart recovery")
        if self._loop is not None and self._force_stop_event is not None:
            self._loop.call_soon_threadsafe(self._force_stop_event.set)

    async def _run_local_critical(self, description: str, function: Any, *args: Any) -> Any:
        """Own synchronous work until its thread really returns, despite cancellation."""

        async def run_in_thread() -> tuple[bool, Any]:
            # Keep BaseException on the awaiting coroutine.  Letting a shielded
            # child task finish with KeyboardInterrupt/SystemExit makes asyncio
            # treat it as a loop-level termination before the caller can observe
            # and handle the exception.
            token = install_admission_check(lambda: not self.is_draining)
            try:
                try:
                    return True, await asyncio.to_thread(function, *args)
                except BaseException as exc:
                    return False, exc
            finally:
                reset_admission_check(token)

        task = asyncio.create_task(run_in_thread(), name=f"local-critical:{description}")
        self._critical_operations[task] = description
        try:
            try:
                completed, value = await asyncio.shield(task)
            except asyncio.CancelledError:
                # asyncio cancellation cannot stop a running executor thread. Keep
                # ownership and its durable lifecycle until the real boundary.
                if self.lifecycle is not EngineLifecycle.FORCED:
                    completed, value = await asyncio.shield(task)
                else:
                    raise
            if completed:
                return value
            raise value
        finally:
            if task.done():
                self._critical_operations.pop(task, None)

    async def _wait_for_local_critical_operations(self) -> None:
        """Wait for the drain-start local work, unless force-stop is requested."""
        while self._critical_operations:
            pending = set(self._critical_operations)
            logger.info(f"Draining local critical operations: {list(self._critical_operations.values())}")
            force_wait = asyncio.create_task(self._force_stop_event.wait()) if self._force_stop_event is not None else None
            waiters = pending | ({force_wait} if force_wait is not None else set())
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if force_wait is not None:
                if force_wait in done:
                    return
                force_wait.cancel()
                await asyncio.gather(force_wait, return_exceptions=True)
            await asyncio.sleep(0)

    def _claim_jules_session_list_refresh(self) -> bool:
        """Claim the hourly full-list maintenance cycle when it is due."""
        now = time.monotonic()
        if now < self._next_jules_session_list_refresh:
            return False

        self._next_jules_session_list_refresh = now + JULES_SESSION_LIST_REFRESH_INTERVAL_SECONDS
        return True

    def notify_pr_merged_or_closed(self) -> None:
        """Signal that a PR was merged or closed, interrupting any active wait in the producer loop."""
        logger.info("PR merged or closed event received; requesting early wake-up of producer loop")
        self._pr_merged_or_closed = True
        if self._wake_up_event is not None:
            if self._loop is not None and not self._loop.is_closed():
                try:
                    self._loop.call_soon_threadsafe(self._wake_up_event.set)
                except RuntimeError:
                    self._wake_up_event.set()
            else:
                self._wake_up_event.set()

    async def _sleep_or_wake(self, sleep_time: float) -> bool:
        """Sleep for sleep_time seconds, or cut short if a PR is merged/closed.

        Returns:
            True if woken up early (by PR merge/close), False if sleep completed normally.
        """
        if self._pr_merged_or_closed:
            self._pr_merged_or_closed = False
            if self._wake_up_event is not None:
                self._wake_up_event.clear()
            logger.info("PR merged/closed event was already queued; cutting short wait time and resuming immediately")
            return True

        if self._wake_up_event is None:
            self._wake_up_event = asyncio.Event()
        self._wake_up_event.clear()

        try:
            await asyncio.wait_for(self._wake_up_event.wait(), timeout=sleep_time)
            self._pr_merged_or_closed = False
            self._wake_up_event.clear()
            logger.info("Wait time cut short by PR merged/closed event; resuming main loop immediately")
            return True
        except asyncio.TimeoutError:
            return False

    def _check_if_pr_merged_or_closed(self, candidate: Candidate, result: CandidateProcessingResult) -> bool:
        """Check if the processing result indicates a PR was merged or closed."""
        for action in result.actions:
            action_lower = action.lower()
            if "successfully merged pr" in action_lower or "merged pr #" in action_lower or "was merged" in action_lower:
                return True
            if "closed pr #" in action_lower or "closed unfixable pr" in action_lower or "closed stale jules pr" in action_lower or "closed empty jules pr" in action_lower or "closing pr" in action_lower:
                return True
        return False

    async def check_and_start_recurrent_jules_tasks_async(self, repo_name: str) -> None:
        """Scan .auto-coder/prompts/*.md files and start recurrent Jules tasks if not already running."""
        try:
            await self._run_local_critical(
                "recurrent provider scan",
                check_and_start_recurrent_jules_tasks,
                repo_name,
                self._get_implementation_slots(repo_name),
            )
        except Exception as e:
            logger.error(f"Error checking/starting recurrent Jules tasks: {e}")

    def handle_stale_jules_issue_sessions(self, repo_name: str) -> List[str]:
        """Hand issues over to backend_with_high_score when Jules times out without a PR."""
        from .issue_processor import handle_stale_jules_issue_sessions

        try:
            stale_result = handle_stale_jules_issue_sessions(
                repo_name,
                self.config,
                self.github,
                implementation_slots=self._get_implementation_slots(repo_name),
                authorize_dispatch=self._authorize_stale_jules_dispatch,
            )
            for action in stale_result.actions:
                logger.info(f"Stale Jules issue session: {action}")
            return stale_result.actions
        except Exception as e:
            logger.error(f"Error handling stale Jules issue sessions: {e}")
            return []

    def _get_specification_validator(self, repo_name: str) -> SpecificationValidationLifecycle:
        validator = self._specification_validators.get(repo_name)
        if validator is None:
            validator = SpecificationValidationLifecycle(repo_name, configured_provider_identity())
            self._specification_validators[repo_name] = validator
        return validator

    def _is_issue_specification_validation_enabled(self, repo_name: str, config: Optional[AutomationConfig] = None) -> bool:
        """Return whether individual Issue specification validation is enabled."""
        cfg = config or self.config
        if cfg is not None and getattr(cfg, "repo_name", None) == repo_name:
            return bool(getattr(cfg, "issue_specification_validation", True))
        if cfg is not None and not getattr(cfg, "issue_specification_validation", True):
            return False
        from .llm_backend_config import get_issue_specification_validation_from_config

        return get_issue_specification_validation_from_config(repo_name=repo_name)

    def _is_issue_decomposition_validation_enabled(self, repo_name: str, config: Optional[AutomationConfig] = None) -> bool:
        """Return whether parent/child decomposition validation is enabled."""
        cfg = config or self.config
        if cfg is not None and getattr(cfg, "repo_name", None) == repo_name:
            return bool(getattr(cfg, "issue_decomposition_validation", True))
        if cfg is not None and not getattr(cfg, "issue_decomposition_validation", True):
            return False
        from .llm_backend_config import get_issue_decomposition_validation_from_config

        return get_issue_decomposition_validation_from_config(repo_name=repo_name)

    def _is_pr_adversarial_validation_enabled(self, repo_name: str, config: Optional[AutomationConfig] = None) -> bool:
        """Return whether PR adversarial validation is enabled."""
        cfg = config or self.config
        if cfg is not None and getattr(cfg, "repo_name", None) == repo_name:
            return bool(getattr(cfg, "pr_adversarial_validation", True)) and bool(getattr(cfg, "ENABLE_ADVERSARIAL_VALIDATION", True))
        if cfg is not None and (not getattr(cfg, "pr_adversarial_validation", True) or not getattr(cfg, "ENABLE_ADVERSARIAL_VALIDATION", True)):
            return False
        from .llm_backend_config import get_pr_adversarial_validation_from_config

        return get_pr_adversarial_validation_from_config(repo_name=repo_name)

    def _is_pr_review_thread_gate_enabled(self, repo_name: str, config: Optional[AutomationConfig] = None) -> bool:
        """Return whether PR review thread gate is enabled."""
        cfg = config or self.config
        if cfg is not None and getattr(cfg, "repo_name", None) == repo_name:
            return bool(getattr(cfg, "pr_review_thread_gate", True))
        if cfg is not None and not getattr(cfg, "pr_review_thread_gate", True):
            return False
        from .llm_backend_config import get_pr_review_thread_gate_from_config

        return get_pr_review_thread_gate_from_config(repo_name=repo_name)

    def _is_automatic_test_fix_enabled(self, repo_name: str, config: Optional[AutomationConfig] = None) -> bool:
        """Return whether automatic test fix is enabled."""
        cfg = config or self.config
        if cfg is not None and getattr(cfg, "repo_name", None) == repo_name:
            return bool(getattr(cfg, "automatic_test_fix", True))
        if cfg is not None and not getattr(cfg, "automatic_test_fix", True):
            return False
        from .llm_backend_config import get_automatic_test_fix_from_config

        return get_automatic_test_fix_from_config(repo_name=repo_name)

    def _get_decomposition_validator(self, repo_name: str) -> DecompositionValidationLifecycle:
        validator = self._decomposition_validators.get(repo_name)
        if validator is None:
            validator = DecompositionValidationLifecycle(repo_name, configured_provider_identity())
            self._decomposition_validators[repo_name] = validator
        return validator

    def _fetch_authoritative_decomposition_set(self, repo_name: str, parent_number: int) -> Optional[tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        """Reconcile every member, then fetch one stable native direct-child set."""
        if isinstance(self.github, GitHubClient):
            self._reconcile_declared_family(repo_name, parent_number)
        parent = self.github.get_issue_dispatch_snapshot_strict(repo_name, parent_number)
        if not isinstance(parent, dict) or parent.get("number") != parent_number or "pull_request" in parent:
            return None
        if not isinstance(self.github, GitHubClient):
            members = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
            if not isinstance(members, list):
                return None
            authoritative_children = []
            for member in members:
                number = member.get("number") if isinstance(member, dict) else None
                if not isinstance(number, int) or isinstance(number, bool):
                    return None
                child = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                if not isinstance(child, dict) or child.get("number") != number or "pull_request" in child:
                    return None
                authoritative_children.append(child)
            return parent, authoritative_children
        if isinstance(self.github, GitHubClient) and parse_parent_declaration(parent.get("body")).status is not ParentDeclarationStatus.ABSENT:
            parent = self._reconcile_parent_issue(repo_name, parent_number, parent)

        previous_members: Optional[set[int]] = None
        for _attempt in range(5):
            members = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
            if not isinstance(members, list):
                return None
            member_numbers: set[int] = set()
            for member in members:
                number = member.get("number") if isinstance(member, dict) else None
                if not isinstance(number, int) or isinstance(number, bool):
                    return None
                member_numbers.add(number)
                child = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                if not isinstance(child, dict) or child.get("number") != number or "pull_request" in child:
                    return None
                if isinstance(self.github, GitHubClient) and parse_parent_declaration(child.get("body")).status is not ParentDeclarationStatus.ABSENT:
                    self._reconcile_parent_issue(repo_name, number, child)
            confirmed = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
            confirmed_numbers: set[int] = set()
            for member in confirmed:
                confirmed_number = member.get("number") if isinstance(member, dict) else None
                if not isinstance(confirmed_number, int) or isinstance(confirmed_number, bool):
                    return None
                confirmed_numbers.add(confirmed_number)
            if len(confirmed_numbers) != len(confirmed):
                return None
            if confirmed_numbers == member_numbers and (previous_members is None or previous_members == member_numbers):
                break
            previous_members = member_numbers
        else:
            raise ParentOperationalError("native direct-child membership did not stabilize")

        parent = self.github.get_issue_dispatch_snapshot_strict(repo_name, parent_number)
        parent = self._reconcile_parent_issue(repo_name, parent_number, parent)
        authoritative_children = []
        for number in sorted(confirmed_numbers):
            child = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
            child = self._reconcile_parent_issue(repo_name, number, child)
            native_parent = self.github.get_parent_issue_details_strict(repo_name, number)
            if not isinstance(child, dict) or child.get("number") != number or not isinstance(native_parent, dict) or native_parent.get("number") != parent_number:
                raise ParentOperationalError(f"native parent for child #{number} is not #{parent_number}")
            authoritative_children.append(child)
        self._require_shallow_hierarchy(repo_name, parent_number, parent, authoritative_children)
        return parent, authoritative_children

    def _reconcile_declared_family(self, repo_name: str, parent_number: int) -> None:
        """Discover and materialize all open declarations for one parent.

        Native sub-issue reads cannot prove that a separately-created sibling
        has not already declared the parent.  Therefore each pass starts from
        the complete authoritative open-Issue enumeration.  A second pass
        fences concurrent body and membership changes before any caller may
        construct a validation identity.
        """
        enumerator = getattr(self.github, "get_open_entities_strict", None)
        if not callable(enumerator):
            raise ParentOperationalError("authoritative open-Issue enumeration is unavailable")

        previous: Optional[dict[int, str]] = None
        for _attempt in range(5):
            try:
                entities = enumerator(repo_name)
                open_entities = getattr(entities, "issues", None)
                if not isinstance(open_entities, list):
                    raise ParentOperationalError("authoritative open-Issue enumeration was malformed")
                discovered: dict[int, tuple[Dict[str, Any], str]] = {}
                seen: set[int] = set()
                for entity in open_entities:
                    number = getattr(entity, "number", None)
                    if not isinstance(number, int) or isinstance(number, bool) or number in seen:
                        raise ParentOperationalError("authoritative open-Issue enumeration contained an invalid or duplicate Issue")
                    seen.add(number)
                    snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    if not isinstance(snapshot, dict) or snapshot.get("number") != number or "pull_request" in snapshot or not self._is_open_issue(snapshot):
                        raise ParentOperationalError(f"authoritative open Issue #{number} could not be confirmed")
                    declaration = parse_parent_declaration(snapshot.get("body"))
                    if declaration.status is ParentDeclarationStatus.SUPPORTED and declaration.parent_number == parent_number:
                        discovered[number] = (snapshot, str(snapshot.get("body") or ""))

                native = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
                if not isinstance(native, list):
                    raise ParentOperationalError(f"cannot establish direct-child membership for Issue #{parent_number}")
                for member in native:
                    number = member.get("number") if isinstance(member, dict) else None
                    if not isinstance(number, int) or isinstance(number, bool):
                        raise ParentOperationalError(f"direct-child membership for Issue #{parent_number} was malformed")
                    snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    declaration = parse_parent_declaration(snapshot.get("body"))
                    if declaration.status is ParentDeclarationStatus.INVALID:
                        raise ParentSpecificationError(f"Issue #{number}: {declaration.reason or 'invalid Parent-Issue declaration'}")
                    if declaration.status is ParentDeclarationStatus.SUPPORTED and declaration.parent_number != parent_number:
                        raise ParentSpecificationError(f"Parent-Issue declaration #{declaration.parent_number} conflicts with native parent #{parent_number}")

                for number, (snapshot, _body) in sorted(discovered.items()):
                    self._reconcile_parent_issue(repo_name, number, snapshot)
                generation = {number: body for number, (_snapshot, body) in discovered.items()}
                if previous == generation:
                    return
                previous = generation
            except (ParentSpecificationError, ParentOperationalError):
                raise
            except Exception as exc:
                raise ParentOperationalError(f"declaration-aware family discovery failed: {exc}") from exc
        raise ParentOperationalError("declared family did not stabilize during authoritative discovery")

    def _require_shallow_hierarchy(
        self,
        repo_name: str,
        parent_number: int,
        parent: Dict[str, Any],
        children: List[Dict[str, Any]],
    ) -> None:
        """Reject a graph member that is both a child and a parent.

        GitHub supports deeper sub-issue trees, but Auto-Coder's decomposition
        contract deliberately has exactly one relationship level.  These reads
        are strict so unavailable evidence cannot silently flatten a real tree.
        """
        parent_reader = getattr(self.github, "get_parent_issue_details_strict", None)
        child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
        if not callable(parent_reader) or not callable(child_reader):
            raise ParentOperationalError("authoritative shallow-hierarchy readers are unavailable")
        ancestor = parent_reader(repo_name, parent_number)
        if ancestor is not None:
            raise ParentSpecificationError(f"Issue #{parent_number} is both a child and a parent; nested hierarchies are unsupported")
        for child in children:
            number = child.get("number")
            descendants = child_reader(repo_name, int(cast(int, number)))
            if not isinstance(descendants, list):
                raise ParentOperationalError(f"cannot establish direct-child membership for Issue #{number}")
            if descendants:
                raise ParentSpecificationError(f"Issue #{number} is both a child and a parent; nested hierarchies are unsupported")

    def _complete_container_parent(
        self,
        repo_name: str,
        parent_number: int,
        decomposition_decision: DecompositionDecision,
        child_decisions: dict[int, ValidationDecision],
    ) -> tuple[bool, str]:
        """Close an exactly validated, fully completed parent specification set."""

        def record(completed: bool, reason: str) -> tuple[bool, str]:
            _record_issue_stage_result(
                parent_number,
                "issue.container-parent-completion",
                f"issue#{parent_number} container-parent completion",
                Outcome.COMPLETED if completed else Outcome.DEFERRED,
                {"parent_number": parent_number, "reason": reason},
            )
            return completed, reason

        try:
            current = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
            if current is None:
                return record(False, "authoritative parent/direct-child state is unavailable")
            parent, children = current
            if not self._is_open_issue(parent) or not is_implementation_ready(parent):
                return record(False, "parent is no longer open and submitted")
            decomposition = self._get_decomposition_validator(repo_name)
            if decomposition_decision.verdict != "READY" or decomposition.identity(parent, children) != decomposition_decision.identity:
                return record(False, "decomposition validation identity is stale or is not READY")
            individual = self._get_specification_validator(repo_name)
            for child in children:
                number = child.get("number")
                if not isinstance(number, int) or child.get("state") != "closed":
                    return record(False, "a direct child is no longer closed")
                decision = child_decisions.get(number)
                relationship = self._child_review_context(parent, children, number)
                identity = individual.identity(number, str(child.get("title") or ""), str(child.get("body") or ""), relationship)
                if decision is None or decision.verdict != "READY" or decision.identity != identity:
                    return record(False, f"individual validation for child #{number} is stale or is not READY")
            self.github.close_issue(repo_name, parent_number)
            closed = self.github.get_issue_dispatch_snapshot_strict(repo_name, parent_number)
            if not isinstance(closed, dict) or closed.get("state") != "closed":
                return record(False, "GitHub did not confirm parent closure")
            return record(True, "Completed - closed container parent after all direct children completed")
        except Exception as exc:
            return record(False, f"authoritative parent completion failed: {exc}")

    def _get_authoritative_parent_number(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> Optional[int]:
        """Resolve the current native parent without trusting collected hints."""
        parent_number = snapshot.get("parent_issue_number")
        if not isinstance(parent_number, int):
            parent_number = parse_parent_issue_url_number(snapshot.get("parent_issue_url"))
        if isinstance(parent_number, int):
            return parent_number
        parent_reader = getattr(self.github, "get_parent_issue_details_strict", None)
        parent = parent_reader(repo_name, issue_number) if callable(parent_reader) else None
        number = parent.get("number") if isinstance(parent, dict) else None
        return number if isinstance(number, int) and not isinstance(number, bool) else None

    def _defer_initial_issue_stabilization(self, repo_name: str, snapshot: Dict[str, Any]) -> bool:
        """Persist creation-anchored reevaluation and report whether it is deferred."""
        created_at = snapshot.get("created_at")
        number = snapshot.get("number")
        if not isinstance(created_at, str) or not isinstance(number, int):
            return False
        deadline = issue_stabilization_deadline(created_at)
        if deadline is None or deadline <= time.time():
            _record_issue_stage_result(number, "issue.creation-stabilization", f"issue#{number} creation stabilization", Outcome.COMPLETED, {"issue_number": number})
            return False
        self.invalidations.invalidate(EntityIdentity(repo_name, "issue", number), not_before=deadline)
        _record_issue_stage_result(number, "issue.creation-stabilization", f"issue#{number} creation stabilization", Outcome.DEFERRED, {"issue_number": number, "deadline": deadline})
        return True

    def _reconcile_parent_issue(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Reconcile metadata and return only a snapshot bearing that declaration."""
        current = snapshot
        for _attempt in range(5):
            declaration = parse_parent_declaration(current.get("body"))
            try:
                native = self.github.get_parent_issue_details_strict(repo_name, issue_number)
            except Exception as exc:
                raise ParentOperationalError(f"cannot read native parent: {exc}") from exc
            native_number = native.get("number") if isinstance(native, dict) else None

            if declaration.status is ParentDeclarationStatus.INVALID:
                raise ParentSpecificationError(declaration.reason or "invalid Parent-Issue declaration")
            if declaration.status is ParentDeclarationStatus.SUPPORTED:
                declared = declaration.parent_number
                assert declared is not None
                if declared == issue_number:
                    raise ParentSpecificationError("an Issue cannot declare itself as its parent")
                if isinstance(native_number, int) and native_number != declared:
                    raise ParentSpecificationError(f"Parent-Issue declaration #{declared} conflicts with native parent #{native_number}")
                if native_number is None:
                    try:
                        target = self.github.get_issue_dispatch_snapshot_strict(repo_name, declared)
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 404:
                            raise ParentSpecificationError(f"declared parent #{declared} does not exist") from exc
                        raise ParentOperationalError(f"cannot resolve declared parent #{declared}: {exc}") from exc
                    except Exception as exc:
                        raise ParentOperationalError(f"cannot resolve declared parent #{declared}: {exc}") from exc
                    if not isinstance(target, dict) or target.get("number") != declared or "pull_request" in target:
                        raise ParentSpecificationError(f"declared parent #{declared} is not an Issue in {repo_name}")
                    if not self._is_open_issue(target):
                        raise ParentSpecificationError(f"declared parent #{declared} is closed")
                    try:
                        target_parent = self.github.get_parent_issue_details_strict(repo_name, declared)
                        child_members = self.github.get_direct_sub_issues_strict(repo_name, issue_number)
                    except Exception as exc:
                        raise ParentOperationalError(f"cannot validate shallow Parent-Issue relationship: {exc}") from exc
                    if target_parent is not None:
                        raise ParentSpecificationError(f"declared parent #{declared} is already a child; nested hierarchies are unsupported")
                    if not isinstance(child_members, list):
                        raise ParentOperationalError(f"cannot establish direct-child membership for Issue #{issue_number}")
                    if child_members:
                        raise ParentSpecificationError(f"Issue #{issue_number} is already a parent and cannot become a child")
                    child_id = current.get("id")
                    if not isinstance(child_id, int) or isinstance(child_id, bool):
                        raise ParentOperationalError("authoritative child snapshot omitted its database ID")
                    try:
                        self.github.add_sub_issue_strict(repo_name, declared, issue_number, child_id)
                    except InvalidSubIssueRelationshipError as exc:
                        raise ParentSpecificationError(str(exc)) from exc
                    except Exception as exc:
                        raise ParentOperationalError(f"cannot materialize Parent-Issue relationship: {exc}") from exc

            try:
                refreshed = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
                refreshed_parent = self.github.get_parent_issue_details_strict(repo_name, issue_number)
            except Exception as exc:
                raise ParentOperationalError(f"cannot re-fetch reconciled relationship: {exc}") from exc
            if not isinstance(refreshed, dict) or refreshed.get("number") != issue_number or "pull_request" in refreshed:
                raise ParentOperationalError("GitHub returned an ambiguous reconciled Issue snapshot")
            refreshed_number = refreshed_parent.get("number") if isinstance(refreshed_parent, dict) else None
            if declaration.status is ParentDeclarationStatus.SUPPORTED and refreshed_number != declaration.parent_number:
                raise ParentOperationalError("GitHub did not confirm the materialized Parent-Issue relationship")
            if parse_parent_declaration(refreshed.get("body")) == declaration:
                return refreshed
            current = refreshed
        raise ParentOperationalError("Parent-Issue declaration did not stabilize during reconciliation")

    def _reconcile_validation_snapshot(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Reconcile declarations on an authoritative validation input."""
        if isinstance(self.github, GitHubClient) and parse_parent_declaration(snapshot.get("body")).status is not ParentDeclarationStatus.ABSENT:
            return self._reconcile_parent_issue(repo_name, issue_number, snapshot)
        return snapshot

    def _dependency_family_lock(self, repo_name: str, parent_number: int) -> threading.RLock:
        with self._dependency_family_locks_guard:
            return self._dependency_family_locks.setdefault((repo_name, parent_number), threading.RLock())

    def _reject_sibling_dependency(self, repo_name: str, issue_number: int, parent_number: int, body: str, reason: str) -> None:
        """Finish every still-current rejection effect independently."""
        digest = hashlib.sha256(f"{body}\0{parent_number}\0{reason}".encode()).hexdigest()
        marker = f"<!-- {INVALID_DEPENDENCY_MARKER_PREFIX}:{digest} -->"
        failures: list[str] = []
        try:
            current = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
            native_parent = self.github.get_parent_issue_details_strict(repo_name, issue_number)
            if current.get("body") != body or not isinstance(native_parent, dict) or native_parent.get("number") != parent_number:
                return
            comments = self.github.get_issue_comments_strict(repo_name, issue_number)
            if not any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict)):
                self.github.add_comment_to_issue(
                    repo_name,
                    issue_number,
                    f"{marker}\n## Auto-Coder sibling dependency validation\n\n"
                    f"Implementation submission is withdrawn because `{reason}`. `Blocked-By` may name only distinct Issues "
                    f"whose authoritative direct parent is #{parent_number}; use comma-separated local references such as `Blocked-By: #123, #456`. "
                    f"The readiness submission on Issue #{issue_number} and its authoritative parent #{parent_number} is being withdrawn.",
                )
        except Exception as exc:
            failures.append(f"diagnostic: {exc}")
        for target in (issue_number, parent_number):
            try:
                current = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
                native_parent = self.github.get_parent_issue_details_strict(repo_name, issue_number)
                if current.get("body") != body or not isinstance(native_parent, dict) or native_parent.get("number") != parent_number:
                    return
                target_snapshot = current if target == issue_number else self.github.get_issue_dispatch_snapshot_strict(repo_name, target)
                if is_implementation_ready(target_snapshot):
                    self.github.remove_labels(repo_name, target, [IMPLEMENTATION_READY_LABEL])
            except Exception as exc:
                failures.append(f"label #{target}: {exc}")
        if failures:
            raise ParentOperationalError("dependency rejection effects remain incomplete: " + "; ".join(failures))

    def _reconcile_sibling_dependencies(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> DependencySatisfaction:
        """Materialize and gate a complete sibling dependency family from live REST evidence."""
        parent_decl = parse_parent_declaration(snapshot.get("body"))
        if parent_decl.status is ParentDeclarationStatus.SUPPORTED:
            snapshot = self._reconcile_parent_issue(repo_name, issue_number, snapshot)
        parent_decl = parse_parent_declaration(snapshot.get("body"))
        native_parent = self.github.get_parent_issue_details_strict(repo_name, issue_number)
        parent_number = native_parent.get("number") if isinstance(native_parent, dict) else None
        standalone = native_parent is None and parent_decl.status is ParentDeclarationStatus.ABSENT
        declaration = parse_blocked_by_declaration(snapshot.get("body"), parent_decl.status, standalone=standalone)
        if standalone and declaration.status is BlockedByDeclarationStatus.SUPPORTED and declaration.dependencies == frozenset():
            return DependencySatisfaction.SATISFIED
        if declaration.status is BlockedByDeclarationStatus.INVALID and isinstance(parent_number, int):
            self._reject_sibling_dependency(repo_name, issue_number, parent_number, str(snapshot.get("body") or ""), declaration.reason or "invalid Blocked-By declaration")
            return DependencySatisfaction.INVALID
        if declaration.status is BlockedByDeclarationStatus.ABSENT and not isinstance(parent_number, int):
            return DependencySatisfaction.SATISFIED
        if not isinstance(parent_number, int):
            raise ParentOperationalError("dependency parent relationship is not yet materialized")

        with self._dependency_family_lock(repo_name, parent_number):
            # Materialize supported parent declarations before sibling membership is judged.
            current = snapshot
            declaration = parse_blocked_by_declaration(current.get("body"), parse_parent_declaration(current.get("body")).status)
            desired_refs = set(declaration.dependencies or ()) if declaration.status is BlockedByDeclarationStatus.SUPPORTED else set()
            for number in sorted(desired_refs):
                target = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                target_parent_decl = parse_parent_declaration(target.get("body"))
                if target_parent_decl.status is ParentDeclarationStatus.SUPPORTED:
                    self._reconcile_parent_issue(repo_name, number, target)

            parent_snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, parent_number)
            members = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
            member_numbers = {int(member["number"]) for member in members}
            children = []
            for number in sorted(member_numbers):
                child = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                child_parent_decl = parse_parent_declaration(child.get("body"))
                if child_parent_decl.status is ParentDeclarationStatus.SUPPORTED:
                    child = self._reconcile_parent_issue(repo_name, number, child)
                children.append(child)
            confirmed_members = self.github.get_direct_sub_issues_strict(repo_name, parent_number)
            if {int(member["number"]) for member in confirmed_members} != member_numbers:
                raise ParentOperationalError("authoritative dependency family changed during observation")
            if parent_snapshot.get("number") != parent_number:
                raise ParentOperationalError("authoritative dependency parent is unavailable")
            evidence: dict[int, IssueEvidence] = {}
            referenced: set[int] = set()
            native_by_child: dict[int, frozenset[int]] = {}
            snapshots = {int(child["number"]): child for child in children}
            for number, child in snapshots.items():
                native_items = self.github.get_blocked_by_strict(repo_name, number)
                native = frozenset(int(item["number"]) for item in native_items)
                native_by_child[number] = native
                child_decl = parse_blocked_by_declaration(child.get("body"), parse_parent_declaration(child.get("body")).status)
                referenced.update(native)
                referenced.update(child_decl.dependencies or ())
                evidence[number] = IssueEvidence(number, repo_name, IssueType.ISSUE, IssueState.CLOSED if child.get("state") == "closed" else IssueState.OPEN, parent_number, str(child.get("body") or ""), native)
            for number in referenced - set(evidence):
                try:
                    target = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        evidence[number] = IssueEvidence(number, repo_name, IssueType.ISSUE, IssueState.UNAVAILABLE, None, "", frozenset(), True)
                        continue
                    raise
                target_parent = self.github.get_parent_issue_details_strict(repo_name, number)
                evidence[number] = IssueEvidence(
                    number,
                    repo_name,
                    IssueType.PULL_REQUEST if "pull_request" in target else IssueType.ISSUE,
                    IssueState.CLOSED if target.get("state") == "closed" else IssueState.OPEN,
                    target_parent.get("number") if isinstance(target_parent, dict) else None,
                    str(target.get("body") or ""),
                    frozenset(),
                )

            graph = evaluate_family_graph(evidence, parent_number, repo_name)
            invalid = [(number, result) for number, result in graph.results.items() if result.is_valid_graph is GraphValidity.INVALID]
            if invalid:
                for number, result in invalid:
                    child = snapshots[number]
                    self._reject_sibling_dependency(repo_name, number, parent_number, str(child.get("body") or ""), result.reason or "invalid desired dependency graph")
                return DependencySatisfaction.INVALID
            if any(result.is_valid_graph is GraphValidity.UNRESOLVED for result in graph.results.values()):
                raise ParentOperationalError("desired sibling dependency graph is unresolved")

            # No graph mutation starts until every family declaration has passed.
            for number, result in graph.results.items():
                if result.declaration_status is not BlockedByDeclarationStatus.SUPPORTED:
                    continue
                desired = set(result.desired_dependencies or ())
                actual = set(native_by_child[number])
                for dependency_number in sorted(actual - desired):
                    declaring = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    declaring_parent = self.github.get_parent_issue_details_strict(repo_name, number)
                    if declaring.get("body") != snapshots[number].get("body") or not isinstance(declaring_parent, dict) or declaring_parent.get("number") != parent_number:
                        raise ParentOperationalError("dependency declaration or family membership changed before mutation")
                    dependency = self.github.get_issue_dispatch_snapshot_strict(repo_name, dependency_number)
                    self.github.mutate_blocked_by_strict(repo_name, number, int(dependency["id"]), add=False)
                for dependency_number in sorted(desired - actual):
                    declaring = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    declaring_parent = self.github.get_parent_issue_details_strict(repo_name, number)
                    if declaring.get("body") != snapshots[number].get("body") or not isinstance(declaring_parent, dict) or declaring_parent.get("number") != parent_number:
                        raise ParentOperationalError("dependency declaration or family membership changed before mutation")
                    dependency = self.github.get_issue_dispatch_snapshot_strict(repo_name, dependency_number)
                    self.github.mutate_blocked_by_strict(repo_name, number, int(dependency["id"]), add=True)

            # A complete family readback (including reverse edges) fences lost responses and stale writes.
            for number, result in graph.results.items():
                incoming = self.github.get_blocked_by_strict(repo_name, number)
                self.github.get_blocking_strict(repo_name, number)
                expected = set(result.desired_dependencies or ())
                if {int(item["number"]) for item in incoming} != expected:
                    raise ParentOperationalError("native dependency graph did not confirm desired equality")
                latest = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                if latest.get("body") != snapshots[number].get("body"):
                    raise ParentOperationalError("dependency declaration changed during reconciliation")
            return graph.results.get(issue_number, next(iter(graph.results.values()))).satisfaction

    def _preflight_explicit_issue_relationships(self, repo_name: str, issue_number: int) -> Dict[str, Any]:
        """Complete the hierarchy containing an explicit ``--only`` target.

        This deliberately performs no validation or implementation work.  The
        repository-wide enumeration is used only to find declarations which can
        change the target's native direct-child set; all policy decisions happen
        later, from a fresh authoritative read.
        """
        enumerator = getattr(self.github, "get_open_entities_strict", None)
        if not callable(enumerator):
            raise ParentOperationalError("authoritative open-Issue enumeration is unavailable")
        try:
            entities = enumerator(repo_name)
            open_entities = getattr(entities, "issues", None)
            if not isinstance(open_entities, list):
                raise ParentOperationalError("authoritative open-Issue enumeration was malformed")
            numbers: list[int] = []
            for entity in open_entities:
                number = getattr(entity, "number", None)
                if not isinstance(number, int) or isinstance(number, bool):
                    raise ParentOperationalError("authoritative open-Issue enumeration contained an invalid Issue")
                numbers.append(number)
            if len(numbers) != len(set(numbers)):
                raise ParentOperationalError("authoritative open-Issue enumeration contained duplicate Issues")

            snapshots: dict[int, Dict[str, Any]] = {}
            declarations = {}
            for number in numbers:
                snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                if not isinstance(snapshot, dict) or snapshot.get("number") != number or "pull_request" in snapshot or not self._is_open_issue(snapshot):
                    raise ParentOperationalError(f"authoritative open Issue #{number} could not be confirmed")
                declaration = parse_parent_declaration(snapshot.get("body"))
                snapshots[number] = snapshot
                declarations[number] = declaration
            if issue_number not in snapshots:
                target_snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
                if not isinstance(target_snapshot, dict) or target_snapshot.get("number") != issue_number or "pull_request" in target_snapshot:
                    raise ParentOperationalError(f"explicit Issue #{issue_number} could not be confirmed")
                target_declaration = parse_parent_declaration(target_snapshot.get("body"))
                if target_declaration.status is ParentDeclarationStatus.INVALID:
                    raise ParentSpecificationError(f"Issue #{issue_number}: {target_declaration.reason or 'invalid Parent-Issue declaration'}")
                snapshots[issue_number] = target_snapshot
                declarations[issue_number] = target_declaration

            target = snapshots[issue_number]
            if declarations[issue_number].status is ParentDeclarationStatus.INVALID:
                raise ParentSpecificationError(f"Issue #{issue_number}: {declarations[issue_number].reason or 'invalid Parent-Issue declaration'}")
            target_native = self.github.get_parent_issue_details_strict(repo_name, issue_number)
            native_parent = target_native.get("number") if isinstance(target_native, dict) else None
            target_declaration = declarations[issue_number]
            declared_parent = target_declaration.parent_number if target_declaration.status is ParentDeclarationStatus.SUPPORTED else None
            if isinstance(native_parent, int) and isinstance(declared_parent, int) and native_parent != declared_parent:
                raise ParentSpecificationError(f"Parent-Issue declaration #{declared_parent} conflicts with native parent #{native_parent}")
            affected_parent = declared_parent if isinstance(declared_parent, int) else native_parent

            target_children = self.github.get_direct_sub_issues_strict(repo_name, issue_number)
            if not isinstance(target_children, list):
                raise ParentOperationalError(f"cannot establish direct-child membership for Issue #{issue_number}")
            native_child_numbers: set[int] = set()
            for child in target_children:
                child_number = child.get("number") if isinstance(child, dict) else None
                if isinstance(child_number, int) and not isinstance(child_number, bool):
                    native_child_numbers.add(child_number)
            if len(native_child_numbers) != len(target_children):
                raise ParentOperationalError(f"direct-child membership for Issue #{issue_number} was malformed")
            if affected_parent is None and (native_child_numbers or any(declaration.parent_number == issue_number for declaration in declarations.values())):
                affected_parent = issue_number

            if affected_parent is not None:
                affected_numbers = {number for number, declaration in declarations.items() if declaration.parent_number == affected_parent}
                if affected_parent == issue_number:
                    affected_numbers.update(native_child_numbers)
                else:
                    siblings = self.github.get_direct_sub_issues_strict(repo_name, affected_parent)
                    if not isinstance(siblings, list):
                        raise ParentOperationalError(f"cannot establish direct-child membership for Issue #{affected_parent}")
                    for sibling in siblings:
                        number = sibling.get("number") if isinstance(sibling, dict) else None
                        if not isinstance(number, int) or isinstance(number, bool):
                            raise ParentOperationalError(f"direct-child membership for Issue #{affected_parent} was malformed")
                        affected_numbers.add(number)
                affected_numbers.add(issue_number)
                for number in sorted(affected_numbers):
                    snapshot = snapshots.get(number)
                    if snapshot is None:
                        snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    self._reconcile_parent_issue(repo_name, number, snapshot)
                authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, affected_parent)
                if authoritative_set is None:
                    raise ParentOperationalError(f"cannot re-read reconciled hierarchy for parent #{affected_parent}")

            refreshed = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
            if not isinstance(refreshed, dict) or refreshed.get("number") != issue_number or "pull_request" in refreshed:
                raise ParentOperationalError("GitHub returned an ambiguous explicit target after relationship preflight")
            return refreshed
        except ParentSpecificationError:
            raise
        except ParentOperationalError:
            raise
        except Exception as exc:
            raise ParentOperationalError(f"explicit relationship preflight failed: {exc}") from exc

    @staticmethod
    def _traced_validation_job(
        repo_name: str,
        item_number: int,
        stage_id: str,
        label: str,
        facts: Optional[Dict[str, Any]],
        fn: Any,
    ) -> Any:
        """Run a validation-scheduler operation inside its own fresh execution scope.

        ``ValidationScheduler.submit`` copies the submitting thread's
        contextvars into the worker-pool thread that runs ``fn``, which
        would otherwise make this asynchronous job's diagnostic evidence
        appear to belong to whichever worker most recently opened a scope
        for the parent Issue. Opening a fresh ``ExecutionScope`` here, before
        calling ``fn``, gives the job its own execution identity (REQ-002)
        instead of borrowing the caller's.

        A diagnostic-recorder failure (opening the scope or recording the
        result) is caught and logged rather than allowed to prevent ``fn``
        from running or to change the decision it returns (REQ-008).
        """
        collector = get_trace_collector()
        try:
            handle_cm = collector.start_execution(
                repository=repo_name,
                item_type="issue",
                item_number=item_number,
                origin="validation-scheduler",
                stage_id=stage_id,
                label=label,
                facts=facts,
            )
        except Exception:
            logger.opt(exception=True).debug("Diagnostic trace recording failed opening validation scope for issue#{}; continuing untraced", item_number)
            return fn()
        with handle_cm as handle:
            try:
                decision = fn()
            except BaseException:
                try:
                    handle.set_outcome(Outcome.FAILED)
                except Exception:
                    logger.opt(exception=True).debug("Diagnostic trace recording failed for issue#{} validation job; continuing", item_number)
                raise
            verdict = getattr(decision, "verdict", None)
            verdict_outcomes = {"READY": Outcome.COMPLETED, "BLOCKED": Outcome.BLOCKED, "ERROR": Outcome.FAILED}
            outcome = verdict_outcomes.get(verdict, Outcome.UNKNOWN) if isinstance(verdict, str) else Outcome.UNKNOWN
            try:
                handle.set_outcome(outcome)
                collector.record_event(
                    EventKind.STAGE_RESULT,
                    stage_id=stage_id,
                    origin="validation-scheduler",
                    label=label,
                    outcome=outcome,
                    facts={**(facts or {}), "verdict": verdict, "evaluation_source": getattr(decision, "evaluation_source", "unrecorded")},
                )
            except Exception:
                logger.opt(exception=True).debug("Diagnostic trace recording failed for issue#{} validation job; continuing", item_number)
            return decision

    def _submit_individual_validation(
        self,
        repo_name: str,
        item_number: int,
        identity_key: str,
        operation: Any,
        origin: str,
    ) -> ValidationJob[ValidationDecision]:
        """Submit every individual review through the diagnostic job boundary.

        The scheduler may return an existing future.  The producing operation is
        therefore traced inside the submitted callable, while this caller records
        only that it queued or joined the exact identity.  Diagnostic failures are
        deliberately isolated by the existing collector APIs.
        """
        facts: Dict[str, Any] = {
            "issue_number": item_number,
            "review_kind": "individual",
            "validation_identity": identity_key,
            "caller_origin": origin,
        }
        _record_issue_stage_result(
            item_number,
            "issue.individual-validation-observation",
            f"issue#{item_number} individual validation observation",
            Outcome.DEFERRED,
            {**facts, "observation": "submitted-or-joined"},
        )
        return self.validation_scheduler.submit(
            f"individual:{identity_key}",
            partial(
                self._traced_validation_job,
                repo_name,
                item_number,
                "issue.individual-validation-job",
                f"issue#{item_number} individual validation job",
                facts,
                operation,
            ),
        )

    @staticmethod
    def _consume_individual_validation(
        item_number: int,
        identity_key: str,
        job: ValidationJob[ValidationDecision],
        origin: str,
    ) -> ValidationDecision:
        """Observe a shared result without claiming the producer's execution."""
        try:
            decision = job.result()
        except BaseException as exc:
            outcome = Outcome.CANCELLED if isinstance(exc, (asyncio.CancelledError, ValidationAdmissionDeferred)) else Outcome.FAILED
            _record_issue_stage_result(
                item_number,
                "issue.individual-validation-observation",
                f"issue#{item_number} individual validation observation",
                outcome,
                {"review_kind": "individual", "validation_identity": identity_key, "caller_origin": origin, "observation": "consume-error"},
            )
            raise
        verdict = decision.verdict
        decision_identity = getattr(getattr(decision, "identity", None), "key", identity_key)
        outcome = {"READY": Outcome.COMPLETED, "BLOCKED": Outcome.BLOCKED, "ERROR": Outcome.FAILED}.get(verdict, Outcome.UNKNOWN)
        _record_issue_stage_result(
            item_number,
            "issue.individual-validation-observation",
            f"issue#{item_number} individual validation observation",
            outcome,
            {
                "review_kind": "individual",
                "validation_identity": identity_key,
                "decision_identity": decision_identity,
                "caller_origin": origin,
                "observation": "consumed",
                "verdict": verdict,
                "evaluation_source": getattr(decision, "evaluation_source", "unrecorded"),
            },
        )
        return decision

    def _schedule_parent_validations(
        self,
        repo_name: str,
        authoritative_set: tuple[Dict[str, Any], List[Dict[str, Any]]],
        config: Optional[AutomationConfig] = None,
        selected_child_number: Optional[int] = None,
    ) -> tuple[Optional[ValidationJob[DecompositionDecision]], dict[int, ValidationJob[ValidationDecision]]]:
        """Eagerly submit a stable parent generation under the shared bound."""
        parent, children = authoritative_set
        parent_number = int(parent["number"])
        if isinstance(self.github, GitHubClient):
            waiting = False
            for member in [parent, *children]:
                created_at = member.get("created_at")
                number = member.get("number")
                if not isinstance(created_at, str) or not isinstance(number, int) or issue_stabilization_deadline(created_at) is None:
                    logger.warning("Family reconciliation deferred for {}/#{}: unavailable creation timestamp on Issue #{}", repo_name, parent_number, number)
                    raise ValidationAdmissionDeferred("family member creation timestamp is unavailable")
                waiting = self._defer_initial_issue_stabilization(repo_name, member) or waiting
            if waiting:
                logger.info("Family reconciliation waiting for creation deadlines for {}/#{}", repo_name, parent_number)
                raise ValidationAdmissionDeferred("family creation stabilization deadline has not passed")
        member_numbers = sorted(int(child["number"]) for child in children)
        set_job: Optional[ValidationJob[DecompositionDecision]] = None
        if self._is_issue_decomposition_validation_enabled(repo_name, config):
            decomposition = self._get_decomposition_validator(repo_name)
            set_identity = decomposition.identity(parent, children)
            parent_manifest = build_normative_issue_manifest(int(parent["number"]), str(parent.get("title") or ""), str(parent.get("body") or ""))
            child_inputs = [
                DecompositionIssue(
                    build_normative_issue_manifest(int(child["number"]), str(child.get("title") or ""), str(child.get("body") or "")),
                    str(child.get("body") or ""),
                )
                for child in children
            ]
            set_job = self.validation_scheduler.submit(
                f"decomposition:{set_identity.key}",
                lambda: self._traced_validation_job(
                    repo_name,
                    parent_number,
                    "issue.decomposition-validation-job",
                    f"issue#{parent_number} decomposition validation job",
                    {"parent_number": parent_number, "member_issue_numbers": member_numbers},
                    lambda: decomposition.decide(set_identity, DecompositionIssue(parent_manifest, str(parent.get("body") or "")), child_inputs),
                ),
            )
        else:
            _record_issue_stage_result(
                parent_number,
                "issue.decomposition-validation-job",
                f"issue#{parent_number} decomposition validation job",
                Outcome.SKIPPED,
                {"parent_number": parent_number, "member_issue_numbers": member_numbers, "reason": "decomposition validation is disabled"},
            )
        child_jobs: dict[int, ValidationJob[ValidationDecision]] = {}
        if self._is_issue_specification_validation_enabled(repo_name, config):
            individual = self._get_specification_validator(repo_name)
            for child in children:
                number = int(child["number"])
                if selected_child_number is not None and number != selected_child_number:
                    continue
                title = str(child.get("title") or "")
                body = str(child.get("body") or "")
                manifest = build_normative_issue_manifest(number, title, body)
                relationship_context = self._child_review_context(parent, children, number)
                identity = individual.identity(number, title, body, relationship_context)
                child_jobs[number] = self._submit_individual_validation(
                    repo_name,
                    number,
                    identity.key,
                    partial(individual.decide, manifest, title, body, relationship_context),
                    "parent-child-scheduling",
                )
        return set_job, child_jobs

    @staticmethod
    def _child_review_context(parent: Dict[str, Any], children: List[Dict[str, Any]], issue_number: int) -> IndividualRelationshipContext:
        """Serialize only caller-reconciled graph evidence for child analysis."""

        def contract(issue: Dict[str, Any]) -> dict[str, object]:
            number = int(issue["number"])
            title = str(issue.get("title") or "")
            body = str(issue.get("body") or "")
            manifest = build_normative_issue_manifest(number, title, body)
            return {
                "issue_number": number,
                "relationship": "parent" if number == int(parent["number"]) else "sibling",
                "title": title,
                "normative_manifest": [{"requirement_id": item.requirement_id, "text": item.text} for item in manifest.requirements],
                "body_non_normative_evidence": body,
            }

        related = [contract(parent)] + [contract(child) for child in children if int(child["number"]) != issue_number]
        return IndividualRelationshipContext(role="child", related_contracts=json.dumps(related, ensure_ascii=False, indent=2))

    def _join_parent_validations(
        self,
        decomposition_job: Optional[ValidationJob[DecompositionDecision]],
        child_jobs: dict[int, ValidationJob[ValidationDecision]],
    ) -> tuple[Optional[DecompositionDecision], dict[int, ValidationDecision]]:
        """Join a submitted batch completely before propagating any failure."""
        decomposition_decision: Optional[DecompositionDecision] = None
        child_decisions: dict[int, ValidationDecision] = {}
        first_error: Optional[BaseException] = None
        if decomposition_job is not None:
            try:
                decomposition_decision = decomposition_job.result()
                decomposition_identity = getattr(decomposition_decision, "identity", None)
                parent_identity = getattr(decomposition_identity, "parent", None)
                ambient_scope = current_scope()
                parent_number = getattr(parent_identity, "issue_number", ambient_scope.item_number if ambient_scope is not None else -1)
                identity_key = getattr(decomposition_identity, "key", decomposition_job.identity_key.removeprefix("decomposition:"))
                _record_issue_stage_result(
                    parent_number,
                    "issue.decomposition-validation-observation",
                    f"issue#{parent_number} decomposition validation observation",
                    {"READY": Outcome.COMPLETED, "BLOCKED": Outcome.BLOCKED, "ERROR": Outcome.FAILED}.get(decomposition_decision.verdict, Outcome.UNKNOWN),
                    {
                        "review_kind": "decomposition",
                        "validation_identity": identity_key,
                        "observation": "consumed",
                        "verdict": decomposition_decision.verdict,
                    },
                )
            except BaseException as exc:
                first_error = exc
        for number, job in child_jobs.items():
            try:
                identity_key = job.identity_key.removeprefix("individual:")
                child_decisions[number] = self._consume_individual_validation(number, identity_key, job, "parent-child-consumer")
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return decomposition_decision, child_decisions

    def _standalone_relationship_is_current(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> bool:
        """Reject and, when blocked, apply a parent submission discovered late."""
        if isinstance(self.github, GitHubClient):
            snapshot = self._preflight_explicit_issue_relationships(repo_name, issue_number)
        parent_number = self._get_authoritative_parent_number(repo_name, issue_number, snapshot)
        if parent_number is None:
            return True
        authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
        if authoritative_set is None or issue_number not in {child.get("number") for child in authoritative_set[1]}:
            return False
        if is_implementation_ready(authoritative_set[0]):
            if self._defer_initial_issue_stabilization(repo_name, authoritative_set[0]):
                return False
            if self._is_issue_decomposition_validation_enabled(repo_name):
                validator = self._get_decomposition_validator(repo_name)
                decomposition_job, child_jobs = self._schedule_parent_validations(repo_name, authoritative_set)
                decision, _ = self._join_parent_validations(decomposition_job, child_jobs)
                if decision is not None and decision.verdict == "BLOCKED":
                    validator.apply_blocked(
                        self.github,
                        decision,
                        lambda number: self._fetch_authoritative_decomposition_set(repo_name, number),
                    )
        # Even READY was obtained after individual validation started, so this
        # attempt must restart through the ordered set-before-child workflow.
        return False

    def _standalone_validation_is_current(self, repo_name: str, decision: ValidationDecision) -> bool:
        """Verify that an individual identity is still the required standalone identity."""
        snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, decision.identity.issue_number)
        if not isinstance(snapshot, dict) or not self._is_open_issue(snapshot) or not is_implementation_ready(snapshot):
            return False
        if isinstance(self.github, GitHubClient):
            snapshot = self._reconcile_parent_issue(repo_name, decision.identity.issue_number, snapshot)
        validator = self._get_specification_validator(repo_name)
        if validator.identity(decision.identity.issue_number, str(snapshot.get("title") or ""), str(snapshot.get("body") or "")) != decision.identity:
            return False
        if self._get_authoritative_parent_number(repo_name, decision.identity.issue_number, snapshot) is not None:
            return False
        child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
        children = child_reader(repo_name, decision.identity.issue_number) if callable(child_reader) else []
        return isinstance(children, list) and not children

    def _validate_submitted_parent_generation_for_child(
        self,
        repo_name: str,
        issue_number: int,
        snapshot: Dict[str, Any],
        *,
        target_only: bool = False,
    ) -> None:
        """Materialize validation evidence triggered by one authoritative child change.

        Webhooks invalidate only the edited Issue.  Resolve its live relationship
        before closed-state and implementation-owner filtering so those concerns
        cannot consume the invalidation without validating the changed set.
        """
        declaration = parse_parent_declaration(snapshot.get("body"))
        if isinstance(self.github, GitHubClient):
            snapshot = self._preflight_explicit_issue_relationships(repo_name, issue_number)
            declaration = parse_parent_declaration(snapshot.get("body"))
        parent_number = self._get_authoritative_parent_number(repo_name, issue_number, snapshot)
        # This eager hook exists to discover a submitted identity. An unrelated
        # non-ready leaf has none, so leave its full reconciliation to the common
        # dispatch path rather than adding redundant authoritative reads.
        if parent_number is None and declaration.status is ParentDeclarationStatus.ABSENT:
            return
        snapshot = self._reconcile_parent_issue(repo_name, issue_number, snapshot)
        parent_number = self._get_authoritative_parent_number(repo_name, issue_number, snapshot)
        if parent_number is None:
            return
        authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
        if authoritative_set is None or issue_number not in {child.get("number") for child in authoritative_set[1]}:
            return
        if not is_implementation_ready(authoritative_set[0]):
            return
        if self._defer_initial_issue_stabilization(repo_name, authoritative_set[0]):
            return
        if target_only:
            decomposition_job, child_jobs = self._schedule_parent_validations(
                repo_name,
                authoritative_set,
                selected_child_number=issue_number,
            )
        else:
            decomposition_job, child_jobs = self._schedule_parent_validations(repo_name, authoritative_set)
        # Do not acknowledge the durable invalidation until every missing
        # identity has either persisted reusable evidence or returned ERROR.
        failures: list[str] = []
        try:
            decomposition_decision, child_decisions = self._join_parent_validations(decomposition_job, child_jobs)
            if decomposition_decision is not None and decomposition_decision.verdict == "ERROR":
                failures.append("decomposition validation failed")
            if any(decision.verdict == "ERROR" for decision in child_decisions.values()):
                failures.append("individual validation failed")
        except ValidationAdmissionDeferred:
            failures.append("validation was deferred")
        except Exception as exc:
            failures.append(f"validation raised {type(exc).__name__}")
        if failures:
            raise RuntimeError(f"validation batch incomplete while processing child invalidation: {', '.join(failures)}")

    def _authorize_stale_jules_dispatch(self, repo_name: str, issue_number: int, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Apply set, ordering, and Issue authorization to daemon replacement work."""
        current = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
        if not isinstance(current, dict) or current.get("number") != issue_number or not self._is_open_issue(current):
            return None
        if isinstance(self.github, GitHubClient) and parse_parent_declaration(current.get("body")).status is not ParentDeclarationStatus.ABSENT:
            try:
                current = self._reconcile_parent_issue(repo_name, issue_number, current)
            except (ParentSpecificationError, ParentOperationalError):
                return None

        direct_children = self.github.get_direct_sub_issues_strict(repo_name, issue_number)
        if not isinstance(direct_children, list):
            return None
        # A stale parent session cannot be replaced as standalone work. Analyze
        # the newly observed submission, but leave child selection to the normal
        # sequential candidate workflow.
        if direct_children:
            if is_implementation_ready(current):
                if self._defer_initial_issue_stabilization(repo_name, current):
                    return None
                authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, issue_number)
                if authoritative_set is None:
                    return None
                if self._is_issue_decomposition_validation_enabled(repo_name):
                    parent_validator = self._get_decomposition_validator(repo_name)
                    decomposition_job, child_jobs = self._schedule_parent_validations(repo_name, authoritative_set)
                    parent_decision, _ = self._join_parent_validations(decomposition_job, child_jobs)
                    if parent_decision is not None and parent_decision.verdict == "BLOCKED":
                        parent_validator.apply_blocked(
                            self.github,
                            parent_decision,
                            lambda number: self._fetch_authoritative_decomposition_set(repo_name, number),
                        )
            return None

        parent_number = self._get_authoritative_parent_number(repo_name, issue_number, current)
        decomposition_validator: Optional[DecompositionValidationLifecycle] = None
        decomposition_decision: Optional[DecompositionDecision] = None
        joined_child_decisions: dict[int, ValidationDecision] = {}
        decomposition_enabled = self._is_issue_decomposition_validation_enabled(repo_name)
        if parent_number is not None:
            authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
            if authoritative_set is None or issue_number not in {child.get("number") for child in authoritative_set[1]}:
                return None
            # A native child is authorized only through a live submitted parent;
            # its own label is never a fallback after the parent is withdrawn.
            if not self._is_open_issue(authoritative_set[0]) or not is_implementation_ready(authoritative_set[0]):
                return None
            if self._defer_initial_issue_stabilization(repo_name, authoritative_set[0]):
                return None
            decomposition_validator = self._get_decomposition_validator(repo_name)
            decomposition_job, child_jobs = self._schedule_parent_validations(repo_name, authoritative_set)
            decomposition_decision, joined_child_decisions = self._join_parent_validations(decomposition_job, child_jobs)
            if decomposition_enabled and decomposition_decision is not None:
                if decomposition_decision.verdict == "BLOCKED":
                    decomposition_validator.apply_blocked(
                        self.github,
                        decomposition_decision,
                        lambda number: self._fetch_authoritative_decomposition_set(repo_name, number),
                    )
                    return None
                if decomposition_decision.verdict != "READY":
                    return None
        if parent_number is None and not is_implementation_ready(current):
            return None
        if parent_number is None and self._defer_initial_issue_stabilization(repo_name, current):
            return None
        snapshot = current
        title = str(snapshot.get("title") or "")
        body = str(snapshot.get("body") or "")
        manifest = build_normative_issue_manifest(issue_number, title, body)
        if manifest.error:
            return None
        validator = self._get_specification_validator(repo_name)
        relationship_context = self._child_review_context(*cast(tuple[dict, list[dict]], authoritative_set), issue_number) if parent_number is not None else None
        individual_identity = validator.identity(issue_number, title, body, relationship_context)
        spec_validation_enabled = self._is_issue_specification_validation_enabled(repo_name)
        decision: Optional[ValidationDecision] = None
        if spec_validation_enabled:
            if parent_number is not None:
                decision = joined_child_decisions[issue_number]
            else:
                job = self._submit_individual_validation(repo_name, issue_number, individual_identity.key, lambda: validator.decide(manifest, title, body), "standalone-intake")
                decision = self._consume_individual_validation(issue_number, individual_identity.key, job, "standalone-intake")
            if decision.verdict == "BLOCKED":
                if parent_number is not None:

                    def _set_is_current() -> bool:
                        latest = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
                        if latest is None or not self._is_open_issue(latest[0]) or not is_implementation_ready(latest[0]) or issue_number not in {child.get("number") for child in latest[1]}:
                            return False
                        relationship = self._child_review_context(*latest, issue_number)
                        if validator.identity(issue_number, title, body, relationship) != decision.identity:
                            return False
                        if decomposition_enabled and decomposition_decision is not None and decomposition_validator is not None:
                            return decomposition_validator.identity(*latest) == decomposition_decision.identity
                        return True

                    validator.apply_inherited_blocked(
                        self.github,
                        decision,
                        parent_number,
                        _set_is_current,
                    )
                else:
                    validator.apply_blocked(
                        self.github,
                        decision,
                        lambda: self._standalone_validation_is_current(repo_name, decision),
                    )
                return None
            if decision.verdict != "READY":
                return None
        refreshed = self.github.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
        if not isinstance(refreshed, dict) or not self._is_open_issue(refreshed):
            return None
        refreshed_children = self.github.get_direct_sub_issues_strict(repo_name, issue_number)
        refreshed_parent = self._get_authoritative_parent_number(repo_name, issue_number, refreshed)
        if refreshed_children or refreshed_parent != parent_number:
            return None
        if parent_number is not None:
            refreshed_set = self._fetch_authoritative_decomposition_set(repo_name, parent_number)
            if refreshed_set is None or not self._is_open_issue(refreshed_set[0]) or not is_implementation_ready(refreshed_set[0]) or issue_number not in {child.get("number") for child in refreshed_set[1]}:
                return None
            if decomposition_enabled and decomposition_decision is not None and decomposition_validator is not None:
                if decomposition_validator.identity(*refreshed_set) != decomposition_decision.identity:
                    return None
        elif not is_implementation_ready(refreshed):
            return None
        refreshed_relationship = self._child_review_context(*cast(tuple[dict, list[dict]], refreshed_set), issue_number) if parent_number is not None else None
        identity = validator.identity(issue_number, str(refreshed.get("title") or ""), str(refreshed.get("body") or ""), refreshed_relationship)
        expected_identity = decision.identity if spec_validation_enabled and decision is not None else individual_identity
        if identity != expected_identity:
            return None
        if isinstance(self.github, GitHubClient) and hasattr(self.github, "token"):
            try:
                dependency_satisfaction = self._reconcile_sibling_dependencies(repo_name, issue_number, refreshed)
            except Exception:
                return None
            if dependency_satisfaction is not DependencySatisfaction.SATISFIED:
                return None
        return refreshed

    async def start_automation(self, repo_name: str, concurrency: Optional[int] = None) -> None:
        """Start the automation engine with event-driven architecture."""
        if concurrency is None:
            concurrency = self.config.MAX_CONCURRENT_TASKS

        logger.info(f"Starting automation for repository: {repo_name} with {concurrency} workers")
        self.invalidations.recover(repo_name)
        await self._enqueue_pending_invalidations(repo_name)

        # Record resource usage and unhandled asyncio errors for the whole run
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        self._force_stop_event = asyncio.Event()
        if self.is_draining:
            self._shutdown_event.set()

        # The pending-work scheduler must be available (supervising its own
        # durable obligations, including any left 'running' by a controller
        # that stopped mid-dispatch) before startup reconciliation finishes,
        # so retained work from a previous run is never orphaned by a fresh
        # enumeration that only marks entities dirty again.
        pending_work_task = asyncio.create_task(self.pending_work_scheduler.run(self._shutdown_event), name="pending-work-scheduler")
        # Registered before any await so an obligation left 'running' by a
        # crashed prior process can never be recovered while unregistered.
        self._startup_reconciliation_event = asyncio.Event()
        self.pending_work_scheduler.register_handler(STARTUP_RECONCILIATION_STAGE, _StartupReconciliationHandler(self, repo_name))
        self.pending_work_scheduler.register_handler(PR_PROCESSING_STAGE, _PrProcessingStageHandler(self, repo_name))
        self.pending_work_scheduler.register_handler(ISSUE_PROCESSING_STAGE, _IssueProcessingStageHandler(self, repo_name))
        self.pending_work_scheduler.register_handler(VALIDATION_PUBLICATION_STAGE, _ValidationPublicationStageHandler(self, repo_name))

        # A pending approval/merge effect (Issue #1939) is resumed by its own
        # dedicated scheduler rather than PR_PROCESSING_STAGE: its deadlines,
        # throttle counters, and receipts already live durably in
        # MergeOperationStore (Issue #1937), so this loop reads that store's
        # own due() timings directly instead of duplicating them into a
        # second obligation store.
        merge_operation_task = asyncio.create_task(self.merge_operation_scheduler.run(self._shutdown_event), name="merge-operation-scheduler")
        self.merge_operation_scheduler.register_resume_handler(_MergeOperationResumeHandler(self, repo_name))

        if not self.is_draining:
            # Webhooks are not a durable event log. Recover work missed while
            # this process was offline (including open PR ownership, which
            # must be discovered before releasing startup reservations)
            # before claiming steady-state correctness. A local admission
            # deferral or an actual GitHub throttle/authentication/forbidden
            # response does not terminate the daemon here: it is retained as
            # a durable pending-work obligation and retried by the
            # pending-work scheduler, independent of the ordinary worker
            # pool started below, which must not begin before recovery
            # succeeds.
            await self._perform_startup_reconciliation(repo_name)

        # Sync repo_name to environment for subprocesses (like test.sh)
        os.environ["REPO_NAME"] = repo_name

        self._wake_up_event = asyncio.Event()
        self._pr_merged_or_closed = False
        install_asyncio_diagnostics(self._loop)
        get_health_monitor().start()
        heartbeat("engine:start", repo_name)

        # Maintenance and GitHub work have independent scheduling paths.
        producer_task = asyncio.create_task(self._producer_loop(repo_name), name="producer")
        invalidation_task = asyncio.create_task(self._invalidation_loop(repo_name), name="github-invalidations")
        # Accepted Codex runs are durable and therefore need no webhook to
        # resume initial-PR recovery after registration or process restart.
        from .cloud_run import CloudRunRepository
        from .codex_observation import CodexObservationService
        from .codex_pr_recovery import CodexPRRecoveryMonitor, CodexPRRecoveryStore
        from .codex_wham_client import CodexWhamClient

        codex_recovery_task: Optional[asyncio.Task[Any]] = None
        try:
            cloud_runs = CloudRunRepository(repo_name)
            wham = CodexWhamClient()
            recovery = CodexPRRecoveryMonitor(
                cloud_runs,
                CodexObservationService(self.github, cloud_runs, wham),
                wham,
                CodexPRRecoveryStore(),
                lambda number: self.invalidate_entity(repo_name, "pr", number),
            )
            codex_recovery_task = asyncio.create_task(recovery.run(self._shutdown_event), name="codex-initial-pr-recovery")
        except Exception as exc:
            # Corrupt/unwritable claim state fails closed for reminders without
            # preventing ordinary Issue/PR work from serving the repository.
            logger.error(f"Codex initial-PR recovery is unavailable for {repo_name}: {type(exc).__name__}")
        slot_repository = self._get_implementation_slots(repo_name)
        capacity_task = asyncio.create_task(self._capacity_refill_loop(repo_name), name="implementation-capacity-refill") if isinstance(slot_repository, ImplementationSlotRepository) else None

        # Start workers
        workers = [asyncio.create_task(self._worker_loop(repo_name, i), name=f"worker-{i}") for i in range(concurrency)]

        all_loop_tasks = [producer_task, invalidation_task, pending_work_task, merge_operation_task, *workers]
        if codex_recovery_task is not None:
            all_loop_tasks.append(codex_recovery_task)
        if capacity_task is not None:
            all_loop_tasks.append(capacity_task)
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait(), name="graceful-shutdown-request")
        try:
            done, _ = await asyncio.wait({*all_loop_tasks, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)
            if shutdown_wait in done:
                # Admission stopped synchronously when the lifecycle changed.
                # Cancel loop waiters, but shielded synchronous operations retain
                # ownership and make their callers wait for the true boundary.
                for task in all_loop_tasks:
                    task.cancel()
                await self._wait_for_local_critical_operations()
                await asyncio.gather(*all_loop_tasks, return_exceptions=True)
                if self.lifecycle is EngineLifecycle.FORCED:
                    logger.error("Automation engine force-stopped before its local drain completed")
                    get_health_monitor().record_event("engine_stop", "forced", "")
                else:
                    with self._lifecycle_lock:
                        self._lifecycle = EngineLifecycle.STOPPED
                    logger.info("Automation engine graceful drain completed")
                    get_health_monitor().record_event("engine_stop", "gracefully drained", "")
                return

            shutdown_wait.cancel()
            await asyncio.gather(shutdown_wait, return_exceptions=True)
            core_loops_exited = producer_task.done() and all(worker.done() for worker in workers)
            for task in all_loop_tasks:
                task.cancel()
            await asyncio.gather(*all_loop_tasks, return_exceptions=True)
            for completed_task in done:
                if completed_task in all_loop_tasks and not completed_task.cancelled():
                    completed_task.result()
            if core_loops_exited:
                # Producer and workers returning together is the historical
                # silent-stop boundary. Companion loops are deliberately
                # cancelled because they otherwise wait indefinitely.
                logger.warning("Automation engine stopped: producer and all workers exited without being cancelled")
                get_health_monitor().record_event("engine_stop", "all engine tasks exited", f"workers={concurrency}")
            else:
                logger.warning("Automation engine stopped: an orchestration loop exited unexpectedly")
                get_health_monitor().record_event("engine_stop", "engine task exited", f"workers={concurrency}")
        except asyncio.CancelledError:
            for task in all_loop_tasks:
                task.cancel()
            await asyncio.gather(*all_loop_tasks, return_exceptions=True)
            logger.info("Automation engine stopped")
            get_health_monitor().record_event("engine_stop", "cancelled", "")
            raise
        except BaseException as e:
            logger.opt(exception=True).error(f"Automation engine stopped by an unhandled error: {e}")
            get_health_monitor().record_event("engine_stop", f"unhandled error: {type(e).__name__}: {e}", "")
            raise
        finally:
            shutdown_wait.cancel()
            get_health_monitor().log_snapshot(reason="engine_stop")

    async def _perform_startup_reconciliation(self, repo_name: str) -> None:
        """Complete startup recovery, retrying governed admission failures durably.

        The first attempt runs inline. A ``GitHubRequestError`` -- a local
        admission deferral or an actual GitHub throttle, authentication, or
        forbidden response -- does not terminate the daemon: the attempt is
        retained as a durable pending-work obligation with a stable
        repository-scoped identity (stable across restarts, since it is keyed
        only by repository and stage) and retried by the already-running
        pending-work scheduler, independent of the ordinary worker pool. This
        method then waits for that obligation to resolve -- or for shutdown --
        without blocking the event loop, so webhook receipt, status, and
        graceful shutdown remain responsive while recovery is incomplete. Any
        other exception is a genuine defect and continues to propagate.
        """
        identity = WorkIdentity(repo_name, "startup", STARTUP_RECONCILIATION_STAGE)
        try:
            await self._attempt_startup_reconciliation(repo_name)
            # A prior process may have left this identity retained (waiting on
            # a not-yet-due retry, or blocked on an operational failure) before
            # this fresh inline attempt succeeded outright; clear it so the
            # scheduler never repeats a full enumeration for already-completed
            # recovery merely because that old obligation's deadline arrives.
            await asyncio.to_thread(get_pending_work_store().supersede, identity)
            return
        except GitHubRequestError as exc:
            obligation = await asyncio.to_thread(get_pending_work_store().defer, identity, exc, (STARTUP_RECONCILIATION_EFFECT,))
            logger.warning(f"Startup GitHub reconciliation for {repo_name} deferred ({obligation.reason.value}); " f"the daemon stays up and the pending-work scheduler will retry automatically " f"(next eligible at {obligation.not_before})")
            self.pending_work_scheduler.wake()

        assert self._startup_reconciliation_event is not None and self._shutdown_event is not None
        wait_ready = asyncio.ensure_future(self._startup_reconciliation_event.wait())
        wait_shutdown = asyncio.ensure_future(self._shutdown_event.wait())
        try:
            await asyncio.wait({wait_ready, wait_shutdown}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending_wait in (wait_ready, wait_shutdown):
                if not pending_wait.done():
                    pending_wait.cancel()
            await asyncio.gather(wait_ready, wait_shutdown, return_exceptions=True)

    async def _attempt_startup_reconciliation(self, repo_name: str) -> None:
        """One full attempt at startup recovery: PR ownership, then Issue/PR enumeration.

        Used for both the inline first attempt and every scheduler-driven
        retry, so a resumed scan always performs a complete fresh discovery
        rather than resuming from stale partial state.
        """
        # Discover open PR ownership before releasing startup reservations. A
        # PR linked only by branch metadata has no Issue timeline event and
        # may not have been recorded if the previous process stopped before
        # its first candidate scan.
        await asyncio.to_thread(self._get_implementation_slots(repo_name).reconcile, self.github, True)
        await self._reconcile_open_github_entities(repo_name)

    async def _reconcile_open_github_entities(self, repo_name: str) -> None:
        """One attempt at recovery through the normal invalidation path.

        Enumeration observations never complete or clean an entity. They only
        mark its stable identity dirty; workers subsequently perform the same
        strict current-state fetch and eligibility decision used for webhooks.
        Consequently, a webhook arriving after an older enumeration observation
        cannot be cleared by reconciliation. A malformed or incomplete page
        fails the whole operation (see ``get_open_entities_strict``), so a
        caller never advertises recovery as complete from a partial result.
        """
        self.startup_reconciled = False
        self.startup_reconciliation_error = None
        try:
            entities = await asyncio.to_thread(self.github.get_open_entities_strict, repo_name)
            for issue in entities.issues:
                not_before = issue_stabilization_deadline(issue.created_at) if issue.created_at is not None else None
                await self.invalidate_entity(repo_name, "issue", issue.number, not_before=not_before)
            for number in entities.pull_requests:
                await self.invalidate_entity(repo_name, "pr", number)
        except BaseException as exc:
            self.startup_reconciliation_error = f"{type(exc).__name__}: {exc}"
            logger.opt(exception=True).error(f"Startup GitHub reconciliation failed for {repo_name}: {exc}")
            raise
        self.startup_reconciled = True
        logger.info(f"Startup GitHub reconciliation completed for {repo_name}: " f"{len(entities.issues)} Issues and {len(entities.pull_requests)} PRs invalidated")

    async def _producer_loop(self, repo_name: str) -> None:
        """Run time-based maintenance without polling GitHub for candidate work."""
        logger.info("Producer started")
        get_trace_logger().log("System", "Producer started", details={"repo_name": repo_name})

        # Check closed branch once at start (as per original run method)
        if not await self._run_local_critical("startup closed-branch reconciliation", self._check_and_handle_closed_branch, repo_name):
            logger.info("Closed item handled on startup, continuing to producer loop")
            get_health_monitor().record_event("producer_startup", "closed item handled on startup", repo_name)

        skip_jules_sessions = False
        iteration = 0
        while True:
            try:
                iteration += 1
                heartbeat("producer:check-updates", f"iteration {iteration}")
                # Check updates
                await self._run_local_critical("update check", check_for_updates_and_restart)
                if self.is_draining:
                    return

                if not skip_jules_sessions and self._claim_jules_session_list_refresh():
                    # All list-dependent maintenance shares this cycle's fresh listing.
                    invalidate_jules_sessions_cache()

                    # Resume sessions
                    heartbeat("producer:jules-sessions", f"iteration {iteration}")
                    await self._run_local_critical("remote session reconciliation", check_and_resume_or_archive_sessions, repo_name)
                    if self.is_draining:
                        return

                    # Take issues away from Jules sessions that timed out without creating a PR
                    await self._run_local_critical("stale remote session reconciliation", self.handle_stale_jules_issue_sessions, repo_name)

                    if self.is_draining:
                        return

                    # Check and start recurrent Jules tasks
                    await self.check_and_start_recurrent_jules_tasks_async(repo_name)
                    if self.is_draining:
                        return
                elif skip_jules_sessions:
                    logger.info("Resumed loop early after PR merge/close; skipping Jules session enumeration")
                    skip_jules_sessions = False

                if self.is_draining:
                    return

                heartbeat("producer:maintenance-sleep", f"{MAINTENANCE_INTERVAL_SECONDS}s")
                woken_early = await self._sleep_or_wake(MAINTENANCE_INTERVAL_SECONDS)
                if woken_early:
                    skip_jules_sessions = True

            except asyncio.CancelledError:
                logger.info("Producer loop cancelled")
                get_health_monitor().record_event("producer_exit", "cancelled", f"iteration {iteration}")
                raise
            except Exception as e:
                logger.opt(exception=True).error(f"Error in producer loop: {e}")
                get_health_monitor().record_event("producer_error", f"{type(e).__name__}: {e}", f"iteration {iteration}")
                await asyncio.sleep(60)  # Sleep on error

    async def _invalidation_loop(self, repo_name: str) -> None:
        """Consume durable invalidations promptly, including delayed Issue work."""
        self._invalidation_wake_event = asyncio.Event()
        while True:
            # Clear first so an invalidation arriving during the drain remains
            # observable instead of being erased by a later clear.
            self._invalidation_wake_event.clear()
            await self._drain_ci_webhooks(repo_name)
            await self._enqueue_pending_invalidations(repo_name)
            delay = await asyncio.to_thread(self.invalidations.seconds_until_next_ready, repo_name)
            ci_delay = await asyncio.to_thread(self.invalidations.seconds_until_next_ci, repo_name)
            if ci_delay is not None:
                delay = ci_delay if delay is None else min(delay, ci_delay)
            try:
                if delay is None:
                    await self._invalidation_wake_event.wait()
                else:
                    await asyncio.wait_for(self._invalidation_wake_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _drain_ci_webhooks(self, repo_name: str) -> None:
        """Resolve due SHA obligations, then promote quiet CI batches."""
        while sha := await asyncio.to_thread(self.invalidations.claim_ci_correlation, repo_name):
            try:
                numbers = await asyncio.to_thread(self.github.get_pull_request_numbers_for_commit, repo_name, sha)
            except Exception as exc:
                logger.warning(f"CI correlation pending repository={repo_name} sha={sha[:12]} error={type(exc).__name__}")
                await asyncio.to_thread(self.invalidations.release_ci_correlation, repo_name, sha)
                break
            await asyncio.to_thread(self.invalidations.finish_ci_correlation, repo_name, sha, numbers)
            logger.info(f"CI correlation complete repository={repo_name} sha={sha[:12]} targets={len(numbers)}")
        promoted = await asyncio.to_thread(self.invalidations.promote_due_ci, repo_name)
        promoted += await asyncio.to_thread(self.invalidations.promote_due_ci_watches, repo_name)
        if promoted:
            logger.info(f"Promoted coalesced CI batches repository={repo_name} count={promoted}")

    @staticmethod
    def _issue_refill_priority(issue: Dict[str, Any]) -> int:
        """Apply the configured candidate scan's Issue priority semantics."""
        labels = issue.get("labels", []) or []
        names = {label if isinstance(label, str) else label.get("name") for label in labels if isinstance(label, (str, dict))}
        if names.intersection({"breaking-change", "breaking", "api-change", "deprecation", "version-major"}):
            return 7
        return 3 if "urgent" in names else 0

    @staticmethod
    def _is_open_issue(issue: Dict[str, Any]) -> bool:
        """Return whether an authoritative Issue snapshot is currently open."""
        # Legacy GitHub adapters omit state for successfully fetched open
        # Issues; an explicit closed state is nevertheless authoritative.
        return str(issue.get("state") or "open").lower() == "open"

    async def _refill_normal_implementation_slots(self, repo_name: str) -> bool:
        """Evaluate one level-triggered refill obligation from fresh GitHub state.

        Returning false keeps the obligation pending. Candidate rejection is a
        completed evaluation, while an incomplete enumeration is not.
        """
        async with self._refill_lock:
            slots = self._get_implementation_slots(repo_name)
            if await asyncio.to_thread(slots.available_normal_slots) == 0:
                return True
            blocked_issue_numbers: set[int] = set()
            reconciliation_retry_required = False
            try:
                entities = await asyncio.to_thread(self.github.get_open_entities_strict, repo_name)
                candidates: List[Candidate] = []
                open_issue_snapshots: List[Dict[str, Any]] = []
                for observed in entities.issues:
                    snapshot = await asyncio.to_thread(self.github.get_issue_dispatch_snapshot_strict, repo_name, observed.number)
                    if not isinstance(snapshot, dict) or snapshot.get("number") != observed.number:
                        raise RuntimeError(f"GitHub returned an ambiguous Issue snapshot for #{observed.number}")
                    if "pull_request" in snapshot or not self._is_open_issue(snapshot):
                        continue
                    if isinstance(self.github, GitHubClient) and isinstance(snapshot.get("id"), int) and parse_parent_declaration(snapshot.get("body")).status is not ParentDeclarationStatus.ABSENT:
                        declaration = parse_parent_declaration(snapshot.get("body"))
                        try:
                            snapshot = await asyncio.to_thread(self._reconcile_parent_issue, repo_name, observed.number, snapshot)
                        except ParentSpecificationError as exc:
                            blocked_issue_numbers.add(observed.number)
                            if declaration.parent_number is not None:
                                blocked_issue_numbers.add(declaration.parent_number)
                            logger.warning("Blocked relationship reconciliation for {}/#{} during refill: {}", repo_name, observed.number, exc)
                            continue
                        except ParentOperationalError as exc:
                            reconciliation_retry_required = True
                            blocked_issue_numbers.add(observed.number)
                            if declaration.parent_number is not None:
                                blocked_issue_numbers.add(declaration.parent_number)
                            logger.warning("Deferred relationship reconciliation for {}/#{} during refill: {}", repo_name, observed.number, exc)
                            continue
                    issue_data = self.github.get_issue_details(snapshot)
                    if not isinstance(issue_data, dict) or issue_data.get("number") != observed.number:
                        raise RuntimeError(f"GitHub returned invalid Issue details for #{observed.number}")
                    open_issue_snapshots.append(issue_data)
                metadata_children: Dict[int, List[int]] = {}
                for issue_data in open_issue_snapshots:
                    if issue_data["number"] in blocked_issue_numbers:
                        continue
                    number = issue_data["number"]
                    native_parent = await asyncio.to_thread(self.github.get_parent_issue_number_strict, repo_name, number) if isinstance(self.github, GitHubClient) else issue_data.get("parent_issue_number")
                    parent = native_parent if isinstance(native_parent, int) else None
                    # Minimal adapters and legacy synthetic fixtures do not
                    # expose the database ID needed by GitHub's mutation API.
                    # Production snapshots always do and were reconciled above.
                    if parent is None and not isinstance(issue_data.get("id"), int):
                        parent = parse_parent_issue_number(str(issue_data.get("body") or ""), current_issue_number=number)
                    if parent is not None:
                        metadata_children.setdefault(parent, []).append(number)
                for issue_data in open_issue_snapshots:
                    if issue_data["number"] in blocked_issue_numbers:
                        continue
                    if not self._is_issue_author_allowed(issue_data):
                        continue
                    candidate = Candidate(type="issue", data=issue_data, priority=self._issue_refill_priority(issue_data), issue_number=issue_data["number"])
                    candidate.data["refill_metadata_open_children"] = metadata_children
                    candidates.append(candidate)
                candidates.sort(key=lambda value: (-value.priority, value.data.get("created_at", ""), value.issue_number or 0))
            except Exception as exc:
                logger.warning(f"Authoritative Issue refill enumeration failed for {repo_name}; obligation remains pending: {exc}")
                return False

            retry_required = reconciliation_retry_required
            for candidate in candidates:
                if await asyncio.to_thread(slots.available_normal_slots) == 0:
                    break
                # The common dispatch path repeats strict readiness, contract,
                # hierarchy, ownership, authorization, duplicate and atomic
                # capacity admission checks immediately before implementation.
                if self.is_draining:
                    return True
                result = await self._run_local_critical(
                    f"capacity refill issue #{candidate.issue_number}",
                    partial(self._process_single_candidate, origin="capacity-refill-intake"),
                    repo_name,
                    candidate,
                )
                retry_required = retry_required or result.refill_retry_required
            return not retry_required

    async def _capacity_refill_loop(self, repo_name: str) -> None:
        """Observe shared slot state and service capacity transitions without GitHub polling."""
        slots = self._get_implementation_slots(repo_name)
        previous_count, previous_identity = await asyncio.to_thread(slots.normal_capacity_snapshot)
        refill_pending = False
        while True:
            available_count, identity = await asyncio.to_thread(slots.normal_capacity_snapshot)
            if available_count > 0 and (previous_count == 0 or identity != previous_identity):
                refill_pending = True
                logger.info("Normal implementation capacity became available; requesting fresh Issue refill")
            previous_count, previous_identity = available_count, identity
            if refill_pending and available_count > 0:
                refill_pending = not await self._refill_normal_implementation_slots(repo_name)
                current_count, current_identity = await asyncio.to_thread(slots.normal_capacity_snapshot)
                # Dispatch may run long enough for another process to fill and
                # release a slot between samples. Atomic state replacement is
                # the durable evidence that this transition needs a fresh pass.
                if current_count > 0 and current_identity != previous_identity:
                    refill_pending = True
                previous_count, previous_identity = current_count, current_identity
            delay = REFILL_RETRY_INTERVAL_SECONDS if refill_pending else CAPACITY_STATE_CHECK_INTERVAL_SECONDS
            await asyncio.sleep(delay)

    async def _worker_loop(self, repo_name: str, worker_id: int) -> None:
        """Worker loop that processes candidates from the queue."""
        with active_repo_context(repo_name):
            logger.info(f"Worker {worker_id} started")
            get_trace_logger().log("System", f"Worker {worker_id} started")

            while True:
                heartbeat(f"worker-{worker_id}:idle", f"queue={self.queue.qsize()}")
                candidate = await self.queue.get()
                if self.is_draining:
                    # Durable queued ownership remains recoverable by recover()
                    # on the next run; shutdown never executes it merely to
                    # empty this process's in-memory queue.
                    self.queue.task_done()
                    return
                item_number = candidate.data.get("number", "N/A")
                decision_completed = False
                deferral_committed = False
                stop_after_persistence_failure = False
                invalidation_claim: Optional[ClaimedInvalidation] = None

                try:
                    if candidate.invalidation_generation is not None:
                        invalidation_claim = ClaimedInvalidation(
                            EntityIdentity(repo_name, candidate.type, int(item_number)),
                            candidate.invalidation_generation,
                            candidate.urgent_admission,
                        )
                        if not await asyncio.to_thread(self.invalidations.begin_processing, invalidation_claim):
                            continue
                        if candidate.type == "dependency":
                            await self._expand_dependency_obligation(repo_name)
                            decision_completed = True
                            continue
                        authoritative_candidate = await asyncio.to_thread(self._create_candidate_from_single, repo_name, candidate.type, int(item_number), True)
                        if authoritative_candidate is None:
                            # A successful authoritative read can decide that an
                            # absent or ineligible entity needs no processing.
                            decision_completed = True
                            continue
                        authoritative_candidate.invalidation_generation = candidate.invalidation_generation
                        authoritative_candidate.urgent_admission = candidate.urgent_admission
                        candidate = authoritative_candidate

                        if candidate.type == "issue":
                            await self._run_local_critical(
                                f"worker {worker_id} submitted-parent validation for issue #{item_number}",
                                self._validate_submitted_parent_generation_for_child,
                                repo_name,
                                int(item_number),
                                candidate.data,
                            )
                            if self.is_draining:
                                return

                    self.active_workers[worker_id] = candidate
                    logger.info(f"Worker {worker_id} processing {candidate.type} #{item_number}")
                    heartbeat(f"worker-{worker_id}:processing", f"{candidate.type} #{item_number}")

                    # An invalidation candidate was fetched strictly above. Do
                    # not overwrite that authority with the cached closed-state
                    # helper: a reopened entity may still have a cached closed
                    # representation. Direct/operator candidates retain their
                    # existing explicit closed-state check.
                    is_closed = candidate.data.get("state") == "closed" if invalidation_claim is not None else is_item_closed_on_github(repo_name, candidate.type, item_number, self.github)
                    if is_closed:
                        logger.info(f"Worker {worker_id} skipping closed {candidate.type} #{item_number}")
                        if candidate.type == "pr":
                            self.notify_pr_merged_or_closed()
                        decision_completed = True
                        continue

                    get_trace_logger().log("Worker", f"Worker {worker_id} started processing {candidate.type} #{item_number}", item_type=candidate.type, item_number=item_number, details={"worker_id": worker_id})

                    # Process candidate
                    result = await self._run_local_critical(
                        f"worker {worker_id} {candidate.type} #{item_number}",
                        partial(self._process_single_candidate, origin="durable-invalidation-worker"),
                        repo_name,
                        candidate,
                    )
                    decision_completed = not bool(result.error) and not (candidate.urgent_admission and result.capacity_deferred)

                    if result.error:
                        logger.error(f"Worker {worker_id} failed to process {candidate.type} #{item_number}: {result.error}")
                        get_trace_logger().log("Worker", f"Worker {worker_id} failed to process {candidate.type} #{item_number}", item_type=candidate.type, item_number=item_number, details={"worker_id": worker_id, "error": result.error})
                    else:
                        logger.info(f"Worker {worker_id} successfully processed {candidate.type} #{item_number}")
                        get_trace_logger().log("Worker", f"Worker {worker_id} successfully processed {candidate.type} #{item_number}", item_type=candidate.type, item_number=item_number, details={"worker_id": worker_id})

                    # Check if PR was merged or closed during candidate processing
                    if self._check_if_pr_merged_or_closed(candidate, result):
                        self.notify_pr_merged_or_closed()

                except GitHubRequestDeferred as deferred:
                    if invalidation_claim is None:
                        raise
                    context = deferred.outcome.context
                    try:
                        retained = await asyncio.to_thread(
                            self.invalidations.defer,
                            invalidation_claim,
                            deferred.reason,
                            deferred.retry_at,
                            context.api_origin,
                        )
                    except Exception as persistence_error:
                        stop_after_persistence_failure = True
                        logger.opt(exception=True).error("Failed to persist authoritative-refresh deferral " f"repository={repo_name} entity={candidate.type}#{item_number} " f"reason={deferred.reason} api_origin={context.api_origin}: {persistence_error}")
                        get_health_monitor().record_event(
                            "worker_error",
                            f"worker {worker_id}: deferral persistence failed: {type(persistence_error).__name__}",
                            f"{candidate.type} #{item_number}",
                        )
                    else:
                        deferral_committed = True
                        log = logger.error if deferred.reason == "governor_state_unavailable" else logger.warning
                        log("Authoritative refresh safely deferred " f"repository={repo_name} entity={candidate.type}#{item_number} " f"reason={retained.reason} retry_at={retained.retry_not_before} " f"api_origin={retained.api_origin or 'unknown'}")
                        if self._invalidation_wake_event is not None:
                            self._invalidation_wake_event.set()
                except asyncio.CancelledError:
                    logger.info(f"Worker {worker_id} cancelled")
                    get_health_monitor().record_event("worker_exit", f"worker {worker_id} cancelled", f"{candidate.type} #{item_number}")
                    raise
                except Exception as e:
                    logger.opt(exception=True).error(f"Worker {worker_id} error processing candidate: {e}")
                    get_health_monitor().record_event("worker_error", f"worker {worker_id}: {type(e).__name__}: {e}", f"{candidate.type} #{item_number}")
                finally:
                    if invalidation_claim is not None:
                        if decision_completed:
                            self.invalidations.complete(invalidation_claim)
                        elif not deferral_committed and not stop_after_persistence_failure:
                            self.invalidations.release(invalidation_claim)
                            # Retry transient authoritative-fetch/processing
                            # failures independently of the maintenance loop,
                            # but retain the former backoff against hot loops.
                            if self._invalidation_wake_event is not None:
                                asyncio.get_running_loop().call_later(60, self._invalidation_wake_event.set)
                    self.active_workers[worker_id] = None
                    self.queue.task_done()
                    if decision_completed:
                        await self._enqueue_pending_invalidations(repo_name)
                if self.is_draining:
                    logger.info(f"Worker {worker_id} reached its graceful drain checkpoint")
                    return
                if stop_after_persistence_failure:
                    logger.error(f"Worker {worker_id} stopped after deferral persistence failure")
                    return

    async def _expand_dependency_obligation(self, repo_name: str) -> None:
        """Discover affected Issues from a complete, current open-Issue scan.

        Discovery deliberately precedes candidate filtering and does not rely
        on native reverse edges.  Each discovered identity becomes its own
        durable generation before the scoped obligation is acknowledged.
        """
        entities = await asyncio.to_thread(self.github.get_open_entities_strict, repo_name)
        issues = getattr(entities, "issues", None)
        if not isinstance(issues, list):
            raise RuntimeError("authoritative dependency discovery returned malformed Issues")
        for issue in issues:
            number = getattr(issue, "number", None)
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise RuntimeError("authoritative dependency discovery returned an invalid Issue")
            deadline = issue_stabilization_deadline(issue.created_at) if issue.created_at is not None else None
            await self.invalidate_entity(repo_name, "issue", number, not_before=deadline)

    async def invalidate_entity(
        self,
        repo_name: str,
        entity_type: str,
        number: int,
        delivery_id: Optional[str] = None,
        event_type: Optional[str] = None,
        action: Optional[str] = None,
        not_before: Optional[float] = None,
        urgent_admission: bool = False,
    ) -> bool:
        """Durably mark an entity dirty and arrange an authoritative reevaluation."""
        accepted = await asyncio.to_thread(
            self.invalidations.invalidate,
            EntityIdentity(repo_name, entity_type, number),
            delivery_id,
            event_type,
            action,
            not_before,
            urgent_admission,
        )
        if accepted and not self.is_draining:
            await self._enqueue_pending_invalidations(repo_name)
            if self._invalidation_wake_event is not None:
                self._invalidation_wake_event.set()
        return accepted

    async def _enqueue_pending_invalidations(self, repo_name: str) -> None:
        """Claim durable identities, fetch authoritative state, and queue decisions."""
        if self.is_draining:
            return
        async with self._invalidation_drain_lock:
            while not self.is_draining and (claim := await asyncio.to_thread(self.invalidations.claim, repo_name)):
                # Only stable identity crosses the durable-to-memory boundary.
                # Authoritative state is fetched after a worker marks this claim
                # processing, so notifications received before that point coalesce.
                candidate = Candidate(
                    type=claim.identity.entity_type,
                    data={"number": claim.identity.number},
                    priority=1 if claim.identity.entity_type == "pr" else 0,
                    issue_number=claim.identity.number if claim.identity.entity_type == "issue" else None,
                    invalidation_generation=claim.generation,
                    urgent_admission=claim.urgent_admission,
                )
                await self.queue.put(candidate)

    def get_status(self) -> Dict[str, Any]:
        """Get the current status of the automation engine."""
        queue_items = list(self.queue._queue) if hasattr(self.queue, "_queue") else []

        # Helper to check if item is in queue or processing
        processing_map = {}  # (type, number) -> worker_id
        for wid, c in self.active_workers.items():
            if c:
                processing_map[(c.type, c.data.get("number"))] = wid

        queued_map = {}  # (type, number) -> priority
        for c in queue_items:
            queued_map[(c.type, c.data.get("number"))] = c.priority

        open_items_status = []

        # Process PRs
        for pr in self.open_prs_snapshot:
            number = pr.get("number")
            status_str = "Open"
            worker_id = processing_map.get(("pr", number))
            if worker_id is not None:
                status_str = f"Processing (Worker {worker_id})"
            elif ("pr", number) in queued_map:
                status_str = f"Queued (Priority {queued_map[('pr', number)]})"

            open_items_status.append(
                {
                    "type": "pr",
                    "number": number,
                    "title": pr.get("title"),
                    "status": status_str,
                    "created_at": pr.get("created_at"),
                }
            )

        # Process Issues
        for issue in self.open_issues_snapshot:
            number = issue.get("number")
            status_str = "Open"
            worker_id = processing_map.get(("issue", number))
            if worker_id is not None:
                status_str = f"Processing (Worker {worker_id})"
            elif ("issue", number) in queued_map:
                status_str = f"Queued (Priority {queued_map[('issue', number)]})"

            open_items_status.append(
                {
                    "type": "issue",
                    "number": number,
                    "title": issue.get("title"),
                    "status": status_str,
                    "created_at": issue.get("created_at"),
                }
            )

        status = {
            "lifecycle": self.lifecycle.value,
            "local_critical_operations": list(self._critical_operations.values()),
            "startup_reconciliation": {
                "complete": self.startup_reconciled,
                "error": self.startup_reconciliation_error,
            },
            "queue_length": self.queue.qsize(),
            "queue_items": [
                {
                    "type": c.type,
                    "number": c.data.get("number"),
                    "priority": c.priority,
                    "title": c.data.get("title"),
                }
                for c in queue_items
            ],
            "active_workers": {
                wid: (
                    {
                        "type": c.type,
                        "number": c.data.get("number"),
                        "title": c.data.get("title"),
                    }
                    if c
                    else None
                )
                for wid, c in self.active_workers.items()
            },
            "open_items": open_items_status,
            "pending_work": self.pending_work_scheduler.snapshot(),
            "merge_operations": self.merge_operation_scheduler.snapshot(),
        }
        return status

    def _check_and_handle_closed_branch(self, repo_name: str) -> bool:
        """
        Check if the current branch corresponds to a closed PR or Issue and handle it.

        This method:
        1. Identifies the current branch
        2. Determines if it corresponds to a PR or Issue (by extracting number from branch name)
        3. Checks if that PR/Issue is closed on GitHub
        4. If closed, checkout main and call check_and_handle_closed_state

        Args:
            repo_name: Repository name in format 'owner/repo'

        Returns:
            True if processing should continue (item is not closed), False otherwise (will exit)
        """
        try:
            # Get current branch name
            current_branch = get_current_branch()
            if not current_branch:
                logger.debug("Could not get current branch, skipping closed branch check")
                return True

            logger.debug(f"Current branch: {current_branch}")

            # Extract issue/PR number from branch name
            # Branch naming convention: issue-<number> (not pr-<number>)
            item_number = extract_number_from_branch(current_branch)
            if item_number is None:
                logger.debug(f"Branch '{current_branch}' does not match issue/PR pattern, skipping closed branch check")
                return True

            # Determine item type based on branch name pattern
            # According to AGENTS.md, only 'issue-<number>' pattern is used
            # (pr-<number> pattern is prohibited)
            item_type = "issue"
            if "issue-" not in current_branch.lower():
                # If somehow we have a pr-<number> branch (shouldn't happen per AGENTS.md)
                # treat it as a PR
                item_type = "pr"

            logger.info(f"Found {item_type} #{item_number} in branch '{current_branch}', checking if closed...")

            # Get current item state from GitHub
            if item_type == "issue":
                # Use GitHubClient directly instead of repo object
                issue = self.github.get_issue(repo_name, item_number)
                current_item = self.github.get_issue_details(issue)
            else:
                # Use GitHubClient directly instead of repo object
                pr = self.github.get_pull_request(repo_name, item_number)
                current_item = self.github.get_pr_details(pr)

            # Check if item is closed
            if current_item.get("state") == "closed":
                logger.info(f"{item_type.capitalize()} #{item_number} is closed, switching to main branch and calling check_and_handle_closed_state")

                # Call check_and_handle_closed_state which will:
                # 1. Switch to main branch
                # 2. Return True if the item was closed and handled
                handled = check_and_handle_closed_state(
                    repo_name,
                    item_type,
                    item_number,
                    self.config,
                    self.github,
                    current_item=current_item,
                )
                # If it was handled (closed), return False to indicate we should stop
                # processing this item/branch and move on (e.g. return to main loop)
                return not handled

            # Item is not closed, continue processing
            logger.debug(f"{item_type.capitalize()} #{item_number} is open, continuing processing")
            return True

        except GitHubRequestDeferred:
            # The durable worker must retain the typed reason and deadline;
            # generic candidate logging would flatten this to "refused".
            raise
        except Exception as e:
            logger.warning(f"Failed to check/handle closed branch state: {e}")
            # Continue processing on error
            return True

    @ci_read_phase_method
    def _get_candidates(self, repo_name: str, max_items: Optional[int] = None) -> List[Candidate]:
        """Collect PR/Issue candidates with priority.

        Priority definitions:
        - 7: Breaking-change PR (breaking-change, breaking, api-change, deprecation, version-major)
        - 4: Urgent + unmergeable PR (highest priority after breaking-change)
        - 3: Urgent + mergeable PR or urgent issue
        - 2: Unmergeable PR needing conflict resolution
        - 1: PR requiring fixes (GH Actions failed but mergeable), or mergeable
             with passing checks but blocked awaiting corrective changes on the
             current HEAD per Auto-Coder's own adversarial review (issue #1731)
        - 0: Regular issues

        Sort order:
        - Priority descending (7 -> 0)
        - Creation time ascending (oldest first)
        """
        from .issue_context import extract_linked_issues_from_pr_body
        from .pr_processor import (
            _close_empty_pr,
            _close_stale_jules_pr,
            _dependency_bot_flag_decision,
            _dependency_bot_readiness_decision,
            _is_dependabot_pr,
            _is_jules_pr,
            _reject_unsafe_codex_cloud_pr,
            is_current_head_adversarial_review_blocked,
        )
        from .util.github_action import (
            _check_github_actions_status,
            check_github_actions_and_exit_if_in_progress,
        )

        candidates: List[Candidate] = []
        # Issues queued from the stale-Jules-PR path, to avoid queueing them twice
        requeued_issue_numbers: set[int] = set()

        try:

            # Preload PR data and GitHub Actions statuses to avoid N+1 API calls
            # Optimized to use get_open_prs_json to batch fetch details
            # This replaces the need for get_open_pull_requests which triggers separate API calls
            pr_data_list = self.github.get_open_prs_json(repo_name)

            # Update snapshot
            self.open_prs_snapshot = pr_data_list

            # Sort by creation date ascending (oldest first) to match processing order expectation
            pr_data_list.sort(key=lambda x: x.get("created_at", ""))

            safe_pr_data_list = []
            for pr_data in pr_data_list:
                pr_number = pr_data.get("number")
                if not isinstance(pr_number, int):
                    logger.warning(f"Skipping PR missing/invalid number in data: {pr_data}")
                    continue

                # Branch identity is a safety invariant, not a normal PR
                # processing decision. Reject unsafe Codex Cloud heads before
                # even preloading CI, or author/label/CI gates can defer them
                # indefinitely.
                unsafe_branch_result = _reject_unsafe_codex_cloud_pr(self.github, repo_name, pr_data, self.config)
                if unsafe_branch_result.closed or unsafe_branch_result.metadata_error:
                    for action in unsafe_branch_result.actions:
                        logger.info(f"PR #{pr_number}: {action}")
                    continue
                safe_pr_data_list.append(unsafe_branch_result.authoritative_pr_data or pr_data)

            pr_data_list = safe_pr_data_list

            # Lazy-load repository object if needed for Jules PRs
            repo = None

            for pr_data in pr_data_list:
                labels = pr_data.get("labels", []) or []

                pr_number = pr_data.get("number")
                if not isinstance(pr_number, int):
                    logger.warning(f"Skipping PR missing/invalid number in data: {pr_data}")
                    continue

                # Check PR author allowlist before any processing
                if not self._is_pr_author_allowed(pr_data):
                    logger.debug(f"Skipping PR #{pr_number} - author not in PR allowlist")
                    continue

                # Check if Jules PR is a draft and mark as ready if so
                # This must be done BEFORE checking GitHub Actions status, as some actions only run on ready PRs
                if _is_jules_pr(pr_data) and pr_data.get("draft"):
                    logger.info(f"Jules PR #{pr_number} is a draft, marking as ready for review")
                    try:
                        token = self.github.token
                        api = get_ghapi_client(token)
                        node_id = pr_data.get("node_id")

                        if not node_id:
                            logger.info(f"Node ID missing for PR #{pr_number}, fetching details...")
                            try:
                                # Fallback: Fetch PR details to get node_id
                                owner, repo = repo_name.split("/")
                                pr_details = api.pulls.get(owner, repo, pr_number)
                                node_id = pr_details.get("node_id")
                                if node_id:
                                    # Update local data
                                    pr_data["node_id"] = node_id
                            except Exception as e:
                                logger.warning(f"Failed to fetch details for PR #{pr_number}: {e}")

                        if node_id:
                            end_ci_read_phase("mark-pr-ready")
                            # GraphQL mutation to mark as ready
                            mutation = """
                            mutation($id: ID!) {
                              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                                pullRequest {
                                  isDraft
                                }
                              }
                            }
                            """
                            self.github.graphql_query(query=mutation, variables={"id": node_id})
                            logger.info(f"Successfully marked Jules PR #{pr_number} as ready for review (via GraphQL)")
                            # Update local data
                            pr_data["draft"] = False
                        else:
                            logger.warning(f"Could not mark Jules PR #{pr_number} as ready: missing node_id after fetch attempt")
                    except Exception as e:
                        logger.error(f"Failed to mark Jules PR #{pr_number} as ready: {e}")

                # Close PRs that have zero effective diff.
                # Runs before label and stale checks so empty PRs are cleaned up and retried immediately.
                end_ci_read_phase("empty-pr-policy")
                empty_pr_result = _close_empty_pr(self.github, repo_name, pr_data, self.config)
                if empty_pr_result.closed:
                    for action in empty_pr_result.actions:
                        logger.info(f"PR #{pr_number}: {action}")
                    # Queue the unlocked issue(s) right away so the new attempt starts in
                    # this cycle instead of waiting for the next poll.
                    for issue_number in empty_pr_result.issue_numbers:
                        issue_candidate = self._create_candidate_from_single(repo_name, "issue", issue_number)
                        if issue_candidate:
                            issue_candidate.priority = 3
                            candidates.append(issue_candidate)
                            requeued_issue_numbers.add(issue_number)
                            logger.info(f"Queued issue #{issue_number} for a new attempt after closing empty PR #{pr_number}")
                    continue

                # Close Jules PRs that could not get CI green within the configured timeout.
                # This runs before the label and "waiting for Jules" skips below, because a
                # stale Jules PR normally still carries the @auto-coder label from an earlier
                # run and would otherwise never be looked at again.
                end_ci_read_phase("stale-jules-policy")
                stale_jules_result = _close_stale_jules_pr(self.github, repo_name, pr_data, self.config)
                if stale_jules_result.closed:
                    for action in stale_jules_result.actions:
                        logger.info(f"PR #{pr_number}: {action}")
                    # Queue the unlocked issue(s) right away so the new attempt starts in
                    # this cycle instead of waiting for the next poll.
                    for issue_number in stale_jules_result.issue_numbers:
                        issue_candidate = self._create_candidate_from_single(repo_name, "issue", issue_number)
                        if issue_candidate:
                            issue_candidate.priority = 3
                            candidates.append(issue_candidate)
                            requeued_issue_numbers.add(issue_number)
                            logger.info(f"Queued issue #{issue_number} for a new attempt after closing stale Jules PR #{pr_number}")
                    continue

                # Skip if another instance is processing (@auto-coder label present) using LabelManager check
                with LabelManager(
                    self.github,
                    repo_name,
                    pr_number,
                    item_type="pr",
                    skip_label_add=True,
                    known_labels=pr_data.get("labels"),
                ) as should_process:
                    if not should_process:
                        continue

                # Calculate GitHub Actions status for the PR
                # check_github_actions_and_exit_if_in_progress returns True if we should continue (not in progress)
                # and False if we should stop/skip (in progress)
                should_continue = check_github_actions_and_exit_if_in_progress(
                    repo_name,
                    pr_data,
                    self.config,
                    self.github,
                    switch_branch_on_in_progress=False,
                    item_type="pr",
                )

                if not should_continue:
                    logger.debug(f"Skipping PR #{pr_number} - CI checks are in progress")
                    continue

                # We still need the checks object for priority calculation later
                # Since check_github_actions_and_exit_if_in_progress doesn't return it, we call _check_github_actions_status again
                # or we could refactor, but for now let's just call it to get the object as it's cached
                checks = _check_github_actions_status(repo_name, pr_data, self.config)

                # Check if we should skip this PR because it's waiting for Jules
                if _should_skip_waiting_for_jules(self.github, repo_name, pr_data):
                    logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
                    continue

                mergeable = pr_data.get("mergeable", True)

                # Handle dependency-bot PRs based on configuration, using the
                # same flag/readiness decisions as the mandatory common
                # admission gate in `_process_single_candidate_unified_impl`
                # (Issue #1995, REQ-008). This is an optional, non-authoritative
                # prefilter over facts already collected in this loop (this
                # PR is already known-open from `get_open_prs_json`, and
                # `checks` was already computed above): it can never
                # substitute for that mandatory boundary's own fresh,
                # same-HEAD confirmation (REQ-005).
                dependency_bot_flag_decision = _dependency_bot_flag_decision(_is_dependabot_pr(pr_data), self.config.IGNORE_DEPENDABOT_PRS, self.config.AUTO_MERGE_DEPENDABOT_PRS)
                if dependency_bot_flag_decision is None:
                    dependency_bot_decision = _dependency_bot_readiness_decision(
                        is_open=True,
                        mergeable=mergeable,
                        ci_error=checks.error if (not checks.success and not checks.in_progress) else None,
                        ci_pending=checks.in_progress,
                        ci_success=checks.success,
                    )
                else:
                    dependency_bot_decision = dependency_bot_flag_decision
                if not dependency_bot_decision.allowed:
                    logger.debug(f"Skipping dependency-bot PR #{pr_number} at collector prefilter: {dependency_bot_decision.reason}")
                    continue

                # Check if PR is created by Jules and waiting for Jules update
                if pr_data.get("author") == "jules":
                    try:
                        # Fetch PR reviews and comments to check for interaction
                        last_interaction_time = None
                        last_interaction_type = None

                        # Check reviews
                        reviews = self.github.get_pr_reviews(repo_name, pr_number)
                        for review in reviews:
                            user = review.get("user")
                            if user and user.get("login") != "jules":
                                submitted_at = review.get("submitted_at")
                                if submitted_at:
                                    dt = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
                                    if last_interaction_time is None or dt > last_interaction_time:
                                        last_interaction_time = dt
                                        last_interaction_type = review.get("state")

                        # Check comments
                        comments = self.github.get_pr_comments(repo_name, pr_number)
                        from .util.github_action import is_verified_prompt_regression_advisory_comment

                        pr_head_sha = str(pr_data.get("head", {}).get("sha") or "")
                        for comment in comments:
                            if is_verified_prompt_regression_advisory_comment(repo_name, pr_number, pr_head_sha, comment):
                                continue
                            user = comment.get("user")
                            if user and user.get("login") != "jules":
                                created_at = comment.get("created_at")
                                if created_at:
                                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                                    if last_interaction_time is None or dt > last_interaction_time:
                                        last_interaction_time = dt
                                        last_interaction_type = "COMMENT"

                        if last_interaction_time and last_interaction_type != "APPROVED":
                            # Check for Jules commits after interaction
                            commits = self.github.get_pr_commits(repo_name, pr_number)
                            jules_responded = False
                            if commits:
                                for commit_data in reversed(commits):
                                    # Check commit date
                                    committer = commit_data.get("commit", {}).get("committer", {})
                                    commit_date_str = committer.get("date")
                                    if commit_date_str:
                                        commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
                                        if commit_date > last_interaction_time:
                                            author = commit_data.get("author")
                                            if author and author.get("login") == "jules":
                                                jules_responded = True
                                                break
                                        else:
                                            # Commits are ordered, so if we hit an older one, we can stop
                                            break

                            if not jules_responded:
                                logger.info(f"Skipping PR #{pr_number} - Waiting for Jules to update (requested at {last_interaction_time})")
                                continue

                    except Exception as e:
                        logger.warning(f"Failed to check Jules PR status for #{pr_number}: {e}")

                # Calculate priority
                # Enhanced priority logic to distinguish unmergeable PRs
                if any(
                    label in labels
                    for label in [
                        "breaking-change",
                        "breaking",
                        "api-change",
                        "deprecation",
                        "version-major",
                    ]
                ):
                    # Breaking-change PRs get highest priority (7)
                    pr_priority = 7
                elif "urgent" in labels:
                    # Urgent items get high priority
                    if not mergeable:
                        pr_priority = 4  # Urgent + unmergeable (highest urgent priority)
                    else:
                        pr_priority = 3  # Urgent + mergeable
                elif not mergeable:
                    pr_priority = 2  # Unmergeable PRs (elevated from priority 1)
                elif not checks.success:
                    pr_priority = 1  # Fix-required but mergeable PRs
                elif self._is_pr_adversarial_validation_enabled(repo_name, self.config) and is_current_head_adversarial_review_blocked(self.github, repo_name, pr_data, self.config):
                    # Auto-Coder's own adversarial review already found material
                    # violations at this exact HEAD and has no further normal
                    # action until the PR author/backend supplies a new commit
                    # (issue #1731). Deprioritize like any other fix-required PR
                    # instead of competing at the auto-merge-candidate priority.
                    pr_priority = 1
                else:
                    pr_priority = 2  # Mergeable with successful checks (auto-merge candidate)

                candidates.append(
                    Candidate(
                        type="pr",
                        data=pr_data,
                        priority=pr_priority,
                        branch_name=pr_data.get("head", {}).get("ref"),
                        related_issues=extract_linked_issues_from_pr_body(pr_data.get("body", "")),
                    )
                )

            # Discovery is independent of implementation concurrency and result
            # limits. Always scan ordinary Issues here; priority sorting and max_items
            # are applied afterward, while durable slots authorize implementation.
            all_issues = self.github.get_open_issues_json(repo_name)

            # Update snapshot
            self.open_issues_snapshot = all_issues

            # Build map for fast lookup of open issues
            issue_map = {i["number"]: i for i in all_issues}

            # Numbers of the PRs that are currently open, used to tell a live PR apart
            # from a closed one still listed in an issue timeline
            open_pr_numbers = {pr.get("number") for pr in pr_data_list if isinstance(pr.get("number"), int)}

            for issue_data in all_issues:
                number = issue_data.get("number")
                if not isinstance(number, int):
                    logger.warning(f"Issue data missing or invalid number: {issue_data}")
                    continue

                # Check Issue author allowlist before any processing
                if not self._is_issue_author_allowed(issue_data):
                    logger.debug(f"Skipping Issue #{number} - author not in Issue allowlist")
                    continue

                labels = issue_data.get("labels", []) or []

                # Filter out issues created within the last 10 minutes
                created_at_str = issue_data.get("created_at")
                if created_at_str:
                    # Parse the timestamp string
                    # Example: "2024-07-15T12:34:56Z"
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))

                    # Ensure it's timezone-aware (UTC)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)

                    # Get current time in UTC
                    now_utc = datetime.now(timezone.utc)

                    # If created within the last 5 minutes, skip
                    if now_utc - created_at < timedelta(minutes=5):
                        logger.debug(f"Skipping issue #{issue_data.get('number')} - created less than 5 minutes ago")
                        continue

                # Dependency declarations are production intake, not candidate
                # decoration.  Observe them even without a child readiness label.
                blocked_by = parse_blocked_by_declaration(issue_data.get("body"), parse_parent_declaration(issue_data.get("body")).status)
                if isinstance(self.github, GitHubClient) and hasattr(self.github, "token") and blocked_by.status is not BlockedByDeclarationStatus.ABSENT:
                    try:
                        dependency_snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                        dependency_result = self._reconcile_sibling_dependencies(repo_name, number, dependency_snapshot)
                    except Exception as exc:
                        logger.warning(f"Deferring Issue #{number}; sibling dependency reconciliation is unresolved: {exc}")
                        continue
                    if dependency_result is DependencySatisfaction.INVALID:
                        continue

                # Skip if has sub-issues or linked PR
                # Already queued by the stale-Jules-PR path above
                if number in requeued_issue_numbers:
                    continue

                # Skip if another instance is processing (@auto-coder label present) using LabelManager check
                with LabelManager(
                    self.github,
                    repo_name,
                    number,
                    item_type="issue",
                    skip_label_add=True,
                    known_labels=labels,
                ) as should_process:
                    if not should_process:
                        continue

                # Skip if issue has open sub-issues (it should be processed after sub-issues are resolved)
                # Use pre-fetched data
                if issue_data.get("has_open_sub_issues"):
                    continue

                # Skip only while an *open* PR covers the issue; the work happens on that
                # PR. Closed or merged PRs stay in the issue timeline forever, so counting
                # them here would permanently hide any issue that once had a PR - including
                # issues whose stale Jules PR was just closed for a new attempt.
                linked_pr_numbers = set(issue_data.get("linked_pr_numbers") or [])
                open_linked_prs = linked_pr_numbers & open_pr_numbers
                if open_linked_prs:
                    logger.debug(f"Skipping issue #{number} - open PR(s) {sorted(open_linked_prs)} already cover it")
                    continue

                # Calculate priority
                # Priority levels:
                # - 7: Breaking-change (breaking-change, breaking, api-change, deprecation, version-major)
                # - 3: Urgent
                # - 0: Regular issues
                issue_priority = self._issue_refill_priority(issue_data)

                candidates.append(
                    Candidate(
                        type="issue",
                        data=issue_data,
                        priority=issue_priority,
                        issue_number=number,
                    )
                )

            # Sort by priority descending, type (issue first), creation time ascending
            def _type_order(t: str) -> int:
                return 0 if t == "issue" else 1

            candidates.sort(
                key=lambda x: (
                    -x.priority,
                    _type_order(x.type),
                    x.data.get("created_at", ""),
                )
            )

            # Trim if max items specified
            if isinstance(max_items, int) and max_items > 0:
                candidates = candidates[:max_items]

            return candidates
        finally:
            # Clear the sub-issue cache when candidate acquisition is finished
            self.github.clear_sub_issue_cache()

    def _is_issue_author_allowed(self, issue_data: Optional[Dict[str, Any]]) -> bool:
        """Check if the author of the issue is present in the issue allowlist."""
        from .automation_config import get_author_id, is_author_allowlisted

        allowlist = getattr(self.config, "ISSUE_ALLOWLIST", None)
        if allowlist is None:
            allowlist = getattr(self.config, "issue_allowlist", None)
        author_id = get_author_id(issue_data)
        return is_author_allowlisted(author_id, allowlist)

    def _is_pr_author_allowed(self, pr_data: Optional[Dict[str, Any]]) -> bool:
        """Check if the author of the PR is present in the PR allowlist."""
        from .automation_config import get_author_id, is_author_allowlisted

        allowlist = getattr(self.config, "PR_ALLOWLIST", None)
        if allowlist is None:
            allowlist = getattr(self.config, "pr_allowlist", None)
        author_id = get_author_id(pr_data)
        return is_author_allowlisted(author_id, allowlist)

    def _has_open_sub_issues(self, repo_name: str, candidate: Candidate) -> bool:
        """Check if target issue has unresolved sub-issues.
        - candidate is expected to be an element from _get_candidates (type: issue)
        Lookup failures propagate so dispatch fails closed.
        """
        try:
            if candidate.type != "issue":
                return False
            issue_data = candidate.data or {}
            issue_number = candidate.issue_number or issue_data.get("number")
            if not issue_number:
                return False
            if issue_data.get("has_open_sub_issues"):
                return True
            metadata_children = issue_data.get("refill_metadata_open_children", {})
            if isinstance(metadata_children, dict) and metadata_children.get(issue_number):
                return True
            if isinstance(self.github, GitHubClient):
                sub_issues = self.github.get_open_sub_issues_strict(repo_name, issue_number)
            else:
                lookup = getattr(self.github, "get_open_sub_issues", None)
                sub_issues = lookup(repo_name, issue_number) if callable(lookup) else []
            if isinstance(sub_issues, list) and sub_issues:
                return True
            if self.open_issues_snapshot:
                fallback_children = [other["number"] for other in self.open_issues_snapshot if isinstance(other.get("number"), int) and other.get("parent_issue_number") == issue_number and other.get("number") != issue_number]
                if fallback_children:
                    return True
            return False
        except Exception as e:
            logger.warning(f"Failed to check open sub-issues for issue #{candidate.issue_number or issue_data.get('number', 'N/A')}: {e}")
            raise

    def _process_single_candidate_unified(
        self,
        repo_name: str,
        candidate: Candidate,
        config: AutomationConfig,
        jules_mode: bool = False,
        explicit_only: bool = False,
        force: bool = False,
        continue_execution: bool = False,
        advance_issue_attempt: bool = False,
        generation_serialized: bool = False,
        authoritative_parent_number: Optional[int] = None,
        origin: str = "worker",
    ) -> CandidateProcessingResult:
        """Open (or continue) an execution-scoped trace, then dispatch to the real implementation.

        A fresh top-level execution is opened whenever this call is not
        already nested inside a matching (repository, item_type, item_number)
        scope -- covering every origin listed in REQ-001 as well as a
        recursive dispatch into a *different* Issue (e.g. a container
        parent's open child, REQ-002: separate items always have separate
        execution scopes). A call that is already inside a matching scope
        (e.g. the ``generation_serialized`` re-entry for the same owner, or
        a validation-triggered continuation) is nested work that keeps
        participating in the caller's execution instead (REQ-002).
        """
        item_number = candidate.data.get("number")
        ambient = current_scope()
        already_scoped = ambient is not None and ambient.repository == repo_name and ambient.item_type == candidate.type and ambient.item_number == item_number
        if not isinstance(item_number, int) or isinstance(item_number, bool) or already_scoped:
            return self._process_single_candidate_unified_impl(
                repo_name,
                candidate,
                config,
                jules_mode,
                explicit_only,
                force,
                continue_execution,
                advance_issue_attempt,
                generation_serialized,
                authoritative_parent_number,
                origin,
            )
        impl_args = (
            repo_name,
            candidate,
            config,
            jules_mode,
            explicit_only,
            force,
            continue_execution,
            advance_issue_attempt,
            generation_serialized,
            authoritative_parent_number,
            origin,
        )
        try:
            handle_cm = get_trace_collector().start_execution(
                repository=repo_name,
                item_type=candidate.type,
                item_number=item_number,
                origin=origin,
                stage_id=f"{candidate.type}.execution",
                label=f"{candidate.type}#{item_number} execution",
            )
        except Exception:
            logger.opt(exception=True).debug("Diagnostic trace recording failed opening execution scope for {}#{}; continuing untraced", candidate.type, item_number)
            return self._process_single_candidate_unified_impl(*impl_args)
        with handle_cm as handle:
            result = self._process_single_candidate_unified_impl(*impl_args)
            try:
                handle.set_outcome(_map_candidate_result_outcome(result))
            except Exception:
                logger.opt(exception=True).debug("Diagnostic trace recording failed finishing execution scope for {}#{}; continuing", candidate.type, item_number)
            return result

    def _process_single_candidate_unified_impl(
        self,
        repo_name: str,
        candidate: Candidate,
        config: AutomationConfig,
        jules_mode: bool = False,
        explicit_only: bool = False,
        force: bool = False,
        continue_execution: bool = False,
        advance_issue_attempt: bool = False,
        generation_serialized: bool = False,
        authoritative_parent_number: Optional[int] = None,
        origin: str = "worker",
    ) -> CandidateProcessingResult:
        """Unified function for processing single issue or PR candidate.

        Handles all common logic: LabelManager, branch_context, error handling.
        This consolidates the logic from both batch processing (_process_single_candidate)
        and single processing (process_single).

        Args:
            repo_name: Repository name
            candidate: Target candidate to process
            config: AutomationConfig instance
            jules_mode: Whether to use Jules mode for processing (default: False)
            continue_execution: Whether this is an internal lifecycle transition that
                must continue the caller's execution for the same logical owner.
            origin: Diagnostic label propagated to a recursive dispatch into a
                different Issue (e.g. a container parent's open child); does
                not affect processing behavior.

        Returns:
            Processing result
        """
        from .label_manager import LabelManager

        result = CandidateProcessingResult(
            type=candidate.type,
            number=candidate.data.get("number"),
            title=candidate.data.get("title"),
            success=False,
            actions=[],
            error=None,
        )

        # Reject candidates that cannot enter an implementation lifecycle before
        # persisting ownership.  Otherwise an open but ineligible item can leak a
        # slot indefinitely because reconciliation correctly sees it as nonterminal.
        item_number = candidate.data.get("number")
        if not isinstance(item_number, int) or isinstance(item_number, bool):
            result.error = f"Item number is missing for {candidate.type} #{candidate.data.get('number', 'N/A')}"
            return result
        if candidate.type == "pr" and not self._is_pr_author_allowed(candidate.data):
            logger.info(f"Skipping PR #{item_number} - author not in PR allowlist")
            result.target_outcome = ExplicitTargetOutcome.SKIPPED
            result.target_reason = "PR author is not in the allowlist"
            _record_pr_stage_result(item_number, "pr.author-admission", f"pr#{item_number} author admission", Outcome.SKIPPED, {"reason": result.target_reason})
            return result
        if candidate.type == "pr":
            # Common dependency-bot processing-policy gate (Issue #1995):
            # every PR-processing origin funnels through this impl before any
            # implementation reservation, execution, or admission-related PR
            # membership is created, so a startup/webhook-discovered candidate
            # cannot bypass the policy that `_get_candidates()` also applies.
            from .pr_processor import evaluate_dependency_bot_admission

            dependency_bot_decision = evaluate_dependency_bot_admission(self.github, repo_name, candidate.data, config)
            if not dependency_bot_decision.allowed:
                logger.info(f"Refusing PR #{item_number} at dependency-bot admission gate: {dependency_bot_decision.reason}")
                result.target_outcome = dependency_bot_decision.outcome
                result.target_reason = dependency_bot_decision.reason
                _record_pr_stage_result(
                    item_number,
                    "pr.dependency-bot-admission",
                    f"pr#{item_number} dependency-bot admission",
                    Outcome(dependency_bot_decision.outcome.value) if dependency_bot_decision.outcome else Outcome.SKIPPED,
                    {"reason": dependency_bot_decision.reason},
                )
                if dependency_bot_decision.outcome is ExplicitTargetOutcome.DEFERRED:
                    result.refill_retry_required = True
                return result
        if candidate.type == "issue":
            collected_candidate = candidate
            if not self._is_issue_author_allowed(candidate.data):
                logger.info(f"Skipping Issue #{item_number} - author not in Issue allowlist")
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.target_reason = "Issue author is not in the allowlist"
                _record_issue_stage_result(item_number, "issue.author-admission", f"issue#{item_number} author admission", Outcome.SKIPPED, {"reason": result.target_reason})
                return result
            if isinstance(self.github, GitHubClient):
                try:
                    observed = self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number)
                    if not isinstance(observed, dict) or observed.get("number") != item_number or "pull_request" in observed:
                        result.error = f"Refusing Issue dispatch for {repo_name}#{item_number}: GitHub returned an ambiguous item snapshot"
                        return result
                    refreshed = self._reconcile_parent_issue(repo_name, item_number, observed) if parse_parent_declaration(observed.get("body")).status is not ParentDeclarationStatus.ABSENT else observed
                    refreshed_manifest = build_normative_issue_manifest(item_number, str(refreshed.get("title") or ""), str(refreshed.get("body") or ""))
                    if refreshed_manifest.error is None:
                        collected_candidate.data.update(refreshed)
                    candidate = Candidate(
                        type=candidate.type,
                        data={**candidate.data, **refreshed},
                        priority=candidate.priority,
                        issue_number=candidate.issue_number,
                        branch_name=candidate.branch_name,
                        related_issues=candidate.related_issues,
                        invalidation_generation=candidate.invalidation_generation,
                        urgent_admission=candidate.urgent_admission,
                    )
                except ParentSpecificationError as exc:
                    result.error = f"Parent-Issue reconciliation blocked processing: {exc}"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Blocked - invalid Parent-Issue relationship metadata"]
                    _record_issue_stage_result(item_number, "issue.parent-reconciliation", f"issue#{item_number} parent reconciliation", Outcome.BLOCKED, {"reason": str(exc)})
                    return result
                except ParentOperationalError as exc:
                    result.error = f"Parent-Issue reconciliation is temporarily unavailable: {exc}"
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - Parent-Issue reconciliation requires retry"]
                    result.refill_retry_required = True
                    _record_issue_stage_result(item_number, "issue.parent-reconciliation", f"issue#{item_number} parent reconciliation", Outcome.DEFERRED, {"reason": str(exc)})
                    return result
            # A submitted parent represents its whole direct-child contract, not
            # a standalone coding target. Numeric order is only a deterministic
            # tie-breaker: dependency refusal must not strand another root.
            try:
                direct_child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
                # Candidate hints can be stale and daemon normalization only
                # describes open children. Complete authoritative membership is
                # therefore consulted before any Issue can be treated standalone.
                direct_children = direct_child_reader(repo_name, item_number) if callable(direct_child_reader) else []
            except GitHubRequestError as exc:
                return self._defer_issue_evaluation(repo_name, item_number, candidate.data, exc, result)
            except Exception as exc:
                result.error = f"Cannot determine authoritative direct-child membership: {exc}"
                return result
            if isinstance(direct_children, list) and direct_children:
                try:
                    parent_snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number)
                except GitHubRequestError as exc:
                    return self._defer_issue_evaluation(repo_name, item_number, candidate.data, exc, result)
                except Exception as exc:
                    result.error = f"Cannot confirm parent readiness submission: {exc}"
                    return result
                if not self._is_open_issue(parent_snapshot):
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - parent issue is closed"]
                    return result
                if is_implementation_ready(parent_snapshot):
                    if self._defer_initial_issue_stabilization(repo_name, parent_snapshot):
                        result.target_outcome = ExplicitTargetOutcome.DEFERRED
                        result.actions = ["Deferred - readiness submission is in its initial stabilization window"]
                        return result
                    try:
                        parent_submission_set = self._fetch_authoritative_decomposition_set(repo_name, item_number)
                    except ParentSpecificationError as exc:
                        result.error = f"Parent-Issue reconciliation blocked processing: {exc}"
                        result.target_outcome = ExplicitTargetOutcome.BLOCKED
                        result.actions = ["Blocked - invalid Parent-Issue relationship metadata"]
                        return result
                    except ParentOperationalError as exc:
                        result.error = f"Parent-Issue reconciliation is temporarily unavailable: {exc}"
                        result.target_outcome = ExplicitTargetOutcome.DEFERRED
                        result.actions = ["Deferred - Parent-Issue reconciliation requires retry"]
                        result.refill_retry_required = True
                        return result
                    if parent_submission_set is None:
                        result.error = "Cannot fetch authoritative parent/child specification set"
                        return result
                    # Validation eligibility belongs to the submitted generation,
                    # not to implementation eligibility. Submit the complete set
                    # before closed-child filtering or retained-owner routing.
                    decomposition_job, child_jobs = self._schedule_parent_validations(repo_name, parent_submission_set, config)
                    parent_decision, child_decisions = self._join_parent_validations(decomposition_job, child_jobs)
                    _, authoritative_children = parent_submission_set
                    open_children = sorted(
                        (child for child in authoritative_children if child.get("state") == "open" and isinstance(child.get("number"), int)),
                        key=lambda child: int(child["number"]),
                    )
                    if not open_children:
                        parent_decomposition_validator = self._get_decomposition_validator(repo_name)
                        decomposition_enabled = self._is_issue_decomposition_validation_enabled(repo_name, config)
                        if decomposition_enabled and parent_decision is not None and parent_decision.verdict == "BLOCKED":
                            side_effect_error = parent_decomposition_validator.apply_blocked(
                                self.github,
                                parent_decision,
                                lambda number: self._fetch_authoritative_decomposition_set(repo_name, number),
                            )
                            result.error = "Parent/child decomposition validation found material defects"
                            if side_effect_error:
                                result.error += f"; GitHub side effect failed: {side_effect_error}"
                            result.target_outcome = ExplicitTargetOutcome.BLOCKED
                            result.actions = ["Rejected - blocked parent/child decomposition"]
                            _record_issue_stage_result(item_number, "issue.decomposition-validation", f"issue#{item_number} decomposition validation", Outcome.BLOCKED, {"member_issue_numbers": sorted(child_decisions)})
                        elif decomposition_enabled and parent_decision is not None and parent_decision.verdict == "ERROR":
                            result.error = "Decomposition validation failed; parent readiness was preserved for retry"
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED
                            result.actions = ["Deferred - decomposition validation error"]
                            _record_issue_stage_result(item_number, "issue.decomposition-validation", f"issue#{item_number} decomposition validation", Outcome.FAILED, {"member_issue_numbers": sorted(child_decisions)})
                        elif any(decision.verdict == "ERROR" for decision in child_decisions.values()):
                            result.error = "Individual validation failed; parent readiness was preserved for retry"
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED
                            result.actions = ["Deferred - child specification validation error"]
                            _record_issue_stage_result(item_number, "issue.individual-validation", f"issue#{item_number} individual validation", Outcome.FAILED, {"member_issue_numbers": sorted(child_decisions)})
                        elif any(decision.verdict == "BLOCKED" for decision in child_decisions.values()):
                            blocked = next(decision for decision in child_decisions.values() if decision.verdict == "BLOCKED")
                            validator = self._get_specification_validator(repo_name)

                            def set_is_current() -> bool:
                                latest = self._fetch_authoritative_decomposition_set(repo_name, item_number)
                                return latest is not None and self._is_open_issue(latest[0]) and is_implementation_ready(latest[0]) and parent_decision is not None and parent_decomposition_validator.identity(*latest) == parent_decision.identity

                            side_effect_error = validator.apply_inherited_blocked(self.github, blocked, item_number, set_is_current)
                            result.error = "Child specification validation found material defects"
                            if side_effect_error:
                                result.error += f"; GitHub side effect failed: {side_effect_error}"
                            result.target_outcome = ExplicitTargetOutcome.BLOCKED
                            result.actions = ["Rejected - blocked child specification"]
                            _record_issue_stage_result(item_number, "issue.individual-validation", f"issue#{item_number} individual validation", Outcome.BLOCKED, {"member_issue_numbers": sorted(child_decisions)})
                        elif decomposition_enabled and parent_decision is not None:
                            complete, message = self._complete_container_parent(repo_name, item_number, parent_decision, child_decisions)
                            if complete:
                                result.success = True
                                result.target_outcome = ExplicitTargetOutcome.SUCCESS
                                result.actions = [message]
                            else:
                                result.error = message
                                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                                result.actions = ["Deferred - container parent completion requires retry"]
                                result.refill_retry_required = True
                        else:
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED
                            result.actions = ["Deferred - container parent requires decomposition validation"]
                        return result
                    last_refusal: Optional[CandidateProcessingResult] = None
                    for open_child in open_children:
                        child_number = int(open_child["number"])
                        child = self.github.get_issue_dispatch_snapshot_strict(repo_name, child_number)
                        child["parent_issue_number"] = item_number
                        child_result = self._process_single_candidate_unified(
                            repo_name,
                            Candidate(type="issue", data=child, priority=candidate.priority, issue_number=child_number),
                            config,
                            jules_mode,
                            explicit_only,
                            force,
                            continue_execution,
                            advance_issue_attempt,
                            authoritative_parent_number=item_number,
                            origin=origin,
                        )
                        if child_result.success:
                            return child_result
                        last_refusal = child_result
                        if child_result.target_outcome is ExplicitTargetOutcome.BLOCKED:
                            return child_result
                    if last_refusal is not None:
                        return last_refusal
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = [f"Skipped - parent submission is missing {IMPLEMENTATION_READY_LABEL} label"]
                _record_issue_stage_result(item_number, "issue.hierarchy-admission", f"issue#{item_number} hierarchy admission", Outcome.SKIPPED, {"reason": result.actions[0]})
                return result

            live_parent_number = authoritative_parent_number or self._get_authoritative_parent_number(repo_name, item_number, candidate.data)
            if isinstance(self.github, GitHubClient) and isinstance(live_parent_number, int):
                try:
                    live_parent_set = self._fetch_authoritative_decomposition_set(repo_name, live_parent_number)
                except Exception as exc:
                    result.error = f"Cannot fetch authoritative parent/child specification set: {exc}"
                    return result
                if live_parent_set is None or item_number not in {child.get("number") for child in live_parent_set[1]}:
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - child is no longer in the authoritative parent set"]
                    return result
                if not self._is_open_issue(live_parent_set[0]):
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - authoritative parent is closed"]
                    return result
                if not is_implementation_ready(live_parent_set[0]):
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = [f"Skipped - authoritative parent is missing {IMPLEMENTATION_READY_LABEL} label"]
                    return result
                if self._defer_initial_issue_stabilization(repo_name, live_parent_set[0]):
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - readiness submission is in its initial stabilization window"]
                    return result
            elif is_implementation_ready(candidate.data) and self._defer_initial_issue_stabilization(repo_name, candidate.data):
                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                result.actions = ["Deferred - readiness submission is in its initial stabilization window"]
                return result
            if not generation_serialized:
                # One owner lock spans validation, final verification, admission,
                # and implementation. A changed generation therefore waits for
                # the preceding implementation to actually finish; validation
                # never deletes or overlaps its capacity ownership.
                owner = ImplementationOwner("issue", item_number)
                slots = self._get_implementation_slots(repo_name)
                retained_async_owner = slots.has_provider_sessions(owner)
                if (slots.active_execution_ids(owner) or retained_async_owner) and not continue_execution:
                    try:
                        owned_snapshot = self._reconcile_validation_snapshot(
                            repo_name,
                            item_number,
                            self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number),
                        )
                    except ParentSpecificationError as exc:
                        result.error = f"Parent-Issue reconciliation blocked processing: {exc}"
                        result.target_outcome = ExplicitTargetOutcome.BLOCKED
                        result.actions = ["Blocked - invalid Parent-Issue relationship metadata"]
                        return result
                    except ParentOperationalError as exc:
                        result.error = f"Parent-Issue reconciliation is temporarily unavailable: {exc}"
                        result.target_outcome = ExplicitTargetOutcome.DEFERRED
                        result.actions = ["Deferred - Parent-Issue reconciliation requires retry"]
                        result.refill_retry_required = True
                        return result
                    owned_parent = self._get_authoritative_parent_number(repo_name, item_number, owned_snapshot)
                    if owned_parent is not None:
                        self._validate_submitted_parent_generation_for_child(repo_name, item_number, owned_snapshot)
                    owned_child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
                    owned_children = owned_child_reader(repo_name, item_number) if callable(owned_child_reader) else []
                    if is_implementation_ready(owned_snapshot) and owned_parent is None and isinstance(owned_children, list) and not owned_children:
                        owned_title = str(owned_snapshot.get("title") or "")
                        owned_body = str(owned_snapshot.get("body") or "")
                        owned_manifest = build_normative_issue_manifest(item_number, owned_title, owned_body)
                        if owned_manifest.error is None and self._is_issue_specification_validation_enabled(repo_name, config):
                            owned_validator = self._get_specification_validator(repo_name)
                            owned_identity = owned_validator.identity(item_number, owned_title, owned_body)
                            owned_job = self._submit_individual_validation(
                                repo_name,
                                item_number,
                                owned_identity.key,
                                lambda: owned_validator.decide(owned_manifest, owned_title, owned_body),
                                "retained-owner-reevaluation",
                            )
                            owned_decision = self._consume_individual_validation(item_number, owned_identity.key, owned_job, "retained-owner-reevaluation")
                            if owned_decision.verdict == "ERROR":
                                result.error = "Specification validation failed; implementation-ready was preserved for retry"
                                result.refill_retry_required = True
                                return result
                            if owned_decision.verdict == "BLOCKED":
                                owned_validator.apply_blocked(
                                    self.github,
                                    owned_decision,
                                    lambda: self._standalone_validation_is_current(repo_name, owned_decision),
                                )
                                result.error = "Specification validation found material defects"
                                result.target_outcome = ExplicitTargetOutcome.BLOCKED
                                result.actions = ["Rejected - blocked specification"]
                                return result
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = [f"Deferred - implementation ownership already exists ({owner.key})"]
                    return result
                with slots.serialize(owner):
                    if owner in slots.active_owners() and not continue_execution:
                        # A completed local launch can leave a bare owner while
                        # its Issue stays open. It has no actual implementation
                        # work to coordinate, so retire it before validation.
                        # Owners with PR/provider/execution membership remain
                        # protected and cannot be silently discarded.
                        snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number)
                        validator = self._get_specification_validator(repo_name)
                        current_identity = validator.identity(
                            item_number,
                            str(snapshot.get("title") or ""),
                            str(snapshot.get("body") or ""),
                        ).key
                        if (is_implementation_ready(snapshot) and slots.validation_identity(owner) == current_identity) or not slots.release_unbound_idle_owner(owner):
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED
                            result.actions = [f"Deferred - implementation ownership already exists ({owner.key})"]
                            return result
                    return self._process_single_candidate_unified(
                        repo_name,
                        candidate,
                        config,
                        jules_mode,
                        explicit_only,
                        force,
                        continue_execution,
                        advance_issue_attempt,
                        generation_serialized=True,
                        authoritative_parent_number=authoritative_parent_number,
                        origin=origin,
                    )
            try:
                current_issue = self._reconcile_validation_snapshot(
                    repo_name,
                    item_number,
                    self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number),
                )
            except ParentSpecificationError as exc:
                result.error = f"Parent-Issue reconciliation blocked processing: {exc}"
                result.target_outcome = ExplicitTargetOutcome.BLOCKED
                result.actions = ["Blocked - invalid Parent-Issue relationship metadata"]
                return result
            except ParentOperationalError as exc:
                result.error = f"Parent-Issue reconciliation is temporarily unavailable: {exc}"
                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                result.actions = ["Deferred - Parent-Issue reconciliation requires retry"]
                result.refill_retry_required = True
                return result
            except Exception as exc:
                result.error = str(exc)
                result.refill_retry_required = True
                return result
            if not isinstance(current_issue, dict) or current_issue.get("number") != item_number:
                result.error = f"Refusing Issue dispatch for {repo_name}#{item_number}: GitHub returned an ambiguous item snapshot"
                return result
            if "pull_request" in current_issue:
                result.error = f"Refusing Issue dispatch for {repo_name}#{item_number}: GitHub identifies the target as pr"
                return result

            inherited_parent_number: Optional[int] = None
            decomposition_decision: Optional[DecompositionDecision] = None
            decomposition_validator: Optional[DecompositionValidationLifecycle] = None
            authoritative_set: Optional[tuple[Dict[str, Any], List[Dict[str, Any]]]] = None
            independently_ready = is_implementation_ready(current_issue)
            live_parent_number = authoritative_parent_number or self._get_authoritative_parent_number(repo_name, item_number, current_issue)
            # Current authoritative relationship data, never the collected
            # candidate hint, decides whether set authorization is mandatory.
            parent_details: Optional[Dict[str, Any]]
            if isinstance(live_parent_number, int):
                parent_details = {"number": live_parent_number}
            else:
                parent_details = None
            if isinstance(parent_details, dict) and isinstance(parent_details.get("number"), int):
                inherited_parent_number = int(parent_details["number"])
                try:
                    authoritative_set = self._fetch_authoritative_decomposition_set(repo_name, inherited_parent_number)
                except Exception as exc:
                    result.error = f"Cannot fetch authoritative parent/child specification set: {exc}"
                    return result
                if authoritative_set is None or item_number not in {child.get("number") for child in authoritative_set[1]}:
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - child is no longer in the authoritative parent set"]
                    return result
                if not self._is_open_issue(authoritative_set[0]):
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - authoritative parent is closed"]
                    return result
                if not is_implementation_ready(authoritative_set[0]):
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = [f"Skipped - authoritative parent is missing {IMPLEMENTATION_READY_LABEL} label"]
                    return result

            inherited_ready = authoritative_set is not None and self._is_open_issue(authoritative_set[0]) and is_implementation_ready(authoritative_set[0])

            # Readiness is intentionally decided from the same cache-bypassing
            # snapshot as the dispatch type and requirement contract. Candidate
            # data may have been collected earlier, so trusting its labels would
            # allow a subsequently removed readiness label to start work. Keep
            # this before slot resolution and every implementation ownership side
            # effect; explicit/forced processing therefore cannot bypass it.
            if not self._is_open_issue(current_issue) or (not independently_ready and not inherited_ready):
                logger.info(f"Skipping Issue #{item_number} - missing {IMPLEMENTATION_READY_LABEL} label")
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = [f"Skipped - missing {IMPLEMENTATION_READY_LABEL} label"]
                _record_issue_stage_result(item_number, "issue.readiness-admission", f"issue#{item_number} readiness admission", Outcome.SKIPPED, {"reason": result.actions[0]})
                return result

            if inherited_ready:
                assert authoritative_set is not None
                parent_snapshot, child_snapshots = authoritative_set
                if self._defer_initial_issue_stabilization(repo_name, parent_snapshot):
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - readiness submission is in its initial stabilization window"]
                    return result
                decomposition_validator = self._get_decomposition_validator(repo_name)
                individual_validator = self._get_specification_validator(repo_name)
                decomposition_enabled = self._is_issue_decomposition_validation_enabled(repo_name, config)
                if decomposition_enabled and decomposition_validator.is_reissue_required(inherited_parent_number or 0) is True:
                    result.error = "Parent specification set requires a replacement Issue number"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Rejected - parent set is durably reissue-required"]
                    return result
                spec_validation_enabled = self._is_issue_specification_validation_enabled(repo_name, config)
                if spec_validation_enabled and individual_validator.is_reissue_required(item_number) is True:
                    result.error = "Child specification requires a replacement Issue number"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Rejected - child is durably reissue-required"]
                    return result
                decomposition_job, eager_child_jobs = self._schedule_parent_validations(repo_name, authoritative_set, config)
                decomposition_decision, eager_child_decisions = self._join_parent_validations(decomposition_job, eager_child_jobs)
                if decomposition_enabled and decomposition_decision is not None:
                    if decomposition_decision.verdict == "ERROR":
                        result.error = "Decomposition validation failed; parent readiness was preserved for retry"
                        result.target_outcome = ExplicitTargetOutcome.DEFERRED
                        result.actions = ["Deferred - decomposition validation error"]
                        # This child observes the parent's decomposition-job gate
                        # from its own execution scope; it did not run that job.
                        _record_issue_stage_result(item_number, "issue.decomposition-gate-observed", f"issue#{item_number} decomposition gate observed", Outcome.FAILED, {"parent_number": inherited_parent_number})
                        return result
                    if decomposition_decision.verdict == "BLOCKED":
                        try:
                            side_effect_error = decomposition_validator.apply_blocked(
                                self.github,
                                decomposition_decision,
                                lambda number: self._fetch_authoritative_decomposition_set(repo_name, number),
                            )
                        except Exception as exc:
                            side_effect_error = str(exc)
                        result.error = "Parent/child decomposition validation found material defects"
                        result.target_outcome = ExplicitTargetOutcome.BLOCKED
                        result.actions = ["Rejected - blocked parent/child decomposition"]
                        if side_effect_error:
                            result.error += f"; GitHub side effect failed: {side_effect_error}"
                        _record_issue_stage_result(item_number, "issue.decomposition-gate-observed", f"issue#{item_number} decomposition gate observed", Outcome.BLOCKED, {"parent_number": inherited_parent_number})
                        return result
                if spec_validation_enabled:
                    for eager_decision in eager_child_decisions.values():
                        if eager_decision.verdict == "ERROR":
                            result.error = "Individual validation failed; parent readiness was preserved for retry"
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED
                            result.actions = ["Deferred - child specification validation error"]
                            _record_issue_stage_result(item_number, "issue.individual-validation", f"issue#{item_number} individual validation", Outcome.FAILED, {"issue_number": item_number})
                            return result
            current_body = str(current_issue.get("body") or "")
            current_title = str(current_issue.get("title") or "")
            contract = build_normative_issue_manifest(item_number, current_title, current_body)
            if contract.error:
                fingerprint = hashlib.sha256(f"{REQUIREMENT_CONTRACT_PARSER_VERSION}\0{current_body}\0{contract.error}".encode("utf-8")).hexdigest()
                marker = f"<!-- {INVALID_REQUIREMENT_CONTRACT_MARKER_PREFIX}:{REQUIREMENT_CONTRACT_PARSER_VERSION}:{fingerprint} -->"
                try:
                    comments = self.github.get_issue_comments_strict(repo_name, item_number)
                except Exception as exc:
                    # An unavailable comment listing is not authoritative evidence
                    # that the diagnostic is absent. Fail closed so a transient read
                    # failure can never turn into a duplicate write.
                    result.error = f"Cannot safely check requirement contract diagnostics: {exc}"
                    logger.warning(f"Deferred invalid Issue #{item_number} because diagnostic lookup failed: {exc}")
                    return result
                already_reported = any(marker in str(comment.get("body") or "") for comment in comments if isinstance(comment, dict))
                if not already_reported:
                    diagnostic = (
                        f"{marker}\n"
                        "## Auto-Coder requirement contract validation\n\n"
                        f"Implementation has not started because the Issue requirement contract is invalid: **{contract.error}**.\n\n"
                        "Edit the `## Requirements` section so every non-empty entry uses a unique `REQ-NNN:` identifier "
                        "(for example, `- REQ-001: Describe the required observable behavior.`). Auto-Coder will re-evaluate the edited Issue on a later scan."
                    )
                    self.github.add_comment_to_issue(repo_name, item_number, diagnostic)
                logger.warning(f"Rejected Issue #{item_number} before implementation dispatch: {contract.error}")
                result.target_outcome = ExplicitTargetOutcome.BLOCKED
                result.actions = [f"Rejected - invalid requirement contract: {contract.error}"]
                result.error = contract.error
                _record_issue_stage_result(item_number, "issue.normative-contract-check", f"issue#{item_number} normative contract check", Outcome.BLOCKED, {"reason": contract.error})
                return result

            # Semantic readiness is a durable authorization for this exact title,
            # body, repository, Issue and validator policy. It deliberately runs
            # before implementation ownership/capacity is consulted.
            validator = self._get_specification_validator(repo_name)
            spec_validation_enabled = self._is_issue_specification_validation_enabled(repo_name, config)
            decision: Optional[ValidationDecision] = None
            relationship_context = self._child_review_context(*authoritative_set, item_number) if inherited_ready and authoritative_set is not None else None
            individual_identity = validator.identity(item_number, current_title, current_body, relationship_context)
            if spec_validation_enabled:
                if validator.is_reissue_required(item_number) is True:
                    result.error = "Specification requires a replacement Issue number"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Rejected - Issue is durably reissue-required"]
                    return result
                if inherited_ready:
                    # This job was submitted alongside decomposition validation, so
                    # READY completion order cannot bypass either authorization gate.
                    decision = eager_child_jobs[item_number].result()
                else:
                    job = self._submit_individual_validation(
                        repo_name,
                        item_number,
                        individual_identity.key,
                        lambda: validator.decide(contract, current_title, current_body),
                        "normal-worker-processing",
                    )
                    decision = self._consume_individual_validation(item_number, individual_identity.key, job, "normal-worker-processing")
                if decision.verdict == "ERROR":
                    result.error = "Specification validation failed; implementation-ready was preserved for retry"
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - specification validation error"]
                    result.refill_retry_required = True
                    return result
                if decision.verdict == "BLOCKED":
                    try:
                        if inherited_parent_number is not None:
                            decomposition_enabled = self._is_issue_decomposition_validation_enabled(repo_name, config)

                            def _set_is_current() -> bool:
                                latest = self._fetch_authoritative_decomposition_set(repo_name, inherited_parent_number)
                                if latest is None or not self._is_open_issue(latest[0]) or not is_implementation_ready(latest[0]) or item_number not in {child.get("number") for child in latest[1]}:
                                    return False
                                relationship = self._child_review_context(*latest, item_number)
                                if validator.identity(item_number, current_title, current_body, relationship) != decision.identity:
                                    return False
                                if decomposition_enabled and decomposition_decision is not None and decomposition_validator is not None:
                                    return decomposition_validator.identity(*latest) == decomposition_decision.identity
                                return True

                            side_effect_error = validator.apply_inherited_blocked(
                                self.github,
                                decision,
                                inherited_parent_number,
                                _set_is_current,
                            )
                        else:
                            side_effect_error = validator.apply_blocked(
                                self.github,
                                decision,
                                lambda: self._standalone_validation_is_current(repo_name, decision),
                            )
                    except GitHubRequestError as exc:
                        return self._defer_validation_publication(repo_name, item_number, decision, exc, result)
                    except Exception as exc:
                        side_effect_error = str(exc)
                    if side_effect_error:
                        logger.error(f"Specification BLOCKED side effects failed for Issue #{item_number}: {side_effect_error}")
                        result.error = f"Specification is blocked; GitHub side effect failed: {side_effect_error}"
                        result.target_outcome = ExplicitTargetOutcome.BLOCKED
                        result.actions = ["Rejected - blocked specification (side effects incomplete)"]
                        return result
                    result.error = "Specification validation found material defects"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Rejected - blocked specification"]
                    return result

            # This is the final cache-bypassing check immediately before slot and
            # ownership handling. READY for an edited or withdrawn submission is
            # not transferable.
            try:
                dispatch_snapshot = self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number)
            except Exception as exc:
                result.error = f"Cannot confirm validated Issue generation before dispatch: {exc}"
                result.refill_retry_required = True
                return result
            # Reconcile from the same cache-bypassing generation used for final
            # admission, after all awaited validation. No earlier graph result
            # or force/urgent path is authorization.
            if isinstance(self.github, GitHubClient) and hasattr(self.github, "token"):
                try:
                    dependency_satisfaction = self._reconcile_sibling_dependencies(repo_name, item_number, dispatch_snapshot)
                except Exception as exc:
                    _record_issue_stage_result(item_number, "issue.sibling-dependency-gate", f"issue#{item_number} sibling dependency gate", Outcome.DEFERRED, {"reason": "relationship reconciliation unavailable"})
                    result.error = f"Sibling dependency reconciliation is unresolved: {exc}"
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - unresolved sibling dependency reconciliation"]
                    return result
                dependency_outcome = Outcome.COMPLETED if dependency_satisfaction is DependencySatisfaction.SATISFIED else Outcome.BLOCKED if dependency_satisfaction is DependencySatisfaction.INVALID else Outcome.DEFERRED
                _record_issue_stage_result(item_number, "issue.sibling-dependency-gate", f"issue#{item_number} sibling dependency gate", dependency_outcome, {"satisfaction": dependency_satisfaction.value})
                if dependency_satisfaction is DependencySatisfaction.INVALID:
                    result.error = "Sibling dependency declaration is invalid"
                    result.target_outcome = ExplicitTargetOutcome.BLOCKED
                    result.actions = ["Rejected - invalid sibling dependency declaration"]
                    return result
                if dependency_satisfaction in {DependencySatisfaction.WAITING, DependencySatisfaction.UNAVAILABLE}:
                    result.target_outcome = ExplicitTargetOutcome.DEFERRED
                    result.actions = ["Deferred - sibling prerequisite remains open or unavailable"]
                    return result
            dispatch_relationship: Optional[IndividualRelationshipContext] = None
            submission_current = self._is_open_issue(dispatch_snapshot) and is_implementation_ready(dispatch_snapshot)
            if spec_validation_enabled and validator.is_reissue_required(item_number) is True:
                submission_current = False
            decomposition_enabled = self._is_issue_decomposition_validation_enabled(repo_name, config)
            if inherited_parent_number is not None:
                latest_set = self._fetch_authoritative_decomposition_set(repo_name, inherited_parent_number)
                if latest_set is not None:
                    dispatch_relationship = self._child_review_context(*latest_set, item_number)
                submission_current = latest_set is not None and self._is_open_issue(latest_set[0]) and is_implementation_ready(latest_set[0]) and item_number in {child.get("number") for child in latest_set[1]}
                if decomposition_enabled and decomposition_decision is not None and decomposition_validator is not None:
                    submission_current = submission_current and latest_set is not None and decomposition_validator.identity(*latest_set) == decomposition_decision.identity
            elif submission_current:
                # An Issue classified as standalone can become a parent without
                # changing its own text or labels. Recheck membership before any
                # ownership-facing operation and require a new set pass instead.
                direct_child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
                latest_children = direct_child_reader(repo_name, item_number) if callable(direct_child_reader) else []
                submission_current = not (isinstance(latest_children, list) and latest_children) and self._standalone_relationship_is_current(repo_name, item_number, dispatch_snapshot)
            dispatch_identity = validator.identity(
                item_number,
                str(dispatch_snapshot.get("title") or ""),
                str(dispatch_snapshot.get("body") or ""),
                dispatch_relationship,
            )
            expected_identity = decision.identity if spec_validation_enabled and decision is not None else individual_identity
            if not submission_current or dispatch_identity != expected_identity:
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = ["Skipped - validated Issue generation is stale or no longer submitted"]
                return result

            # All subsequent admission checks must consume exactly the
            # authoritative generation whose specification was authorized.
            candidate.data.update(dispatch_snapshot)
            collected_candidate.data.update(dispatch_snapshot)

            # Refill and direct dispatch share this last authoritative hierarchy
            # gate. Candidate collection metadata is not sufficient because a
            # child can open after enumeration and before slot admission.
            try:
                hierarchy_blocked = self._has_open_sub_issues(repo_name, candidate)
            except Exception as exc:
                result.error = f"Cannot establish current Issue hierarchy before dispatch: {exc}"
                result.refill_retry_required = True
                return result
            if hierarchy_blocked:
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = ["Skipped - unresolved Issue hierarchy dependency"]
                return result

        # Validation and other authorization work above belongs to the already
        # started critical operation and may publish its decision. A drain that
        # arrived while it ran must stop before implementation ownership or any
        # local/cloud provider dispatch is started.
        if self.is_draining:
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            result.actions = ["Deferred - graceful shutdown began before implementation dispatch"]
            return result

        slots = self._get_implementation_slots(repo_name)
        try:
            owner = slots.resolve_owner(candidate.type, candidate.data, self.github)
        except ImplementationOwnerResolutionError as exc:
            result.error = f"Cannot safely resolve logical implementation owner: {exc}"
            return result
        except Exception as exc:
            result.error = str(exc)
            return result

        implementation_pr = item_number if candidate.type == "pr" and owner.kind != "pr" else None
        labels = candidate.data.get("labels", [])
        urgent_issue = candidate.type == "issue" and isinstance(labels, list) and any(label == "urgent" or (isinstance(label, dict) and label.get("name") == "urgent") for label in labels)

        def issue_generation_is_current() -> bool:
            if candidate.type != "issue":
                return True
            latest = self.github.get_issue_dispatch_snapshot_strict(repo_name, item_number)
            latest_relationship: Optional[IndividualRelationshipContext] = None
            if inherited_parent_number is not None:
                identity_set = self._fetch_authoritative_decomposition_set(repo_name, inherited_parent_number)
                if identity_set is not None:
                    latest_relationship = self._child_review_context(*identity_set, item_number)
            child_current = isinstance(latest, dict) and self._is_open_issue(latest) and validator.identity(item_number, str(latest.get("title") or ""), str(latest.get("body") or ""), latest_relationship) == expected_identity
            if not child_current:
                return False
            latest_labels = latest.get("labels", [])
            latest_is_urgent = isinstance(latest_labels, list) and any(label == "urgent" or (isinstance(label, dict) and label.get("name") == "urgent") for label in latest_labels)
            if urgent_issue and not latest_is_urgent:
                return False
            if inherited_parent_number is not None:
                current_set = self._fetch_authoritative_decomposition_set(repo_name, inherited_parent_number)
                if current_set is None or not self._is_open_issue(current_set[0]) or not is_implementation_ready(current_set[0]) or item_number not in {child.get("number") for child in current_set[1]}:
                    return False
                if decomposition_enabled and decomposition_decision is not None and decomposition_validator is not None:
                    if decomposition_validator.identity(*current_set) != decomposition_decision.identity:
                        return False
                if isinstance(self.github, GitHubClient) and hasattr(self.github, "token"):
                    try:
                        return self._reconcile_sibling_dependencies(repo_name, item_number, latest) is DependencySatisfaction.SATISFIED
                    except Exception:
                        return False
                return True
            direct_child_reader = getattr(self.github, "get_direct_sub_issues_strict", None)
            latest_children = direct_child_reader(repo_name, item_number) if callable(direct_child_reader) else []
            return is_implementation_ready(latest) and not (isinstance(latest_children, list) and latest_children) and self._standalone_relationship_is_current(repo_name, item_number, latest)

        # Try to reuse an existing owner before reconciliation.  In particular,
        # this atomically records a newly discovered branch-linked PR while its
        # Issue owner still exists.  Reconciling first could release that owner
        # when the closed Issue has no timeline relationship for the PR.
        with repository_dispatch_authority(repo_name):
            try:
                generation_is_current = issue_generation_is_current()
            except Exception as exc:
                result.error = f"Cannot confirm Issue generation before ownership admission: {exc}"
                result.refill_retry_required = True
                return result
            if not generation_is_current:
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.actions = ["Skipped - validated Issue generation changed before ownership admission"]
                return result
            if self.is_draining:
                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                result.actions = ["Deferred - graceful shutdown began before implementation ownership admission"]
                return result
            execution_id = slots.current_execution_id(owner) if continue_execution else None
            inherited_execution = execution_id is not None
            owner_existed_before_admission = owner in slots.active_owners()
            try:
                if not inherited_execution:
                    execution_id = slots.start_execution(
                        owner,
                        implementation_pr=implementation_pr,
                        bypass_capacity=explicit_only,
                        bypass_active_execution=explicit_only and force and candidate.type == "pr",
                        allow_urgent_emergency=urgent_issue,
                        github_client=self.github if owner.kind == "issue" and isinstance(self.github, GitHubClient) else None,
                    )
                if execution_id is None and not explicit_only:
                    slots.reconcile(self.github)
                    try:
                        generation_is_current = issue_generation_is_current()
                    except Exception as exc:
                        result.error = f"Cannot confirm Issue generation during capacity reconciliation: {exc}"
                        result.refill_retry_required = True
                        return result
                    if not generation_is_current:
                        result.target_outcome = ExplicitTargetOutcome.SKIPPED
                        result.actions = ["Skipped - validated Issue generation changed during capacity reconciliation"]
                        return result
                    execution_id = slots.start_execution(
                        owner,
                        implementation_pr=implementation_pr,
                        allow_urgent_emergency=urgent_issue,
                        github_client=self.github if owner.kind == "issue" and isinstance(self.github, GitHubClient) else None,
                    )
            except ImplementationHierarchyConflict as exc:
                result.target_outcome = ExplicitTargetOutcome.DEFERRED
                result.actions = [f"Deferred - direct parent/child implementation conflict ({exc})"]
                return result
            except ImplementationHierarchyUnavailable as exc:
                result.error = f"Cannot establish authoritative hierarchy for implementation admission: {exc}"
                result.refill_retry_required = True
                return result
        if execution_id is None:
            reason = "active execution already exists" if slots.active_execution_ids(owner) else "logical implementation limit is occupied"
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            result.actions = [f"Deferred - {reason} ({owner.key})"]
            result.capacity_deferred = not bool(slots.active_execution_ids(owner))
            return result

        if candidate.type == "issue" and not inherited_execution:
            if not slots.record_validation_identity(owner, expected_identity.key):
                slots.finish_execution(owner, execution_id)
                result.error = "Could not bind implementation ownership to validated Issue generation"
                return result

        try:
            # Issue force changes scheduling eligibility, not generation-level
            # serialization. PR forced recovery retains its separate execution.
            if explicit_only and force and candidate.type == "pr":
                if advance_issue_attempt:
                    result = self._process_single_candidate_reserved(
                        repo_name,
                        candidate,
                        config,
                        jules_mode,
                        force_adversarial_validation=True,
                        advance_issue_attempt=True,
                    )
                else:
                    result = self._process_single_candidate_reserved(repo_name, candidate, config, jules_mode, force_adversarial_validation=True)
            else:
                with slots.serialize(owner):
                    if advance_issue_attempt:
                        result = self._process_single_candidate_reserved(repo_name, candidate, config, jules_mode, advance_issue_attempt=True)
                    else:
                        result = self._process_single_candidate_reserved(repo_name, candidate, config, jules_mode)
        finally:
            if not inherited_execution:
                slots.finish_execution(owner, execution_id)
                if not owner_existed_before_admission and result.actions == ["Skipped - another instance started processing (@auto-coder label added)"]:
                    slots.release_unbound_idle_owner(owner)
                slots.reconcile(self.github)
        return result

    def _process_single_candidate_reserved(
        self,
        repo_name: str,
        candidate: Candidate,
        config: AutomationConfig,
        jules_mode: bool = False,
        force_adversarial_validation: bool = False,
        advance_issue_attempt: bool = False,
    ) -> CandidateProcessingResult:
        """Process a candidate after its durable owner slot is reserved."""
        result = CandidateProcessingResult(
            type=candidate.type,
            number=candidate.data.get("number"),
            title=candidate.data.get("title"),
            success=False,
            actions=[],
            error=None,
        )

        if self.is_draining:
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            result.actions = ["Deferred - graceful shutdown began before reserved dispatch"]
            return result

        try:
            # Get item number and type
            item_number = candidate.data.get("number")
            item_type = candidate.type

            # Ensure item_number is not None
            if item_number is None:
                raise ValueError(f"Item number is missing for {item_type} #{candidate.data.get('number', 'N/A')}")

            implementation_slots = self._get_implementation_slots(repo_name)

            if item_type == "pr":
                from .pr_processor import _reject_unsafe_codex_cloud_pr

                unsafe_branch_result = _reject_unsafe_codex_cloud_pr(self.github, repo_name, candidate.data, config)
                if unsafe_branch_result.closed:
                    result.actions = list(unsafe_branch_result.actions)
                    result.success = True
                    result.target_outcome = ExplicitTargetOutcome.SUCCESS
                    return result
                if unsafe_branch_result.metadata_error:
                    result.actions = list(unsafe_branch_result.actions)
                    result.error = unsafe_branch_result.metadata_error
                    return result
                candidate.data = unsafe_branch_result.authoritative_pr_data or candidate.data

            # Check author allowlists before any processing or API actions
            if item_type == "pr" and not self._is_pr_author_allowed(candidate.data):
                logger.info(f"Skipping PR #{item_number} - author not in PR allowlist")
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.target_reason = "PR author is not in the allowlist"
                return result
            elif item_type == "issue" and not self._is_issue_author_allowed(candidate.data):
                logger.info(f"Skipping Issue #{item_number} - author not in Issue allowlist")
                result.target_outcome = ExplicitTargetOutcome.SKIPPED
                result.target_reason = "Issue author is not in the allowlist"
                return result

            # Candidate data is not an authority for GitHub's item type. The
            # Issues API represents pull requests as issue-like objects, and
            # candidates can also arrive from retries, explicit target_type
            # arguments, or other internal enqueue paths. Gate at the common
            # Issue dispatch boundary, before labels, attempts, branches,
            # CloudRuns, or task-start comments are ever created.
            if item_type == "issue":
                authoritative_type = self._get_authoritative_item_type(repo_name, item_number)
                if authoritative_type != "issue":
                    raise ValueError(f"Refusing Issue dispatch for {repo_name}#{item_number}: GitHub identifies the target as {authoritative_type}")
                if advance_issue_attempt:
                    from .attempt_manager import increment_attempt

                    increment_attempt(repo_name, item_number)

            # Close empty PRs or stale Jules PRs before the label gate below.
            if item_type == "pr":
                from .pr_processor import _close_empty_pr, _close_stale_jules_pr

                empty_pr_result = _close_empty_pr(self.github, repo_name, candidate.data, config)
                if empty_pr_result.closed:
                    for action in empty_pr_result.actions:
                        logger.info(f"PR #{item_number}: {action}")
                    result.actions = list(empty_pr_result.actions)
                    # Start the new attempt on the unlocked issue(s) right away
                    for issue_number in empty_pr_result.issue_numbers:
                        result.actions.extend(self._process_unlocked_issue(repo_name, issue_number, config, jules_mode))
                    result.success = True
                    result.target_outcome = ExplicitTargetOutcome.SUCCESS
                    return result

                stale_jules_result = _close_stale_jules_pr(self.github, repo_name, candidate.data, config)
                if stale_jules_result.closed:
                    for action in stale_jules_result.actions:
                        logger.info(f"PR #{item_number}: {action}")
                    result.actions = list(stale_jules_result.actions)
                    # Start the new attempt on the unlocked issue(s) right away
                    for issue_number in stale_jules_result.issue_numbers:
                        result.actions.extend(self._process_unlocked_issue(repo_name, issue_number, config, jules_mode))
                    result.success = True
                    result.target_outcome = ExplicitTargetOutcome.SUCCESS
                    return result

            # Use LabelManager context manager to handle @auto-coder label automatically
            with LabelManager(
                self.github,
                repo_name,
                item_number,
                item_type=item_type,
                config=config,
                # A replacement attempt continues the Issue's existing logical
                # ownership. Its retained @auto-coder label belongs to this
                # lifecycle, not another worker, so it must not reject the handoff.
                known_labels=candidate.data.get("labels") if candidate.data else None,
            ) as should_process:
                if not should_process:
                    get_trace_logger().log("Skip", f"Skipping {item_type} #{item_number} - already processing", item_type=item_type, item_number=item_number, details={"reason": "label_exists"})
                    result.target_outcome = ExplicitTargetOutcome.SKIPPED
                    result.actions = ["Skipped - another instance started processing (@auto-coder label added)"]
                    return result

                if item_type == "issue":
                    # Check if issue has difficult label
                    is_difficult = False
                    if candidate.data:
                        raw_labels = candidate.data.get("labels", [])
                        for label in raw_labels:
                            lname = label.get("name", "") if isinstance(label, dict) else str(label)
                            if lname.strip().lower() == "difficult":
                                is_difficult = True
                                break

                    if is_difficult:
                        # For difficult issues, bypass Jules and delegate to backend_with_high_score_cloud directly
                        logger.info(f"Issue #{item_number} has 'difficult' label. Delegating to backend_with_high_score_cloud.")
                        get_trace_logger().log("Dispatch", f"Dispatching issue #{item_number} to High Score Cloud Backend (difficult label)", item_type="issue", item_number=item_number, details={"mode": "high_score_cloud"})
                        _record_issue_stage_result(item_number, "issue.dispatch-route", f"issue#{item_number} dispatch route", Outcome.COMPLETED, {"route": "high-score-cloud"})
                        from .issue_processor import _process_issue_high_score_cloud

                        result.actions = _process_issue_high_score_cloud(
                            repo_name,
                            candidate.data,
                            config,
                            self.github,
                            label_context=should_process,
                            implementation_slots=implementation_slots,
                        )
                    elif jules_mode:
                        # Use Cloud mode (backend_cloud, defaulting to Jules) for issue processing
                        get_trace_logger().log("Dispatch", f"Dispatching issue #{item_number} to Cloud Mode (backend_cloud)", item_type="issue", item_number=item_number, details={"mode": "cloud"})
                        _record_issue_stage_result(item_number, "issue.dispatch-route", f"issue#{item_number} dispatch route", Outcome.COMPLETED, {"route": "cloud"})
                        from .issue_processor import _process_issue_cloud_backend

                        result.actions = _process_issue_cloud_backend(
                            repo_name,
                            candidate.data,
                            config,
                            self.github,
                            label_context=should_process,
                            implementation_slots=implementation_slots,
                        )

                    else:
                        # Regular issue processing
                        get_trace_logger().log("Dispatch", f"Dispatching issue #{item_number} to Local Mode", item_type="issue", item_number=item_number, details={"mode": "local"})
                        _record_issue_stage_result(item_number, "issue.dispatch-route", f"issue#{item_number} dispatch route", Outcome.COMPLETED, {"route": "local"})
                        result.actions = self._take_issue_actions(repo_name, candidate.data)

                    # Cloud launchers persist the authoritative provider task in
                    # CloudManager. Mirror that production output into logical
                    # slot membership before the local launch execution returns,
                    # so later generations cannot validate around remote work.
                    from .cloud_manager import CloudManager

                    binding = CloudManager(repo_name).get_binding(item_number)
                    if binding is not None:
                        owner = ImplementationOwner("issue", item_number)
                        if not self._get_implementation_slots(repo_name).record_provider_session(owner, binding.task_id):
                            raise RuntimeError(f"Could not retain asynchronous implementation ownership for issue #{item_number}")
                    result.success = True
                    result.target_outcome = ExplicitTargetOutcome.SUCCESS
                elif item_type == "pr":
                    # PR processing
                    pr_result = process_pull_request(
                        self.github,
                        config,
                        repo_name,
                        candidate.data,
                        force_adversarial_validation=force_adversarial_validation,
                        adversarial_validation_scheduler=self.adversarial_validation_scheduler,
                    )
                    result.actions = pr_result.actions_taken
                    # Check if there was an error during processing
                    if pr_result.error:
                        result.error = pr_result.error
                    result.outcome = pr_result.outcome
                    result.success = pr_result.outcome != PRProcessingOutcome.FAILED
                    result.target_outcome = {
                        PRProcessingOutcome.SUCCESS: ExplicitTargetOutcome.SUCCESS,
                        PRProcessingOutcome.DEFERRED: ExplicitTargetOutcome.DEFERRED,
                        PRProcessingOutcome.FAILED: ExplicitTargetOutcome.FAILED,
                    }[pr_result.outcome]

        except AutoCoderRetryableBackendError as e:
            diagnostic = str(e)
            result.actions.append(f"Deferred: {diagnostic}")
            result.error = diagnostic
            result.outcome = PRProcessingOutcome.DEFERRED
            result.success = True
            result.target_outcome = ExplicitTargetOutcome.DEFERRED
            logger.warning(f"Deferred {candidate.type} #{candidate.data.get('number', 'N/A')} after retryable backend failure: {diagnostic}")
        except Exception as e:
            result.error = str(e)
            logger.error(f"Error processing {candidate.type} #{candidate.data.get('number', 'N/A')}: {e}")

        return result

    def _defer_issue_evaluation(
        self,
        repo_name: str,
        item_number: int,
        issue_data: Dict[str, Any],
        error: GitHubRequestError,
        result: CandidateProcessingResult,
    ) -> CandidateProcessingResult:
        """Retain an Issue hierarchy/readiness evaluation interrupted by GitHub.

        Mirrors ``process_pull_request``'s ``GitHubRequestError`` handling for
        PRs (see ``pr_processor.py``): the obligation is durably registered
        with the pending-work scheduler (``ISSUE_PROCESSING_STAGE``) instead of
        being reported as an ordinary error, so a registered stage handler
        resumes it through current authoritative state (REQ-001, REQ-002).
        """
        revision = _issue_content_revision(issue_data)
        identity = WorkIdentity(repo_name, f"issue:{item_number}", ISSUE_PROCESSING_STAGE, revision)
        obligation = get_pending_work_store().defer(
            identity,
            error,
            (ISSUE_PROCESSING_REFRESH_EFFECT, ISSUE_PROCESSING_STAGE),
        )
        logger.warning(
            "Deferred Issue #{} after GitHub operational failure {}; next eligible at {}",
            item_number,
            obligation.reason.value,
            obligation.not_before,
        )
        self.pending_work_scheduler.wake()
        result.error = str(error)
        result.target_outcome = ExplicitTargetOutcome.DEFERRED
        result.actions = [f"Deferred GitHub-dependent work: {obligation.reason.value}"]
        return result

    def _defer_validation_publication(
        self,
        repo_name: str,
        item_number: int,
        decision: ValidationDecision,
        error: GitHubRequestError,
        result: CandidateProcessingResult,
    ) -> CandidateProcessingResult:
        """Retain a BLOCKED publication (diagnostic/readiness-withdrawal) interrupted by GitHub.

        Unlike ``_defer_issue_evaluation`` this never reports the durable
        ``BLOCKED`` terminal outcome on an operational failure (REQ-006): a
        publication interruption stays a DEFERRED, automatically-resumable
        obligation on ``VALIDATION_PUBLICATION_STAGE``, and only the effects
        this specific decision has not yet durably completed (per the
        ``SpecificationValidationStore`` flags) are retained (REQ-001, REQ-002).
        """
        identity = validation_publication_identity(repo_name, item_number, decision.identity.key)
        validator = self._get_specification_validator(repo_name)
        current = validator.store.get(decision.identity)
        unfinished: tuple[str, ...] = ()
        if current is None or not current.findings_published:
            unfinished += (DIAGNOSTIC_EFFECT,)
        if current is None or not current.readiness_removed:
            unfinished += (READINESS_WITHDRAWAL_EFFECT,)
        obligation = get_pending_work_store().defer(identity, error, unfinished)
        logger.warning(
            "Deferred BLOCKED publication for Issue #{} after GitHub operational failure {}; next eligible at {}",
            item_number,
            obligation.reason.value,
            obligation.not_before,
        )
        self.pending_work_scheduler.wake()
        result.error = str(error)
        result.target_outcome = ExplicitTargetOutcome.DEFERRED
        result.actions = [f"Deferred BLOCKED publication: {obligation.reason.value}"]
        return result

    def _get_implementation_slots(self, repo_name: str) -> ImplementationSlotRepository:
        if self.implementation_slots is None or self.implementation_slots.repo_name != repo_name:
            self.implementation_slots = ImplementationSlotRepository(repo_name, self.config.MAX_CONCURRENT_IMPLEMENTATIONS)
        return self.implementation_slots

    def _get_authoritative_item_type(self, repo_name: str, item_number: int) -> str:
        """Establish an issue-like target's authoritative GitHub type.

        A caller-supplied candidate type is not authoritative: GitHub's Issues
        API represents pull requests as issue-like objects, and candidates can
        arrive already misclassified from stale collections, explicit
        target_type arguments, or other internal enqueue paths. Delegates to
        the shared implementation (util.gh_cache.resolve_authoritative_item_type)
        so every Issue dispatch path -- including the direct Jules-fallback
        resumption path in issue_processor.handle_stale_jules_issue_sessions --
        uses the same cache-bypassing lookup and the same fail-closed behavior.
        """
        return resolve_authoritative_item_type(self.github, repo_name, item_number)

    def _process_unlocked_issue(
        self,
        repo_name: str,
        issue_number: int,
        config: AutomationConfig,
        jules_mode: bool,
        advance_attempt: bool = False,
    ) -> List[str]:
        """Process an issue whose Jules attempt was just abandoned.

        Args:
            repo_name: Repository name
            issue_number: Issue to start the next attempt on
            config: AutomationConfig instance
            jules_mode: Whether to use Jules mode for processing

        Returns:
            Actions taken while processing the issue
        """
        issue_candidate = self._create_candidate_from_single(repo_name, "issue", issue_number)
        if not issue_candidate:
            logger.warning(f"Could not build a candidate for issue #{issue_number}, skipping the new attempt")
            return [f"Failed to start a new attempt for issue #{issue_number}"]

        logger.info(f"Starting a new attempt for issue #{issue_number}")
        issue_result = self._process_single_candidate_unified(
            repo_name,
            issue_candidate,
            config,
            jules_mode=jules_mode,
            continue_execution=True,
            advance_issue_attempt=advance_attempt,
            origin="stale-provider-session-reassignment",
        )
        actions = [f"Started a new attempt for issue #{issue_number}"] + list(issue_result.actions)
        if issue_result.error:
            actions.append(f"Error processing issue #{issue_number}: {issue_result.error}")
        return actions

    def _process_single_candidate(self, repo_name: str, candidate: Candidate, origin: str = "worker") -> CandidateProcessingResult:
        """Process a single candidate (issue/PR).

        Args:
            repo_name: Repository name
            candidate: Target candidate to process
            origin: Diagnostic label for what triggered this evaluation
                (REQ-001); does not affect processing behavior.

        Returns:
            Processing result
        """
        # Check if Jules mode should be used based on configuration
        from .llm_backend_config import is_jules_mode_enabled

        jules_mode = is_jules_mode_enabled()

        return self._process_single_candidate_unified(
            repo_name,
            candidate,
            self.config,
            jules_mode=jules_mode,
            origin=origin,
        )

    def run(self, repo_name: str) -> Dict[str, Any]:
        """Run the main automation process."""
        logger.info(f"Starting automation for repository: {repo_name}")

        # Check if current branch corresponds to a closed PR/Issue
        if not self._check_and_handle_closed_branch(repo_name):
            # check_and_handle_closed_state will handle branch switching and exit
            # This line should not be reached, but just in case
            return {
                "repository": repo_name,
                "timestamp": datetime.now().isoformat(),
                "issues_processed": [],
                "prs_processed": [],
                "errors": ["Exited due to closed item on current branch"],
            }

        # Get LLM backend information
        llm_backend_info = self._get_llm_backend_info()

        results: Dict[str, Any] = {
            "repository": repo_name,
            "timestamp": datetime.now().isoformat(),
            "llm_backend": llm_backend_info["backend"],
            "llm_provider": llm_backend_info["provider"],
            "llm_model": llm_backend_info["model"],
            "issues_processed": [],
            "prs_processed": [],
            "errors": [],
        }

        try:
            # Get initial candidates
            total_processed = 0

            while True:
                heartbeat("run:check-updates", repo_name)

                # Check for updates and restart if necessary
                check_for_updates_and_restart()

                if self._claim_jules_session_list_refresh():
                    # All list-dependent maintenance shares this cycle's fresh listing.
                    invalidate_jules_sessions_cache()

                    # Check and resume failed Jules sessions
                    check_and_resume_or_archive_sessions()

                    # Take issues away from Jules sessions that timed out without creating a PR
                    self.handle_stale_jules_issue_sessions(repo_name)

                    # Check and start recurrent Jules tasks
                    check_and_start_recurrent_jules_tasks(
                        repo_name,
                        self._get_implementation_slots(repo_name),
                    )

                # Pull latest changes for monitored repository
                try:
                    logger.info("Pulling latest changes for monitored repository...")
                    pull_res = git_pull()
                    if not pull_res.success:
                        logger.warning(f"Failed to pull latest changes: {pull_res.stderr}")
                except Exception as e:
                    logger.warning(f"Failed to pull monitored repository: {e}")

                # Get candidates
                candidates = self._get_candidates(repo_name)

                if not candidates:
                    logger.info("No more candidates found, ending automation")
                    break

                # Process all candidates in this batch
                batch_processed = 0
                for candidate in candidates:
                    try:
                        logger.info(f"Processing {candidate.type} #{candidate.data.get('number', 'N/A')}")
                        heartbeat("run:processing", f"{candidate.type} #{candidate.data.get('number', 'N/A')}")

                        # Process the candidate
                        result = self._process_single_candidate(repo_name, candidate, origin="batch-scan-worker")

                        # Track results
                        # Convert dataclass to dict for backward compatibility with existing code
                        result_dict = {
                            "type": result.type,
                            "number": result.number,
                            "title": result.title,
                            "success": result.success,
                            "actions": result.actions,
                            "error": result.error,
                            "outcome": result.outcome.value,
                        }
                        if candidate.type == "issue":
                            results["issues_processed"].append(result_dict)  # type: ignore
                        elif candidate.type == "pr":
                            results["prs_processed"].append(result_dict)  # type: ignore

                        batch_processed += 1
                        total_processed += 1

                        logger.info(f"Successfully processed {candidate.type} #{candidate.data.get('number', 'N/A')}")

                    except Exception as e:
                        error_msg = f"Failed to process candidate: {e}"
                        logger.error(error_msg)
                        results["errors"].append(error_msg)  # type: ignore

                # If no candidates were processed in this batch, end the loop
                if batch_processed == 0:
                    logger.info("No candidates were processed in this batch, ending automation")
                    break

                # Clear GitHub API cache after each batch
                get_github_cache().clear()
                logger.debug("Cleared GitHub API cache")
            # Save results report
            self._save_report(results, "automation_report", repo_name)

            logger.info(f"Automation completed for {repo_name}")
            return results

        except Exception as e:
            error_msg = f"Automation failed for {repo_name}: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)  # type: ignore
            return results

    def process_single(
        self,
        repo_name: str,
        target_type: str,
        number: int,
        jules_mode: bool = False,
        *,
        explicit_only: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Process a single issue or PR by number.

        Args:
            repo_name: Repository name
            target_type: Type of target ('issue' or 'pr')
            number: Issue or PR number
            jules_mode: Whether to use Jules mode for processing (default: False)

        Returns:
            Dictionary with processing results
        """
        with active_repo_context(repo_name):
            os.environ["REPO_NAME"] = repo_name
            self.config.repo_name = repo_name
            # Check if Jules mode should be used based on configuration
            from .llm_backend_config import is_jules_mode_enabled

            # Both must be true for Jules mode to be enabled
            # jules_mode parameter is requested state, and is_jules_mode_enabled checks config
            jules_mode = jules_mode and is_jules_mode_enabled(repo_name=repo_name)
            from datetime import datetime

            with ProgressStage("Processing single PR/IS"):
                # Check if current branch corresponds to a closed PR/Issue
                if not self._check_and_handle_closed_branch(repo_name):
                    # check_and_handle_closed_state will handle branch switching and exit
                    # This line should not be reached, but just in case
                    closed_result: Dict[str, Any] = {
                        "repository": repo_name,
                        "timestamp": datetime.now().isoformat(),
                        "issues_processed": [],
                        "prs_processed": [],
                        "errors": ["Exited due to closed item on current branch"],
                    }
                    if explicit_only:
                        closed_result.update(
                            target_number=number,
                            target_type=target_type if target_type in {"issue", "pr"} else None,
                            target_outcome=ExplicitTargetOutcome.FAILED.value,
                            target_actions=[],
                            target_reason="Exited due to closed item on current branch",
                        )
                    return closed_result

                logger.info(f"Processing single target: type={target_type}, number={number} for {repo_name}")
                result = ProcessResult(
                    repository=repo_name,
                    timestamp=datetime.now().isoformat(),
                    target_number=number if explicit_only else None,
                )

                def explicit_result() -> Dict[str, Any]:
                    return {
                        "repository": result.repository,
                        "timestamp": result.timestamp,
                        "issues_processed": result.issues_processed,
                        "prs_processed": result.prs_processed,
                        "errors": result.errors,
                        "target_number": result.target_number,
                        "target_type": result.target_type,
                        "target_outcome": result.target_outcome,
                        "target_actions": result.target_actions,
                        "target_reason": result.target_reason,
                    }

                try:
                    # Create a Candidate from the single item
                    candidate = self._create_candidate_from_single(repo_name, target_type, number)
                    if not candidate:
                        if explicit_only:
                            result.target_outcome = ExplicitTargetOutcome.FAILED.value
                            result.target_reason = f"Could not resolve requested target #{number}"
                            result.errors.append(result.target_reason)
                        return explicit_result()

                    if explicit_only:
                        result.target_type = candidate.type if candidate.type in {"issue", "pr"} else None

                    if explicit_only and candidate.type == "issue":
                        try:
                            refreshed_target = self._preflight_explicit_issue_relationships(repo_name, number)
                            candidate.data.update(refreshed_target)
                        except ParentSpecificationError as exc:
                            result.target_outcome = ExplicitTargetOutcome.BLOCKED.value
                            result.target_reason = f"Blocked relationship reconciliation for Issue #{number}: {exc}"
                            result.target_actions = [result.target_reason]
                            result.errors.append(result.target_reason)
                            return explicit_result()
                        except ParentOperationalError as exc:
                            result.target_outcome = ExplicitTargetOutcome.DEFERRED.value
                            result.target_reason = f"Retryable relationship reconciliation failure for Issue #{number}: {exc}"
                            result.target_actions = [result.target_reason]
                            result.errors.append(result.target_reason)
                            return explicit_result()

                    # Explicit/single-item processing is another supported
                    # discovery origin for specification changes.  Resolve and
                    # validate a submitted parent generation before unified
                    # processing can reject a closed child or defer an owned
                    # child.  This mirrors invalidation-worker discovery and
                    # keeps validation eligibility independent of
                    # implementation eligibility.
                    if candidate.type == "issue":
                        self._validate_submitted_parent_generation_for_child(
                            repo_name,
                            number,
                            candidate.data,
                            target_only=explicit_only,
                        )

                    # Use unified processing function
                    processing_args = (repo_name, candidate, self.config, jules_mode)
                    if explicit_only:
                        processing_result = self._process_single_candidate_unified(
                            *processing_args,
                            explicit_only=True,
                            force=force,
                            origin="explicit-single-target",
                        )
                    else:
                        processing_result = self._process_single_candidate_unified(*processing_args, origin="explicit-single-target")

                    if explicit_only:
                        result.target_actions = list(processing_result.actions)
                        result.target_reason = processing_result.target_reason or processing_result.error or (processing_result.actions[0] if processing_result.actions else None)
                        identity_matches = processing_result.number == number and processing_result.type == candidate.type
                        target_outcome = processing_result.target_outcome
                        if not identity_matches:
                            diagnostic = f"Explicit target identity mismatch: requested {candidate.type} #{number}, " f"processed {processing_result.type} #{processing_result.number}"
                            result.errors.append(diagnostic)
                            result.target_reason = diagnostic
                            result.target_outcome = ExplicitTargetOutcome.FAILED.value
                        elif target_outcome is None:
                            diagnostic = f"Explicit processing returned no authoritative outcome for {candidate.type} #{number}"
                            result.errors.append(diagnostic)
                            result.target_reason = diagnostic
                            result.target_outcome = ExplicitTargetOutcome.FAILED.value
                        elif target_outcome is ExplicitTargetOutcome.SUCCESS and processing_result.error:
                            diagnostic = f"Contradictory successful outcome for {candidate.type} #{number}: {processing_result.error}"
                            result.errors.append(diagnostic)
                            result.target_reason = diagnostic
                            result.target_outcome = ExplicitTargetOutcome.FAILED.value
                        else:
                            result.target_outcome = target_outcome.value

                    # Only add to processed list if there was no error and processing succeeded
                    if processing_result.error:
                        # Add error to errors list instead of processed list
                        error_msg = f"Error processing {candidate.type} #{candidate.data.get('number', 'N/A')}: {processing_result.error}"
                        result.errors.append(error_msg)
                        if candidate.type == "pr":
                            result.prs_processed.append(
                                {
                                    "pr_data": candidate.data,
                                    "actions_taken": processing_result.actions,
                                    "outcome": processing_result.outcome.value,
                                }
                            )
                    elif processing_result.success:
                        # Convert to the format expected by process_single
                        if candidate.type == "issue":
                            processed_item = {
                                "issue_data": candidate.data,
                                "actions_taken": processing_result.actions,
                            }
                            result.issues_processed.append(processed_item)
                        elif candidate.type == "pr":
                            processed_item = {
                                "pr_data": candidate.data,
                                "actions_taken": processing_result.actions,
                                "outcome": processing_result.outcome.value,
                            }
                            result.prs_processed.append(processed_item)

                    # After processing, check if the single PR/issue is now closed
                    try:
                        if result.issues_processed or result.prs_processed:
                            # Get the processed item
                            first_processed_item: Dict[str, Any]
                            item_number = None
                            item_type = None

                            if result.issues_processed:
                                first_processed_item = result.issues_processed[0]
                                issue_data: Dict[str, Any] = first_processed_item.get("issue_data", {})
                                item_number = issue_data.get("number")
                                item_type = "issue"
                            elif result.prs_processed:
                                first_processed_item = result.prs_processed[0]
                                pr_data: Dict[str, Any] = first_processed_item.get("pr_data", {})
                                item_number = pr_data.get("number")
                                item_type = "pr"

                            if item_number and item_type:
                                # Check the current state of the item
                                from .util.github_action import check_and_handle_closed_state

                                check_and_handle_closed_state(
                                    repo_name,
                                    item_type,
                                    item_number,
                                    self.config,
                                    self.github,
                                )
                    except Exception as e:
                        logger.warning(f"Failed to check/handle closed item state: {e}")

                except Exception as e:
                    msg = f"Error in process_single: {e}"
                    logger.error(msg)
                    result.errors.append(msg)
                    if explicit_only:
                        result.target_outcome = ExplicitTargetOutcome.FAILED.value
                        result.target_reason = msg

            # Convert dataclass to dict for backward compatibility with existing code
            output: Dict[str, Any] = {
                "repository": result.repository,
                "timestamp": result.timestamp,
                "issues_processed": result.issues_processed,
                "prs_processed": result.prs_processed,
                "errors": result.errors,
            }
            if explicit_only:
                output.update(
                    target_number=result.target_number,
                    target_type=result.target_type,
                    target_outcome=result.target_outcome or ExplicitTargetOutcome.FAILED.value,
                    target_actions=result.target_actions,
                    target_reason=result.target_reason,
                )
                if result.target_outcome is None:
                    diagnostic = f"Explicit processing produced no outcome for target #{number}"
                    output["errors"].append(diagnostic)
                    output["target_reason"] = diagnostic
            return output

    def create_feature_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Analyze repository and create feature enhancement issues."""
        with active_repo_context(repo_name):
            os.environ["REPO_NAME"] = repo_name
            self.config.repo_name = repo_name
            return create_feature_issues(
                self.github,
                self.config,
                repo_name,
            )

    def fix_to_pass_tests(
        self,
        llm_backend_manager: Any,
        max_attempts: Optional[int] = None,
        message_backend_manager: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run tests and, if failing, repeatedly request LLM fixes until tests pass."""
        if self.config.repo_name:
            os.environ["REPO_NAME"] = self.config.repo_name
        run_override = getattr(self, "_run_local_tests", None)
        apply_override = getattr(self, "_apply_workspace_test_fix", None)

        if callable(run_override) or callable(apply_override):
            original_run = fix_to_pass_tests_runner_module.run_local_tests
            original_apply = fix_to_pass_tests_runner_module.apply_workspace_test_fix
            try:
                if callable(run_override):
                    fix_to_pass_tests_runner_module.run_local_tests = run_override
                if callable(apply_override):
                    fix_to_pass_tests_runner_module.apply_workspace_test_fix = apply_override
                return fix_to_pass_tests(
                    self.config,
                    llm_backend_manager,
                    max_attempts,
                    message_backend_manager,
                )
            finally:
                fix_to_pass_tests_runner_module.run_local_tests = original_run
                fix_to_pass_tests_runner_module.apply_workspace_test_fix = original_apply

        return fix_to_pass_tests(
            self.config,
            llm_backend_manager,
            max_attempts,
            message_backend_manager,
        )

    def _get_llm_backend_info(self) -> Dict[str, Optional[str]]:
        """Get LLM backend, provider, and model information for telemetry."""

        info: Dict[str, Optional[str]] = {
            "backend": None,
            "provider": None,
            "model": None,
        }

        def _extract_from_manager(
            manager: Optional[Any],
        ) -> Optional[Dict[str, Optional[str]]]:
            if manager is None:
                return None

            getter = getattr(manager, "get_last_backend_provider_and_model", None)
            if callable(getter):
                try:
                    backend, provider, model = getter()
                    return {"backend": backend, "provider": provider, "model": model}
                except Exception:
                    pass

            getter = getattr(manager, "get_last_backend_and_model", None)
            if callable(getter):
                try:
                    backend, model = getter()
                    return {"backend": backend, "provider": None, "model": model}
                except Exception:
                    pass
            return None

        try:
            sources = (
                lambda: get_llm_backend_manager(),
                lambda: LLMBackendManager.get_llm_instance(),
                lambda: LLMBackendManager._instance,
            )

            for source in sources:
                try:
                    details = _extract_from_manager(source())
                except (RuntimeError, AttributeError):
                    continue
                if details:
                    info.update(details)
                    return info
        except Exception as e:
            logger.debug(f"Error getting LLM backend info: {e}")

        return info

    def _save_report(self, data: Dict[str, Any], filename: str, repo_name: Optional[str] = None) -> None:
        """Save report to file.

        Args:
            data: Report data to save
            filename: Base filename (without timestamp and extension)
            repo_name: Repository name (e.g., 'owner/repo'). If provided, saves to
                      ~/.auto-coder/{repository}/ instead of the default reports/ directory.
        """
        try:
            # If repository name is specified, use repository-specific directory
            if repo_name:
                reports_dir = self.config.get_reports_dir(repo_name)
            else:
                reports_dir = self.config.REPORTS_DIR

            # Create reports directory if it doesn't exist
            os.makedirs(reports_dir, exist_ok=True)

            filepath = os.path.join(
                reports_dir,
                f"{filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            )
            # Secure file creation with 0o600 permissions
            fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                # Ensure permissions are correct even if file existed
                os.chmod(filepath, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            log_action(f"Report saved to {filepath}")
        except Exception as e:
            logger.error(f"Error saving report {filename}: {e}")

    def _create_pr_analysis_prompt(
        self,
        repo_name: str,
        pr_data: Dict[str, Any],
        pr_diff: str = "",
    ) -> str:
        """Compatibility wrapper used in tests to expose the PR prompt builder."""
        return _engine_pr_prompt(repo_name, pr_data, pr_diff, self.config)

    def _get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Get PR diff for analysis."""
        return _pr_get_diff(repo_name, pr_number, self.config)

    def _take_issue_actions(
        self,
        repo_name: str,
        issue_data: Dict[str, Any],
        backend_manager: Optional[Any] = None,
    ) -> List[str]:
        """Take actions on an issue using direct LLM CLI analysis and implementation."""
        from .issue_processor import _take_issue_actions as _take_issue_actions_func

        return _take_issue_actions_func(
            repo_name,
            issue_data,
            self.config,
            self.github,
            backend_manager=backend_manager,
            implementation_slots=self._get_implementation_slots(repo_name),
        )

    def _apply_issue_actions_directly(self, repo_name: str, issue_data: Dict[str, Any]) -> List[str]:
        """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
        from .issue_processor import _apply_issue_actions_directly as _apply_issue_actions_directly_func

        return _apply_issue_actions_directly_func(
            repo_name,
            issue_data,
            self.config,
            self.github,
        )

    def _commit_changes(self, fix_suggestion: Dict[str, Any]) -> str:
        """Commit changes made by the automation."""
        try:
            # Use git_commit_with_retry for centralized commit logic
            commit_message = f"Auto-Coder: {fix_suggestion.get('summary', 'Fix applied')}"
            commit_result = git_commit_with_retry(commit_message)

            if commit_result.success:
                return f"Committed changes: {commit_message}"
            else:
                return f"Failed to commit changes: {commit_result.stderr}"
        except Exception as e:
            return f"Error committing changes: {e}"

    # Additional methods needed by tests
    def _resolve_pr_merge_conflicts(self, repo_name: str, pr_number: int) -> bool:
        """Resolve merge conflicts for a PR."""
        try:
            # Get PR details to determine the base branch
            # repo = self.github.get_repository(repo_name)
            # pr = repo.get_pull(pr_number)
            pr = self.github.get_pull_request(repo_name, pr_number)
            pr_data = self.github.get_pr_details(pr)
            base_branch = pr_data.get("base_branch", "main")

            # Clean up any existing conflicts
            self.cmd.run_command(["git", "reset", "--hard", "HEAD"])
            self.cmd.run_command(["git", "clean", "-fd"])
            self.cmd.run_command(["git", "merge", "--abort"])

            # Checkout the PR branch
            # Checkout the PR branch
            # Use direct git commands instead of gh pr checkout
            # Fetch the PR head to a local branch named pr-<number>
            fetch_ref = f"pull/{pr_number}/head:pr-{pr_number}"
            self.cmd.run_command(["git", "fetch", "origin", fetch_ref])
            self.cmd.run_command(["git", "checkout", f"pr-{pr_number}"])

            # If base branch is not main, fetch and merge it
            if base_branch != "main":
                self.cmd.run_command(["git", "fetch", "origin", base_branch])
                # Resolve base to a fully qualified remote ref to avoid ambiguity
                origin_ref = f"refs/remotes/origin/{base_branch}"
                base_check = self.cmd.run_command(["git", "rev-parse", "--verify", origin_ref])
                resolved_base = origin_ref if base_check.success else base_branch
                self.cmd.run_command(["git", "merge", resolved_base])

            # Push the resolved conflicts
            self.cmd.run_command(["git", "push"])

            return True
        except Exception as e:
            logger.error(f"Failed to resolve merge conflicts for PR #{pr_number}: {e}")
            return False

    def _update_with_base_branch(self, repo_name: str, pr_data: Dict[str, Any]) -> List[str]:
        """Update PR branch with latest changes from base branch."""
        import subprocess

        actions = []

        try:
            # Get the base branch from PR data, default to 'main'
            base_branch = pr_data.get("base_branch", "main")
            pr_number = pr_data.get("number", 999)

            # Fetch the latest changes from origin
            fetch_result = subprocess.run(["git", "fetch", "origin"], capture_output=True, text=True)
            if fetch_result.returncode != 0:
                return [f"Failed to fetch from origin: {fetch_result.stderr}"]

            # Check how many commits behind the base branch we are
            rev_list_result = subprocess.run(
                [
                    "git",
                    "rev-list",
                    "--count",
                    f"HEAD..refs/remotes/origin/{base_branch}",
                ],
                capture_output=True,
                text=True,
            )

            if rev_list_result.returncode == 0:
                commits_behind = int(rev_list_result.stdout.strip())
                if commits_behind > 0:
                    actions.append(f"{commits_behind} commits behind {base_branch}")

                    # Merge the base branch
                    merge_result = subprocess.run(
                        [
                            "git",
                            "merge",
                            f"refs/remotes/origin/{base_branch}",
                            "--no-edit",
                        ],
                        capture_output=True,
                        text=True,
                    )

                    if merge_result.returncode == 0:
                        actions.append(f"Successfully merged {base_branch} branch into PR #{pr_number}")

                        # Push the updated branch
                        push_result = subprocess.run(["git", "push"], capture_output=True, text=True)
                        if push_result.returncode == 0:
                            actions.append("Pushed updated branch")
                            actions.append(self.FLAG_SKIP_ANALYSIS)
                        else:
                            actions.append(f"Failed to push: {push_result.stderr}")
                    else:
                        actions.append(f"Failed to merge {base_branch}: {merge_result.stderr}")
                else:
                    actions.append(f"PR #{pr_number} is up to date with {base_branch} branch")
            else:
                actions.append(f"Could not determine commit status: {rev_list_result.stderr}")

        except Exception as e:
            actions.append(f"Error updating with base branch: {e}")

        return actions

    def _get_repository_context(self, repo_name: str) -> Dict[str, Any]:
        """Get repository context information."""
        try:
            repo = self.github.get_repository(repo_name)
            return {
                "name": repo.name,
                "description": repo.description or "",
                "language": repo.language or "",
                "stars": repo.stargazers_count,
                "forks": repo.forks_count,
            }
        except Exception as e:
            logger.error(f"Failed to get repository context for {repo_name}: {e}")
            # Return minimal fallback data
            return {
                "name": repo_name.split("/")[-1] if "/" in repo_name else repo_name,
                "description": "Unable to fetch description",
                "language": "Unknown",
                "stars": 0,
                "forks": 0,
            }

    def _format_feature_issue_body(self, suggestion: Dict[str, Any]) -> str:
        """Format feature suggestion as issue body."""
        body = "## Feature Request\n\n"
        body += f"**Description:**\n{suggestion.get('description', 'No description provided')}\n\n"
        body += f"**Rationale:**\n{suggestion.get('rationale', 'No rationale provided')}\n\n"
        body += f"**Priority:** {suggestion.get('priority', 'medium')}\n\n"

        # Add acceptance criteria if present
        acceptance_criteria = suggestion.get("acceptance_criteria", [])
        if acceptance_criteria:
            body += "**Acceptance Criteria:**\n"
            for criteria in acceptance_criteria:
                body += f"- [ ] {criteria}\n"
            body += "\n"

        body += "*This feature request was generated automatically by Auto-Coder.*"
        return body

    def _should_auto_merge_pr(self, analysis: Dict[str, Any], pr_data: Dict[str, Any]) -> bool:
        """Determine if PR should be auto-merged."""
        return analysis.get("risk_level") == "low" and not pr_data.get("draft", False)

    def _run_pr_tests(self, repo_name: str, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run tests for PR."""
        test_script_path = self.config.TEST_SCRIPT_PATH

        if not os.path.exists(test_script_path):
            return {
                "success": False,
                "errors": f"Test script not found: {test_script_path}",
                "return_code": -1,
            }

        try:
            target_container = get_target_container(self.config)
            if target_container:
                logger.info(f"Running PR tests via target container: {target_container}")
                cmd_list = ["docker", "exec", target_container, "bash", test_script_path]
            else:
                logger.info(f"Running PR tests via script: {test_script_path}")
                cmd_list = ["bash", test_script_path]

            result = self.cmd.run_command(
                cmd_list,
                timeout=self.cmd.DEFAULT_TIMEOUTS["test"],
            )

            if result.success:
                return {"success": True, "output": result.stdout}
            else:
                return {
                    "success": False,
                    "output": result.stdout,
                    "errors": result.stderr,
                    "return_code": result.returncode,
                }
        except Exception as e:
            return {
                "success": False,
                "errors": f"Failed to execute tests: {e}",
                "return_code": -1,
            }

    def _extract_important_errors(self, test_result: Union[TestResult, Dict[str, Any]]) -> str:
        """Extract important errors using the structured TestResult flow when available.

        Falls back to the legacy regex-based extraction when conversion fails.
        """
        try:
            # Prefer structured extractor from fix_to_pass_tests_runner
            if isinstance(test_result, TestResult):
                return cast(
                    str,
                    extract_important_errors(test_result),
                )
            # Convert legacy dict payloads to TestResult for better extraction
            tr = fix_to_pass_tests_runner_module._to_test_result(test_result)
            return cast(str, extract_important_errors(tr))
        except Exception:
            # Legacy fallback: minimal regex-based extraction from dict payloads
            import re

            important_lines: List[str] = []
            output = ""
            errors_field = ""
            try:
                if isinstance(test_result, dict):
                    output = str(test_result.get("output", ""))
                    errors_field = str(test_result.get("errors", ""))
            except Exception:
                pass

            if output:
                error_patterns = [
                    r"ERROR:.*",
                    r"FAILED:.*",
                    r"Failures?:.*",
                    r"Error.*",
                    r"Exception.*",
                    r"Traceback.*",
                ]
                for line in output.split("\n"):
                    line = line.strip()
                    if any(re.search(pattern, line, re.IGNORECASE) for pattern in error_patterns):
                        if line and line not in important_lines:
                            important_lines.append(line)

            if errors_field and errors_field not in important_lines:
                important_lines.append(errors_field)

            return "\n".join(important_lines)

    def _apply_github_actions_fix(
        self,
        repo_name: str,
        pr_data: Dict[str, Any],
        test_result: TestResult,
        github_logs: Optional[str] = None,
    ) -> List[str]:
        """Apply GitHub Actions fix using structured TestResult context.

        - Accepts TestResult to enable richer, framework-aware error extraction
        - Passes structured metadata to the LLM prompt for targeted fixes
        """
        actions: List[str] = []

        try:
            # Derive a concise error summary using the structured extractor
            error_summary = cast(
                str,
                fix_to_pass_tests_runner_module.extract_important_errors(test_result),
            )
            if not github_logs:
                github_logs = error_summary

            # Prepare enhanced prompt with structured context
            linked_issues_context = get_linked_issues_context(self.github, repo_name, pr_data.get("body", ""))

            prompt = render_prompt(
                "pr.github_actions_fix_direct",
                linked_issues_context=linked_issues_context,
                data={
                    "repo_name": repo_name,
                    "pr_title": pr_data.get("title", "N/A"),
                    "pr_body": pr_data.get("body", "N/A"),
                    "pr_number": pr_data.get("number", "N/A"),
                    "github_logs": (github_logs or ""),
                    # Structured enhancements
                    "structured_errors": test_result.extraction_context or {},
                    "framework_type": test_result.framework_type or "unknown",
                },
            )

            llm_response = run_llm_prompt(prompt)
            preview = (llm_response or "").strip()[:256]
            actions.append(f"Applied GitHub Actions fix{': ' + preview + '...' if preview else ''}")

            # Commit the changes using the centralized commit logic
            commit_result = git_commit_with_retry(f"Auto-Coder: Fix GitHub Actions issues for PR #{pr_data.get('number', 'N/A')}")
            if commit_result.success:
                actions.append("Committed changes")

                # Push the changes
                push_result = git_push()
                if push_result.success:
                    actions.append("Pushed changes")
                else:
                    actions.append(f"Failed to push: {push_result.stderr}")
            else:
                actions.append(f"Failed to commit: {commit_result.stderr}")

        except Exception as e:
            actions.append(f"Error applying GitHub Actions fix: {e}")

        return actions

    def _format_direct_fix_comment(self, pr_data: Dict[str, Any], github_logs: str, fix_actions: List[str]) -> str:
        """Format direct fix comment."""
        return f"Auto-Coder Applied GitHub Actions Fixes\n\n**PR:** #{pr_data['number']} - {pr_data['title']}\n\nError: {github_logs}\n\nFixes applied: {', '.join(fix_actions)}"

    def parse_commit_history_with_actions(self, repo_name: str, search_depth: int = 10) -> List[Dict[str, Any]]:
        """Parse git commit history and identify commits that triggered GitHub Actions.

        Args:
            repo_name: Repository name in format 'owner/repo'
            search_depth: Number of recent commits to check (default: 10)

        Returns:
            List of commits that have GitHub Actions runs with status information.
            Each dict contains: commit_hash, message, actions_status, actions_url
        """
        import subprocess

        try:
            # Use git log --oneline to retrieve recent commit history
            result = subprocess.run(
                ["git", "log", "--oneline", f"-{search_depth}"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.error(f"Failed to get git log: {result.stderr}")
                return []

            commits_with_actions = []

            # Parse the output to extract commit hashes and messages
            lines = result.stdout.strip().split("\n")

            for line in lines:
                if not line.strip():
                    continue

                # Parse commit hash and message (format: "hash message")
                # Strip leading/trailing whitespace from line first
                line = line.strip()
                parts = line.split(" ", 1)
                if len(parts) < 2:
                    continue

                commit_hash = parts[0]
                commit_message = parts[1]

                # Skip lines with empty commit hash (e.g., malformed lines)
                if not commit_hash:
                    continue

                # Check if this commit has associated GitHub Actions runs
                try:
                    # Use GhApi to list workflow runs for this commit
                    token = self.github.token
                    api = get_ghapi_client(token)
                    owner, repo = repo_name.split("/")

                    runs_resp = api.actions.list_workflow_runs_for_repo(owner, repo, head_sha=commit_hash, per_page=1)
                    runs = runs_resp.get("workflow_runs", [])

                    if not runs:
                        logger.debug(f"Commit {commit_hash[:8]}: No GitHub Actions runs found")
                        continue

                    # Check if there are any runs (success or failure)
                    actions_status = None
                    actions_url = ""

                    for run in runs:
                        status = (run.get("conclusion") or run.get("status") or "").lower()
                        # Only include relevant statuses
                        if status in [
                            "success",
                            "completed",
                            "failure",
                            "failed",
                            "cancelled",
                            "pass",
                            "timed_out",
                        ]:
                            actions_status = status
                            actions_url = run.get("html_url", "")
                            break

                    # Only add commits that have completed Action runs
                    if actions_status:
                        commits_with_actions.append(
                            {
                                "commit_hash": commit_hash,
                                "message": commit_message,
                                "actions_status": actions_status,
                                "actions_url": actions_url,
                            }
                        )
                        logger.info(f"Commit {commit_hash[:8]}: Found Actions run with status '{actions_status}'")

                except Exception as e:
                    logger.warning(f"Error checking Actions for commit {commit_hash[:8]}: {e}")
                    continue

            logger.info(f"Found {len(commits_with_actions)} commits with GitHub Actions")
            return commits_with_actions

        except Exception as e:
            logger.error(f"Error parsing commit history: {e}")
            return []

    def _create_candidate_from_single(self, repo_name: str, target_type: str, number: int, propagate_errors: bool = False) -> Optional[Candidate]:
        """Create a Candidate from a single issue or PR.

        Args:
            repo_name: Repository name
            target_type: Type of target ('issue' or 'pr')
            number: Issue or PR number

        Returns:
            Candidate or None if failed
        """
        from .issue_context import extract_linked_issues_from_pr_body

        try:
            # Handle 'auto' type
            if target_type == "auto":
                # Prefer PR to avoid mislabeling PR issues.
                # get_pull_request() returns an empty result instead of raising when the
                # number belongs to an issue, so the number has to be verified.
                target_type = "issue"
                try:
                    pr = self.github.get_pull_request(repo_name, number)
                    pr_data = self.github.get_pr_details(pr) if pr else None
                    if pr_data and pr_data.get("number"):
                        target_type = "pr"
                except Exception:
                    pass

            if target_type == "pr":
                # Get PR data
                # repo = self.github.get_repository(repo_name)
                # pr = repo.get_pull(number)
                if propagate_errors:
                    try:
                        # Invalidation completion needs a cache-bypassing API
                        # result that distinguishes authoritative 404 from a
                        # transport failure. get_pull_request() intentionally
                        # flattens both to None for legacy polling callers.
                        pr = self.github.get_pull_request_metadata_strict(repo_name, number)
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 404:
                            logger.info(f"PR #{number} no longer exists in {repo_name}")
                            return None
                        raise
                else:
                    pr = self.github.get_pull_request(repo_name, number)
                pr_data = self.github.get_pr_details(pr) if pr else None
                if not pr_data or not pr_data.get("number"):
                    logger.error(f"PR #{number} not found in {repo_name}")
                    return None
                if not self._is_pr_author_allowed(pr_data):
                    logger.info(f"Skipping PR #{number} - author not in PR allowlist")
                    return None
                branch_name = pr_data.get("head_branch")
                pr_body = pr_data.get("body", "")
                related_issues = []
                if pr_body:
                    related_issues = extract_linked_issues_from_pr_body(pr_body)

                return Candidate(
                    type="pr",
                    data=pr_data,
                    priority=0,  # Single processing doesn't need priority
                    branch_name=branch_name,
                    related_issues=related_issues,
                )
            elif target_type == "issue":
                if propagate_errors:
                    try:
                        # This one strict snapshot is both the type authority and
                        # the candidate source. A second, failure-flattening read
                        # could otherwise turn a transport outage into absence.
                        issue = self.github.get_issue_dispatch_snapshot_strict(repo_name, number)
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 404:
                            logger.info(f"Issue #{number} no longer exists in {repo_name}")
                            return None
                        raise
                    if "pull_request" in issue:
                        logger.error(f"Refusing Issue candidate for {repo_name}#{number}: GitHub identifies the target as pr")
                        return None
                else:
                    authoritative_type = self._get_authoritative_item_type(repo_name, number)
                    if authoritative_type != "issue":
                        logger.error(f"Refusing Issue candidate for {repo_name}#{number}: GitHub identifies the target as {authoritative_type}")
                        return None

                    # Get issue data
                    # repo = self.github.get_repository(repo_name)
                    # issue = repo.get_issue(number)
                    issue = self.github.get_issue(repo_name, number)
                issue_data = self.github.get_issue_details(issue)
                if not issue_data or not issue_data.get("number"):
                    return None
                if not self._is_issue_author_allowed(issue_data):
                    logger.info(f"Skipping issue #{number} - author not in issue allowlist")
                    return None

                return Candidate(
                    type="issue",
                    data=issue_data,
                    priority=0,  # Single processing doesn't need priority
                    issue_number=number,
                )
        except Exception as e:
            logger.error(f"Failed to create candidate for {target_type} #{number}: {e}")
            if propagate_errors:
                raise
            return None

        return None

    # Constants
    FLAG_SKIP_ANALYSIS = "[SKIP_LLM_ANALYSIS]"
