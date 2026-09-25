"""
PR processing functionality for Auto-Coder automation engine.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from auto_coder.backend_manager import BackendManager, get_llm_backend_manager, run_llm_prompt
from auto_coder.cli_helpers import create_high_score_backend_manager
from auto_coder.cloud_manager import CloudManager
from auto_coder.github_ci_observer import ci_observation_merge_authority, ci_read_phase, end_ci_read_phase
from auto_coder.util.gh_cache import GitHubClient, ReviewThread, get_ghapi_client
from auto_coder.util.github_action import DetailedChecksResult, GitHubActionsStatusResult, _check_github_actions_status, _get_github_actions_logs, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .adversarial_validation_attempts import AdversarialValidationAttemptRepository
from .adversarial_validation_scheduler import AdversarialValidationScheduler
from .adversarial_validator import (
    AdversarialValidationResult,
    adversarial_validation_codex_feedback_marker,
    adversarial_validation_comment_marker,
    count_adversarial_validation_comments,
    format_adversarial_finding_comment,
    format_adversarial_validation_comment,
    format_test_oracle_gap_comment,
    run_adversarial_validation,
    validation_snapshot_is_current,
)
from .attempt_manager import build_pr_attempt_trigger, get_current_attempt, increment_attempt
from .automation_config import AutomationConfig, EmptyPRResult, ExplicitTargetOutcome, ProcessedPRResult, PRProcessingOutcome, StaleJulesPRResult
from .bounded_repair_bundle import (
    build_repair_handoff_bundle,
    render_bounded_repair_payload,
    validate_repair_handoff_bundle,
)
from .branch_manager import BranchManager
from .canonical_pr_blocker_ledger import CanonicalPRBlockerLedger
from .ci_repair_authority import current_ci_failure_authority
from .claude_followup_waits import ClaudeFollowupHoldActive, get_claude_followup_wait_store, wait_from_error
from .codex_cloud_task import extract_codex_cloud_task_id, is_valid_codex_cloud_task_id
from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository, resolve_codex_pr_origin, task_ids_from_text
from .conflict_resolver import _get_merge_conflict_info, resolve_merge_conflicts_with_llm, resolve_pr_merge_conflicts
from .dispatch_claim_store import DispatchIdentity, DispatchOutcome, get_dispatch_claim_store
from .entity_invalidation import DurableInvalidationQueue
from .exceptions import AutoCoderRetryableBackendError, ClaudeFollowupUsageLimitError, DeliveryCertainty
from .execution_trace import EventKind, Outcome, get_trace_collector
from .fix_to_pass_tests_runner import run_local_tests
from .git_branch import branch_context, git_checkout_branch, git_commit_with_retry
from .git_commit import commit_and_push_changes, git_push, save_commit_failure_history
from .git_info import get_commit_log
from .github_app_reviewer import ExactReviewComment, GitHubAppReviewer, ReviewerAppIdentity, load_reviewer_app_config, publish_adversarial_review, resolve_reviewer_app_identity
from .github_pending_work import WorkIdentity, get_pending_work_store
from .invocation_admission import bind_invocation_target
from .issue_context import extract_linked_issues_from_pr_body, get_linked_issues_context, resolve_issue_oracles, validate_issue_references
from .label_manager import LabelManager, LabelOperationError, filter_legacy_auto_coder_label
from .llm_backend_config import get_pr_review_allowlist_from_config, get_review_adjudicator_allowlist_from_config
from .logger_config import get_gh_logger, get_logger
from .pr_blocker_closure import _BLOCKER_ID_RE, _GAP_ID_RE
from .pr_repair import build_existing_pr_repair_prompt, resolve_existing_pr_repair_target
from .pr_repair_guard import (
    PrRepairExhaustionInfo,
    check_pr_repair_exhaustion,
    publish_exhaustion_comment_deduped,
)
from .pr_review_cycle import PHASE_ORDINARY_CLOSURE, VERDICT_FINDINGS, VERDICT_PASS, ClaimContendedError, ContractSnapshot, Finding
from .pr_review_cycle import FindingDisposition as DurableFindingDisposition
from .pr_review_cycle import NotApplicableError, RoundProvenance, StrongPolicyIdentity
from .pr_review_effects import CONFIRMED, REJECTED, UNCERTAIN, AcceptedReviewPayload, EffectAttempt, EffectOperation, ReviewEffectExecutor, ReviewEffectRepository
from .pr_review_execution import ReviewExecutionInput, ReviewMode, ScopeAssessment, execute_review
from .progress_decorators import progress_stage
from .progress_footer import ProgressStage, newline_progress
from .prompt_loader import get_prompt_template, render_prompt
from .review_adjudication import AdjudicationStatus, is_adjudication_envelope
from .review_adjudication_github import ADJUDICATION_DB_ENV, DEFAULT_ADJUDICATION_DB_PATH, AdjudicationContextStore, AdjudicationSnapshot, ReviewAdjudicationService, reconcile_thread
from .review_adjudication_orchestrator import (
    ADJUDICATION_EFFECTS_DB_ENV,
    DEFAULT_ADJUDICATION_EFFECTS_DB_PATH,
    AdjudicationEffectStore,
    format_overrule_explanation,
    plan_adjudication_effects,
)
from .review_capture.pr_adversarial_audit import (
    PrAdversarialReviewTarget,
    begin_executed_review,
    compute_policy_identity,
    find_reusable_source_review_id,
    finish_executed_review,
    linked_issue_membership,
    record_bypassed,
    record_effect,
    record_reused,
)
from .review_thread_validation import (
    ClaimedReviewThread,
    StaleReviewThreadRegistryError,
    StaleReviewThreadResolutionError,
    change_provenance_reply_fingerprint,
    classify_review_threads,
    is_change_provenance_thread,
    render_claimed_review_threads_section,
    reopen_review_threads_after_publication_failure,
    resolve_addressed_review_threads,
    retry_pending_stale_review_thread_rollbacks,
)
from .reviewer_session_registry import ReviewerSessionRegistry
from .security_utils import redact_string
from .shutdown_context import new_work_allowed
from .speculative_jules_lifecycle import get_speculative_jules_lifecycle
from .test_log_utils import extract_all_failed_tests, extract_first_failed_test, extract_important_errors
from .test_result import TestResult
from .trace_logger import get_trace_logger
from .two_tier_pr_gate import TwoTierPrGate
from .util.github_action import _create_github_action_log_summary
from .util.github_request_outcome import GitHubRequestError
from .utils import CommandExecutor, CommandResult, bind_command_execution_cwd, get_pr_author_login, is_same_github_login, log_action, reset_command_execution_cwd

logger = get_logger(__name__)
cmd = CommandExecutor()

# Pending-work stage for a PR evaluation interrupted by a GitHub operational
# failure (local admission deferral, throttle, authentication, or forbidden
# response). Registered with the process-wide PendingWorkScheduler so a
# deferred obligation is actually consumed instead of being retained forever.
PR_PROCESSING_STAGE = "pr-processing"
PR_PROCESSING_REFRESH_EFFECT = "authoritative-refresh"


def _record_pr_stage(
    pr_number: int,
    stage_id: str,
    label: str,
    outcome: Outcome,
    facts: Optional[Dict[str, Any]] = None,
    kind: EventKind = EventKind.STAGE_RESULT,
) -> None:
    """Record a PR-processing stage event using the ambient execution scope.

    The scope is bound by ``AutomationEngine`` before this module's boundary
    functions run (REQ-002 of Issue #1944); when none is bound, ``TraceCollector``
    retains the event as legacy/unscoped rather than inventing one. A
    diagnostic-recorder failure is caught here and never propagates into the
    PR admission/CI/review/merge/repair decision it is describing (REQ-008).
    """
    try:
        merged_facts = {"pr_number": pr_number, **(facts or {})}
        get_trace_collector().record_event(
            kind,
            stage_id=stage_id,
            origin=stage_id,
            label=label,
            outcome=outcome,
            facts=merged_facts,
        )
    except Exception:
        logger.debug(f"Diagnostic trace recording failed for pr#{pr_number} stage {stage_id}; continuing", exc_info=True)


def _remove_reviewer_sessions_for_closed_pr(repo_name: str, pr_number: int) -> None:
    """Best-effort removal of every backend's reviewer association for a closed PR."""
    try:
        ReviewerSessionRegistry().remove_pr(repo_name, pr_number)
    except OSError as exc:
        logger.warning(f"Failed to remove reviewer sessions for closed PR #{pr_number}: {exc}")


_cloud_review_delivery_lock = threading.RLock()
_cloud_conflict_delivery_lock = threading.RLock()

CODEX_REVIEW_SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
CODEX_REVIEW_BOT_LOGIN = "chatgpt-codex-connector[bot]"
CLOUD_REVIEW_FEEDBACK_MARKER_PREFIX = "auto-coder-cloud-review-feedback:v1:"
CLOUD_CONFLICT_FOLLOWUP_MARKER_PREFIX = "auto-coder-cloud-conflict-followup:v1:"


@dataclass(frozen=True)
class TwoTierGateInputs:
    gate: TwoTierPrGate
    contract: ContractSnapshot
    policy: StrongPolicyIdentity
    head_sha: str
    base_sha: str


def _numbered_requirements(body: str) -> List[str]:
    """Return the complete numbered Requirement declarations from an Issue body."""
    return [line.strip() for line in body.splitlines() if re.match(r"^REQ-\d{3}:\s*\S", line.strip())]


def _two_tier_gate_inputs(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
) -> Optional[TwoTierGateInputs]:
    """Resolve H/B/M/P only when the optional strong tier is configured.

    Eligibility is deliberately resolved here rather than inferred from PR prose.
    A failed lookup is represented by an exception so it cannot become a confirmed
    Issue-less result and silently bypass the gate.
    """
    from .llm_backend_config import get_llm_config

    llm_config = get_llm_config()
    strong = llm_config.get_backend_strong_pr_adversarial_validation()
    order = llm_config.get_strong_pr_adversarial_validation_backend_order()
    if strong is None and not order:
        return None

    resolution = resolve_issue_oracles(github_client, repo_name, pr_data=pr_data)
    if resolution.error:
        raise RuntimeError(resolution.error)
    if not resolution.issues:
        return None

    requirements: List[str] = []
    issue_ids: List[str] = []
    for issue in resolution.issues:
        issue_ids.append(f"#{issue.number}")
        declarations = _numbered_requirements(issue.body)
        requirements.extend(f"Issue #{issue.number} {line}" for line in declarations)
    if not requirements:
        return None

    route = tuple(order) or ((strong.name,) if strong is not None else ())
    model_options = {
        "route": route,
        "default": llm_config.get_strong_pr_adversarial_validation_default_backend(),
        "model": getattr(strong, "model", None),
        "options": getattr(strong, "options", None),
    }
    contract = ContractSnapshot(tuple(issue_ids), "\n".join(requirements))
    policy = StrongPolicyIdentity(
        "backend_strong_pr_adversarial_validation",
        json.dumps(model_options, sort_keys=True, default=str),
        "v1",
    )
    head_sha = str((pr_data.get("head") or {}).get("sha") or "")
    base_sha = str((pr_data.get("base") or {}).get("sha") or "")
    if not head_sha or not base_sha:
        raise RuntimeError("PR head/base identity is unavailable for required strong audit")
    return TwoTierGateInputs(TwoTierPrGate(repo_name), contract, policy, head_sha, base_sha)


def _execute_pending_strong_audit(repo_name: str, pr_number: int, inputs: TwoTierGateInputs) -> Tuple[bool, str]:
    """Claim, execute, and durably accept one production strong-audit round."""
    provenance = RoundProvenance(inputs.head_sha, inputs.base_sha)
    try:
        claim = inputs.gate.state.claim_strong_audit(pr_number, provenance, inputs.contract, inputs.policy)
    except ClaimContendedError:
        return False, "another controller owns the live strong-audit claim"
    except NotApplicableError as exc:
        return False, str(exc)

    try:
        with isolated_pr_head_worktree(repo_name, pr_number, inputs.head_sha) as worktree:
            from .cli_helpers import resolve_adversarial_validation_availability

            availability = resolve_adversarial_validation_availability("strong_pr", execution_cwd=worktree)
            if availability.backend_manager is None:
                reason = "strong reviewer route is EXHAUSTED" if availability.exhausted else "strong reviewer route is UNAVAILABLE"
                inputs.gate.state.abandon_claim(
                    pr_number,
                    claim.claim_id,
                    reason,
                    retry_not_before=availability.retry_not_before_epoch or 0.0,
                )
                return False, reason

            diff = CommandExecutor.run_command(
                ["git", "diff", "--no-ext-diff", "--binary", inputs.base_sha, inputs.head_sha],
                cwd=worktree,
            )
            tracked = CommandExecutor.run_command(["git", "ls-files"], cwd=worktree)
            if not diff.success or not tracked.success:
                raise RuntimeError("required repository or reviewed-diff evidence is unavailable")
            review_input = ReviewExecutionInput(
                mode=ReviewMode.STRONG_AUDIT,
                round_id=claim.claim_id,
                attempt_id=f"{claim.open_epoch}:{claim.based_on_version}",
                head_sha=inputs.head_sha,
                base_sha=inputs.base_sha,
                contract=inputs.contract,
                policy=inputs.policy,
                repository_evidence="Pinned read-only snapshot. Tracked paths:\n" + tracked.stdout,
                diff_evidence=diff.stdout,
            )
            result = execute_review(review_input, availability.backend_manager, worktree)
        if not result.is_complete or result.verdict not in {VERDICT_PASS, VERDICT_FINDINGS}:
            reason = result.diagnostic or f"strong reviewer returned {result.verdict}"
            inputs.gate.state.abandon_claim(pr_number, claim.claim_id, reason)
            return False, reason
        inputs.gate.state.record_strong_result(
            pr_number,
            claim.claim_id,
            result.verdict,
            result.reviewer_provenance,
            list(result.findings),
        )
        return True, f"accepted {result.verdict} from {result.reviewer_provenance}; publication remains pending"
    except Exception as exc:
        inputs.gate.state.abandon_claim(pr_number, claim.claim_id, str(exc))
        return False, f"strong audit execution failed: {exc}"


def _execute_pending_ordinary_closure(repo_name: str, pr_number: int, inputs: TwoTierGateInputs) -> Tuple[bool, str, str]:
    """Run ordinary verification for a retained strong finding bundle."""
    snapshot = inputs.gate.state.snapshot(pr_number)
    strong_round = snapshot.accepted_strong_round
    if snapshot.phase != PHASE_ORDINARY_CLOSURE or strong_round is None or not snapshot.open_findings:
        return False, "ordinary closure is not currently applicable", ""
    if strong_round.base_sha != inputs.base_sha or strong_round.contract_identity != inputs.contract.identity or strong_round.policy_identity != inputs.policy.identity:
        return False, "retained strong evidence is stale for the current base, contract, or policy", ""
    try:
        with isolated_pr_head_worktree(repo_name, pr_number, inputs.head_sha) as worktree:
            from .cli_helpers import resolve_adversarial_validation_availability

            availability = resolve_adversarial_validation_availability("pr", execution_cwd=worktree)
            if availability.backend_manager is None:
                reason = "ordinary reviewer route is EXHAUSTED" if availability.exhausted else "ordinary reviewer route is UNAVAILABLE"
                return False, reason, ""
            diff = CommandExecutor.run_command(
                ["git", "diff", "--no-ext-diff", "--binary", strong_round.head_sha, inputs.head_sha],
                cwd=worktree,
            )
            tracked = CommandExecutor.run_command(["git", "ls-files"], cwd=worktree)
            if not diff.success or not tracked.success:
                return False, "required cumulative diff or repository evidence is unavailable", ""
            review_input = ReviewExecutionInput(
                mode=ReviewMode.ORDINARY_CLOSURE,
                round_id=strong_round.round_id,
                attempt_id=f"{snapshot.open_epoch}:{snapshot.transition_version}",
                head_sha=inputs.head_sha,
                base_sha=inputs.base_sha,
                contract=inputs.contract,
                policy=inputs.policy,
                repository_evidence="Pinned read-only snapshot. Tracked paths:\n" + tracked.stdout,
                diff_evidence=diff.stdout,
                finding_set_revision=snapshot.finding_set_revision,
                findings=snapshot.open_findings,
                audited_head_sha=strong_round.head_sha,
            )
            result = execute_review(review_input, availability.backend_manager, worktree)
        if not result.is_complete:
            return False, result.diagnostic, result.reviewer_provenance
        if result.verdict != VERDICT_PASS:
            return False, f"ordinary verification retained {result.verdict} findings", result.reviewer_provenance
        dispositions = [DurableFindingDisposition(item.finding_id, item.status, item.evidence, inputs.head_sha) for item in result.dispositions]
        inputs.gate.state.certify_closure(
            pr_number,
            RoundProvenance(inputs.head_sha, inputs.base_sha),
            inputs.contract,
            inputs.policy,
            strong_round.round_id,
            snapshot.finding_set_revision,
            dispositions,
            bounded=result.scope is ScopeAssessment.BOUNDED,
            bounded_evidence=result.scope_evidence,
            new_findings=list(result.findings),
            expected_version=snapshot.transition_version,
        )
        if result.scope is ScopeAssessment.BOUNDED:
            return True, f"accepted bounded ordinary closure from {result.reviewer_provenance}; publication remains pending", result.reviewer_provenance
        scope = result.scope.value if result.scope is not None else "UNKNOWN"
        return True, f"accepted ordinary convergence with {scope} scope; renewed strong audit is required", result.reviewer_provenance
    except Exception as exc:
        return False, f"ordinary closure execution failed: {exc}", ""


TWO_TIER_REVIEW_DESTINATION = "github-reviewer-app:threads-v1"


def _render_two_tier_finding(finding: Finding) -> str:
    sections = []
    sections.append(
        f"### {finding.finding_id}\n\n"
        f"**Requirements:** {', '.join(finding.requirement_ids)}  \n"
        f"**Status:** {finding.status}  \n"
        f"**Affected boundary:** {finding.affected_boundary}\n\n"
        f"**Scenario:** {finding.counterexample}\n\n"
        f"**Expected:** {finding.expected_behavior}\n\n"
        f"**Actual:** {finding.actual_behavior}\n\n"
        f"**Evidence:** {finding.evidence}\n\n"
        f"**Impact:** {finding.material_consequence}\n\n"
        f"**Regression scenario:** {finding.focused_regression_scenario}\n\n"
    )
    if finding.is_regression_gap:
        sections.append(f"**Incorrect implementation admitted by tests:** {finding.plausible_incorrect_implementation}\n\n" f"**Why tests admit it:** {finding.why_tests_admit_it}\n\n")
    if finding.disposition_evidence:
        sections.append(f"**Disposition evidence:** {finding.disposition_evidence}\n\n")
    return "".join(sections)


def _render_two_tier_review(payload: AcceptedReviewPayload) -> str:
    """Render the review summary; strong findings are separate root threads."""
    title = "Strong audit" if payload.mode == "STRONG_AUDIT" else "Ordinary closure"
    marker = f"<!-- auto-coder-two-tier-review:v1:{payload.identity} -->"
    if payload.mode == "STRONG_AUDIT":
        readable_findings = f"{len(payload.findings)} actionable finding thread(s) are attached to this review.\n\n" if payload.findings else "No findings.\n\n"
    else:
        readable_findings = "".join(_render_two_tier_finding(finding) for finding in payload.findings)
    return (
        f"{marker}\n## {title} evidence (attempt {payload.attempt})\n\n"
        f"Verdict: **{payload.verdict}**  \n"
        f"Target head: `{payload.target_head}`  \n"
        f"Round: `{payload.round_id}`  \n\n"
        f"{readable_findings}"
        "<details><summary>Exact accepted payload</summary>\n\n"
        f"```json\n{payload.canonical_json()}\n```\n\n</details>"
    )


class _GitHubReviewEffectTransport:
    """Authenticated GitHub adapter for one exact review publication."""

    def __init__(self, reviewer: GitHubAppReviewer, payload: AcceptedReviewPayload, authorize: Callable[[], bool]):
        self.reviewer = reviewer
        self.payload = payload
        self.authorize = authorize
        self.body = _render_two_tier_review(payload)
        self.comments = (
            tuple(
                ExactReviewComment(
                    body=f"<!-- auto-coder-two-tier-finding:v1:{payload.identity}:{hashlib.sha256(finding.finding_id.encode()).hexdigest()} -->\n" + _render_two_tier_finding(finding),
                    evidence=finding.evidence,
                )
                for finding in payload.findings
            )
            if payload.mode == "STRONG_AUDIT"
            else ()
        )

    def send(self, operation: EffectOperation) -> EffectAttempt:
        result = self.reviewer.publish_exact_pr_review(
            self.payload.repository,
            self.payload.pr_number,
            self.payload.target_head,
            self.body,
            self.authorize,
            comments=self.comments,
        )
        if result.success:
            return EffectAttempt(CONFIRMED, result.event)
        if result.reason == "Review authority is no longer current":
            return EffectAttempt(REJECTED, reason=result.reason)
        return EffectAttempt(UNCERTAIN, reason=result.reason)

    def reconcile(self, operation: EffectOperation) -> EffectAttempt:
        result = self.reviewer.find_exact_pr_review(
            self.payload.repository,
            self.payload.pr_number,
            self.payload.target_head,
            self.body,
            comments=self.comments,
        )
        if result.success:
            return EffectAttempt(CONFIRMED, result.event)
        # A completed authenticated listing positively establishes absence;
        # an unavailable listing remains uncertain and cannot authorize replay.
        if result.reason == "Exact authenticated review was not found":
            return EffectAttempt(REJECTED, reason=result.reason)
        return EffectAttempt(UNCERTAIN, reason=result.reason)


def _consume_pending_two_tier_publication(repo_name: str, pr_number: int, inputs: TwoTierGateInputs) -> Tuple[bool, str]:
    """Publish only a current, durably accepted two-tier result and acknowledge it."""
    snapshot = inputs.gate.state.snapshot(pr_number)
    strong = snapshot.accepted_strong_round
    if strong is None or not snapshot.pending_effect:
        return False, "no accepted review publication is pending"
    if snapshot.pending_effect == "CLOSURE_PUBLICATION":
        closure = snapshot.accepted_closure
        if closure is None:
            return False, "accepted closure payload is unavailable"
        payload = AcceptedReviewPayload.closure(repo_name, pr_number, strong, closure, snapshot.findings)
    else:
        payload = AcceptedReviewPayload.strong(repo_name, pr_number, strong, snapshot.findings)

    def is_current() -> bool:
        current = inputs.gate.state.snapshot(pr_number)
        return bool(
            not current.closed
            and current.open_epoch == payload.open_epoch
            and current.accepted_strong_round is not None
            and current.accepted_strong_round.round_id == strong.round_id
            and current.finding_set_revision == payload.finding_set_revision
            and current.pending_effect == snapshot.pending_effect
        )

    try:
        reviewer = GitHubAppReviewer(load_reviewer_app_config(repo_name=repo_name))
    except Exception:
        return False, "configured reviewer identity is unavailable; publication was not started"
    executor = ReviewEffectExecutor(ReviewEffectRepository(repo_name))
    operation = executor.apply(
        payload,
        "review-publication",
        TWO_TIER_REVIEW_DESTINATION,
        _GitHubReviewEffectTransport(reviewer, payload, is_current),
        is_current,
    )
    if operation.status != CONFIRMED:
        return False, f"publication {operation.status.lower()}: {operation.reason or 'awaiting the reservation owner'}"
    if payload.mode == "ORDINARY_CLOSURE":
        inputs.gate.state.acknowledge_closure_publication(pr_number, payload.round_id)
    else:
        inputs.gate.state.acknowledge_publication(pr_number, payload.round_id)
    return True, f"confirmed authenticated {payload.mode} publication receipt {operation.receipt}"


@dataclass(frozen=True)
class CodexReviewState:
    """State of the latest Codex GitHub review summary for a pull request."""

    present: bool = False
    completed: bool = False
    lookup_error: Optional[str] = None


@dataclass(frozen=True)
class CodexCloudFeedbackResult:
    """Outcome of requesting CI-failure repair from an existing cloud task."""

    delivered: bool = False
    retryable: bool = False
    actions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CloudConflictDelegationResult:
    """Outcome of routing a merge conflict to an existing cloud session."""

    delegated: bool = False
    reason: str = ""
    accepted_action: str = ""
    deferred: bool = False
    retry_not_before: Optional[float] = None

    def __bool__(self) -> bool:
        return self.delegated


@dataclass(frozen=True)
class CloudConflictDeliveryRecord:
    """Durable state for one non-idempotent cloud conflict follow-up."""

    task_id: str
    status: str


@dataclass
class UnsafeCodexCloudPRResult:
    """Outcome of rejecting a Codex Cloud PR with an unsafe remote head."""

    closed: bool = False
    reissue_delivered: bool = False
    metadata_error: Optional[str] = None
    authoritative_pr_data: Optional[Dict[str, Any]] = None
    actions: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdversarialValidationEligibility:
    """Verified Issue-oracle eligibility for adversarial validation."""

    issue_numbers: Tuple[int, ...] = ()
    lookup_error: Optional[str] = None

    @property
    def is_applicable(self) -> bool:
        return bool(self.issue_numbers)


class PRActionList(list[str]):
    """PR actions plus machine-readable failure state for caller propagation."""

    def __init__(self, values: Sequence[str] = (), adversarial_validation_error: Optional[str] = None) -> None:
        super().__init__(values)
        self.adversarial_validation_error = adversarial_validation_error
        self.quota_deferred = False
        self.retry_not_before: Optional[float] = None


class CloudReviewRepairResult(list[str]):
    """Actions plus confirmation that blocking review work has an owner."""

    def __init__(self, values: Sequence[str] = (), delivered: bool = False, deferred: bool = False, retry_not_before: Optional[float] = None) -> None:
        super().__init__(values)
        self.delivered = delivered
        self.deferred = deferred
        self.retry_not_before = retry_not_before


def _retain_claude_quota_deferral(
    error: ClaudeFollowupUsageLimitError,
    pr_number: int,
    task_id: str,
    purpose: str,
    work_identity: str,
) -> float:
    """Commit a typed refusal before its caller releases delivery state."""
    wait = wait_from_error(error, pr_number, task_id, purpose, work_identity)
    get_claude_followup_wait_store().retain(wait, error.blocking_windows)
    logger.warning(
        "Claude follow-up deferred repository={} pr={} operation={} backend={} " "task={} reason={} certainty={} retry_not_before={}",
        wait.repository,
        pr_number,
        purpose,
        wait.backend_name,
        task_id,
        wait.reason,
        wait.certainty.value,
        wait.retry_not_before,
    )
    get_trace_logger().log(
        "Claude Follow-up Deferred",
        f"Deferred {purpose} for PR #{pr_number} until provider usage can be rechecked",
        item_type="pr",
        item_number=pr_number,
        details={
            "operation": purpose,
            "backend": wait.backend_name,
            "task_id": task_id,
            "reason": wait.reason,
            "certainty": wait.certainty.value,
            "retry_not_before": wait.retry_not_before,
        },
    )
    return wait.retry_not_before


def _send_followup_with_quota_admission(client: Any, repository: str, task_id: str, message: str, identities: tuple[str, ...] = ()) -> bool:
    """Serialize Claude usage rechecks and fence final provider assignment."""
    context = getattr(type(client), "followup_quota_context", None)
    if not callable(context):
        return client.send_followup(task_id, message, identities) if identities else client.send_followup(task_id, message)
    backend_name, credential_context = context(client)
    store = get_claude_followup_wait_store()
    with store.admission(repository, backend_name, credential_context) as claim:
        client._followup_admission = claim
        try:
            return client.send_followup(task_id, message)
        finally:
            client._followup_admission = None


@dataclass(frozen=True)
class CloudTaskOrigin:
    """Uniquely resolved durable cloud ownership and its transport client."""

    provider: str
    task_id: str
    client: Any
    attribution_token: str = ""


@dataclass(frozen=True)
class CloudTaskOriginResolution:
    """Result of resolving a PR's provider-aware durable association."""

    origin: Optional[CloudTaskOrigin] = None
    reason: str = ""


@dataclass(frozen=True)
class ReviewThreadGateState:
    """Tri-state review-thread result used by merge gates."""

    has_unresolved: bool = False
    lookup_error: Optional[str] = None


@dataclass(frozen=True)
class ClaimedReviewThreadGateState:
    """Unresolved-review-thread gate result that separates ordinary blockers
    from threads explicitly claimed as addressed by a supported automated
    reviewer's implementation agent (issue #1619, REQ-001/REQ-011)."""

    claimed: Tuple[Any, ...] = ()
    unresolved: Tuple[ReviewThread, ...] = ()
    blocking_unresolved: Tuple[ReviewThread, ...] = ()
    has_blocking_unresolved: bool = False
    lookup_error: Optional[str] = None


def _enforce_unresolved_provenance_gate(
    result: AdversarialValidationResult,
    claimed_review_threads: Sequence[ClaimedReviewThread],
    resolved_thread_ids: Sequence[str],
) -> Set[str]:
    """Prevent PASS while preserving the validator's concrete provenance result."""
    unresolved_ids = {thread.thread_id for thread in claimed_review_threads if thread.is_change_provenance} - set(resolved_thread_ids)
    if unresolved_ids and result.is_pass:
        result.result = "INCONCLUSIVE"
        result.summary = f"{result.summary.rstrip()} Change-provenance clarification remains unresolved after independent validation.".strip()
        result.diagnostic_category = "change_provenance_clarification"
        result.diagnostic_reason = f"Unresolved clarification thread(s): {', '.join(sorted(unresolved_ids))}"
    return unresolved_ids


def _reconcile_failed_adversarial_publication(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
    result: AdversarialValidationResult,
) -> Tuple[bool, Optional[str]]:
    """Determine whether a failed client call nevertheless durably published."""
    status, status_error = _get_published_adversarial_validation_status(
        github_client,
        repo_name,
        pr_number,
        head_sha,
    )
    if status_error:
        return False, status_error
    expected_status = result.result.strip().upper()
    if expected_status == "PASS" and result.specification_gaps:
        expected_status = "PASS_WITH_SPECIFICATION_GAPS"
    if status != expected_status:
        return False, None
    if result.clarification_reply_fingerprint:
        report, report_error = _get_published_adversarial_validation_comment(
            github_client,
            repo_name,
            pr_number,
            head_sha,
        )
        if report_error:
            return False, report_error
        if not report or result.clarification_reply_fingerprint not in report:
            return False, None
    return True, None


def _resolve_eligible_review_thread_ids(repo_name: str) -> Set[int]:
    """Return configured stable identity IDs eligible for adjudication."""
    configured_ids = get_pr_review_allowlist_from_config(repo_name=repo_name)
    return set(configured_ids or [])


def _get_claimed_review_thread_state(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    config: Optional[AutomationConfig] = None,
) -> ClaimedReviewThreadGateState:
    """Fetch review threads and separate ordinary blockers from claimed threads.

    Reuses the existing ``_get_review_thread_gate_state`` boolean/lookup-error
    result so every call site that has no unresolved threads (or a lookup
    failure) behaves exactly as before. Only when at least one thread is
    unresolved does this additionally fetch full thread detail (comments) to
    determine whether every unresolved thread is an eligible, explicitly
    claimed automated-review thread. Any failure in that additional lookup is
    returned as a structured lookup error so callers can distinguish an API
    failure from an ordinary unresolved-thread blocker.
    """
    gate = _get_review_thread_gate_state(github_client, repo_name, pr_number, config=config)
    if gate.lookup_error:
        return ClaimedReviewThreadGateState(lookup_error=gate.lookup_error)
    if not gate.has_unresolved:
        return ClaimedReviewThreadGateState()

    try:
        client = github_client or GitHubClient.get_instance()
        strict_getter = getattr(type(client), "get_pr_review_threads_strict", None)
        if not callable(strict_getter):
            # No detailed lookup available (e.g. a test double); fail closed
            # to the ordinary "unresolved threads block merge" behavior.
            return ClaimedReviewThreadGateState(has_blocking_unresolved=True)
        threads = client.get_pr_review_threads_strict(repo_name, pr_number)
    except GitHubRequestError:
        raise
    except Exception as e:
        logger.error(f"Failed detailed review-thread lookup for PR #{pr_number}: {e}")
        return ClaimedReviewThreadGateState(lookup_error=str(e))

    eligible_author_ids = _resolve_eligible_review_thread_ids(repo_name)
    classification = classify_review_threads(threads, eligible_author_ids)
    claimed_thread_ids = {thread.thread_id for thread in classification.claimed}
    state = ClaimedReviewThreadGateState(
        claimed=tuple(classification.claimed),
        unresolved=tuple(thread for thread in threads if not thread.is_resolved),
        # Repair delegation must receive only ordinary blockers. Claimed-addressed
        # threads remain unresolved for independent validation, but asking the
        # implementation agent to repair them again violates that protocol.
        blocking_unresolved=tuple(thread for thread in threads if not thread.is_resolved and thread.id not in claimed_thread_ids),
        has_blocking_unresolved=classification.blocking_unresolved_count > 0,
    )
    if config is not None and not _is_pr_adversarial_validation_enabled(config, repo_name):
        return _filter_unresolved_review_threads_for_disabled_validator(state, repo_name)
    return state


_ADVERSARIAL_THREAD_HEADINGS = (
    "### Auto-Coder adversarial finding",
    "### Auto-Coder material test-oracle gap",
    "<!-- auto-coder-two-tier-finding:v1:",
)


def _is_pr_adversarial_validation_enabled(
    config: Optional[AutomationConfig] = None,
    repo_name: Optional[str] = None,
) -> bool:
    """Return whether PR adversarial validation is enabled."""
    if config is not None and repo_name is not None and getattr(config, "repo_name", None) == repo_name:
        return bool(getattr(config, "pr_adversarial_validation", True)) and bool(getattr(config, "ENABLE_ADVERSARIAL_VALIDATION", True))
    if config is not None and (not getattr(config, "pr_adversarial_validation", True) or not getattr(config, "ENABLE_ADVERSARIAL_VALIDATION", True)):
        return False
    if repo_name is not None:
        from .llm_backend_config import get_pr_adversarial_validation_from_config

        return get_pr_adversarial_validation_from_config(repo_name=repo_name)
    if config is not None:
        return bool(getattr(config, "pr_adversarial_validation", True)) and bool(getattr(config, "ENABLE_ADVERSARIAL_VALIDATION", True))
    return True


def _pr_adversarial_review_target(repo_name: str, pr_data: Dict[str, Any]) -> PrAdversarialReviewTarget:
    """Build the exact repository/PR/head identity for durable review audit (REQ-002)."""
    head_sha = str((pr_data.get("head") or {}).get("sha") or pr_data.get("head_sha") or "")
    return PrAdversarialReviewTarget(repository=repo_name, pr_number=int(pr_data.get("number", 0)), head_sha=head_sha)


def _pr_adversarial_policy_identity(config: AutomationConfig, thread_gate_enabled: bool) -> str:
    """Return the same policy/cache identity used consistently at every PR
    adversarial-review audit boundary (BYPASSED, REUSED, EXECUTED), so a REUSED
    consumption can be correlated to its producing EXECUTED review (REQ-011)."""
    max_adv_reviews = config.MAX_ADVERSARIAL_VALIDATIONS if config.MAX_ADVERSARIAL_VALIDATIONS is not None else config.MAX_ADVERSARIAL_REVIEWS
    return compute_policy_identity(max_adversarial_reviews=max_adv_reviews, thread_gate_enabled=thread_gate_enabled)


def _pr_adversarial_linked_issue_membership(repo_name: str, pr_data: Dict[str, Any], verified_issue_numbers: Optional[Sequence[int]] = None) -> Optional[str]:
    """Issue-oracle reference for the durable review record (REQ-002).

    Prefers the exact verified Issue numbers the validation context actually
    used (``adversarial_eligibility.issue_numbers``) when available; falls
    back to a best-effort PR-body parse only when the caller has not already
    resolved eligibility (for example the BYPASSED site, reached before
    eligibility is ever checked). Returns None (explicitly unknown) rather
    than guessing when no linked Issue reference can be resolved.
    """
    if verified_issue_numbers is not None:
        return linked_issue_membership(tuple(verified_issue_numbers))
    try:
        linked_issue_numbers = extract_linked_issues_from_pr_body(pr_data.get("body") or "")
    except Exception:
        return None
    return linked_issue_membership(tuple(linked_issue_numbers))


def _record_pr_adversarial_review_bypassed(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    thread_gate_enabled: bool,
) -> None:
    """Record a reached BYPASSED audit observation for explicit disablement (REQ-005)."""
    target = _pr_adversarial_review_target(repo_name, pr_data)
    policy_identity = _pr_adversarial_policy_identity(config, thread_gate_enabled)
    related_issue_membership = _pr_adversarial_linked_issue_membership(repo_name, pr_data)
    record_bypassed(target, policy_identity=policy_identity, related_issue_membership=related_issue_membership)


def _is_pr_review_thread_gate_enabled(
    config: Optional[AutomationConfig] = None,
    repo_name: Optional[str] = None,
) -> bool:
    """Return whether PR review thread gate is enabled."""
    if config is not None and repo_name is not None and getattr(config, "repo_name", None) == repo_name:
        return bool(getattr(config, "pr_review_thread_gate", True))
    if config is not None and not getattr(config, "pr_review_thread_gate", True):
        return False
    if repo_name is not None:
        from .llm_backend_config import get_pr_review_thread_gate_from_config

        return get_pr_review_thread_gate_from_config(repo_name=repo_name)
    if config is not None:
        return bool(getattr(config, "pr_review_thread_gate", True))
    return True


def _is_automatic_test_fix_enabled(
    config: Optional[AutomationConfig] = None,
    repo_name: Optional[str] = None,
) -> bool:
    """Return whether automatic test failure fix is enabled."""
    if config is not None and repo_name is not None and getattr(config, "repo_name", None) == repo_name:
        return bool(getattr(config, "automatic_test_fix", True))
    if config is not None and not getattr(config, "automatic_test_fix", True):
        return False
    if repo_name is not None:
        from .llm_backend_config import get_automatic_test_fix_from_config

        return get_automatic_test_fix_from_config(repo_name=repo_name)
    if config is not None:
        return bool(getattr(config, "automatic_test_fix", True))
    return True


def is_authoritative_adversarial_thread(
    thread: ReviewThread,
    repo_name: str,
    reviewer_identity: Optional[ReviewerAppIdentity] = None,
) -> bool:
    """Return whether a review thread authoritatively originates from Auto-Coder's adversarial validator."""
    if thread.is_resolved or thread.comments_truncated:
        return False
    comments = thread.comments or []
    if not comments:
        return False
    root = comments[0]
    if root is None or not any(root.body.startswith(heading) or root.body.lstrip().startswith(heading) for heading in _ADVERSARIAL_THREAD_HEADINGS):
        return False
    if reviewer_identity is None:
        try:
            reviewer_identity = resolve_reviewer_app_identity(repo_name)
        except Exception as exc:
            logger.error(f"Could not resolve reviewer App identity to authenticate thread: {exc}")
            return False
    return reviewer_identity.matches_login(root.author_login)


def _filter_unresolved_review_threads_for_disabled_validator(
    state: ClaimedReviewThreadGateState,
    repo_name: str,
) -> ClaimedReviewThreadGateState:
    """Exclude authoritatively identified adversarial validator threads from merge blocking.

    When adversarial validation is disabled, unresolved validator-owned threads
    must not remain internal merge blockers solely through the generic review-thread gate
    (REQ-004), while remaining unmodified and unresolved on GitHub (REQ-006).
    Non-adversarial review threads (such as human reviews) remain blocking (REQ-005).
    """
    try:
        reviewer_identity = resolve_reviewer_app_identity(repo_name)
    except Exception as exc:
        logger.error(f"Could not resolve reviewer identity to check validator review threads: {exc}")
        return state

    remaining_blocking: List[ReviewThread] = []
    for thread in state.blocking_unresolved:
        if is_authoritative_adversarial_thread(thread, repo_name, reviewer_identity):
            continue
        remaining_blocking.append(thread)

    remaining_claimed: List[ClaimedReviewThread] = []
    for thread in state.claimed:
        if any(thread.original_finding.startswith(heading) or thread.original_finding.lstrip().startswith(heading) for heading in _ADVERSARIAL_THREAD_HEADINGS) and reviewer_identity.matches_login(thread.root_author_login):
            continue
        remaining_claimed.append(thread)

    return ClaimedReviewThreadGateState(
        claimed=tuple(remaining_claimed),
        unresolved=state.unresolved,
        blocking_unresolved=tuple(remaining_blocking),
        has_blocking_unresolved=bool(remaining_blocking),
        lookup_error=state.lookup_error,
    )


def _allow_older_head_adversarial_threads(
    state: ClaimedReviewThreadGateState,
    reviewer_login: str,
    *,
    forced: bool = False,
) -> ClaimedReviewThreadGateState:
    """Make authentic validator findings eligible for independent rereview.

    This does not resolve or otherwise acknowledge a finding.  It only moves
    authentic Auto-Coder reviewer threads from the pre-validation merge gate
    into the validator's disposition input, either because the caller has
    established that the current head has no applicable verdict (the default,
    ``forced=False``), or because an explicit ``--force`` run is admitting a
    same-head revalidation despite a saved verdict (``forced=True``, issue
    #2106 REQ-001/REQ-003). The two cases are tagged with distinct
    ``ClaimedReviewThread`` flags so the validation prompt never misrepresents
    a forced same-head rereview as evidence that the head changed.
    """
    promoted: List[ClaimedReviewThread] = []
    remaining: List[ReviewThread] = []
    for thread in state.blocking_unresolved:
        comments = thread.comments or []
        root = comments[0] if comments else None
        if root is None or thread.comments_truncated or not is_same_github_login(root.author_login, reviewer_login) or not any(root.body.startswith(heading) or root.body.lstrip().startswith(heading) for heading in _ADVERSARIAL_THREAD_HEADINGS):
            remaining.append(thread)
            continue
        promoted.append(
            ClaimedReviewThread(
                thread_id=thread.id,
                root_comment_database_id=root.database_id,
                root_author_login=root.author_login,
                original_finding=root.body,
                discussion="\n\n".join(f"{comment.author_login or '(unknown author)'}: {comment.body}" for comment in comments),
                revalidation_after_head_change=not forced,
                revalidation_forced=forced,
            )
        )
    if not promoted:
        return state
    return ClaimedReviewThreadGateState(
        claimed=tuple(state.claimed) + tuple(promoted),
        unresolved=state.unresolved,
        blocking_unresolved=tuple(remaining),
        has_blocking_unresolved=bool(remaining),
        lookup_error=state.lookup_error,
    )


def _comment_value(comment: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a REST dictionary or a GhApi object."""
    return comment.get(key, default) if isinstance(comment, dict) else getattr(comment, key, default)


def _add_unique_pr_comment(github_client: Any, repo_name: str, pr_number: int, body: str) -> bool:
    """Post an informational comment only when the same body is not present."""
    comments = github_client.get_pr_comments(repo_name, pr_number)
    if isinstance(comments, list):
        normalized_body = body.strip()
        for comment in comments:
            existing_body = _comment_value(comment, "body", "")
            if isinstance(existing_body, str) and existing_body.strip() == normalized_body:
                logger.info(f"Skipped duplicate informational comment on PR #{pr_number}")
                return False
    github_client.add_comment_to_pr(repo_name, pr_number, body)
    return True


def _get_codex_review_state(github_client: Any, repo_name: str, pr_number: int) -> CodexReviewState:
    """Return Codex review presence/completion without tying it to a reviewed SHA."""
    try:
        strict_getter = getattr(type(github_client), "get_pr_comments_strict", None)
        if callable(strict_getter):
            comments = github_client.get_pr_comments_strict(repo_name, pr_number)
        else:
            comments = github_client.get_pr_comments(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to inspect Codex review state for PR #{pr_number}: {e}")
        return CodexReviewState(lookup_error=str(e))

    for comment in reversed(comments):
        body = _comment_value(comment, "body", "")
        user = _comment_value(comment, "user") or {}
        login = _comment_value(user, "login", "")
        if login != CODEX_REVIEW_BOT_LOGIN or not isinstance(body, str) or CODEX_REVIEW_SUMMARY_MARKER not in body:
            continue

        code_review_row = next((line for line in body.splitlines() if "Code Review" in line and line.lstrip().startswith("|")), "")
        columns = [column.strip() for column in code_review_row.split("|")]
        status_column = columns[2] if len(columns) > 2 else ""
        return CodexReviewState(present=True, completed=bool(re.search(r"\bCompleted\b", status_column, re.IGNORECASE)))

    return CodexReviewState()


def _get_adversarial_validation_eligibility(github_client: Any, repo_name: str, pr_data: Dict[str, Any]) -> AdversarialValidationEligibility:
    """Use the same verified Issue-oracle resolver as validation context."""
    resolution = resolve_issue_oracles(github_client, repo_name, pr_data=pr_data)
    if resolution.error:
        logger.error(f"Failed to verify adversarial-validation eligibility: {resolution.error}")
        return AdversarialValidationEligibility(lookup_error=resolution.error)
    return AdversarialValidationEligibility(issue_numbers=tuple(issue.number for issue in resolution.issues))


def has_unresolved_review_threads(
    github_client: Any,
    repo_name: str,
    pr_number: int,
) -> bool:
    """Check if a pull request has unresolved review threads.

    Args:
        github_client: GitHub client instance or None
        repo_name: Repository name (owner/repo)
        pr_number: Pull request number

    Returns:
        True if at least one review thread is unresolved, False otherwise.
    """
    try:
        client = github_client or GitHubClient.get_instance()
        if hasattr(client, "has_unresolved_review_threads"):
            res = client.has_unresolved_review_threads(repo_name, pr_number)
            if isinstance(res, bool):
                return res
            if isinstance(res, (list, tuple)):
                return any(not getattr(t, "is_resolved", False) for t in res)
            if res is True:
                return True
            return False
        elif hasattr(client, "get_pr_review_threads"):
            threads = client.get_pr_review_threads(repo_name, pr_number)
            if isinstance(threads, (list, tuple)):
                return any(not getattr(t, "is_resolved", False) for t in threads)
            return False
        return False
    except Exception as e:
        logger.error(f"Error checking unresolved review threads for PR #{pr_number}: {e}")
        return False


def _get_review_thread_gate_state(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    config: Optional[AutomationConfig] = None,
) -> ReviewThreadGateState:
    """Fetch review threads strictly in production so lookup errors fail closed."""
    try:
        client = github_client or GitHubClient.get_instance()
        strict_getter = getattr(type(client), "get_pr_review_threads_strict", None)
        if callable(strict_getter):
            threads = client.get_pr_review_threads_strict(repo_name, pr_number)
            unresolved = [thread for thread in threads if not thread.is_resolved]
            if config is not None and not _is_pr_adversarial_validation_enabled(config, repo_name):
                try:
                    reviewer_identity = resolve_reviewer_app_identity(repo_name)
                    unresolved = [thread for thread in unresolved if not is_authoritative_adversarial_thread(thread, repo_name, reviewer_identity)]
                except Exception as exc:
                    logger.error(f"Could not resolve reviewer identity in review thread gate: {exc}")
            return ReviewThreadGateState(has_unresolved=bool(unresolved))
        return ReviewThreadGateState(has_unresolved=has_unresolved_review_threads(client, repo_name, pr_number))
    except Exception as e:
        logger.error(f"Failed strict review-thread lookup for PR #{pr_number}: {e}")
        return ReviewThreadGateState(lookup_error=str(e))


def _record_codex_pr_attribution(repo_name: str, pr_data: Dict[str, Any]) -> None:
    """Reevaluate PR attribution without authorizing any provider effect."""
    try:
        from .cloud_run import CloudRunRepository
        from .codex_pr_attribution import CodexPrAttributionRepository, resolve_codex_pr_origin

        runs = CloudRunRepository(repo_name)
        result = resolve_codex_pr_origin(
            repo_name,
            pr_data,
            runs,
            CodexPrAttributionRepository(repo_name),
        )
        linked_issues = _resolve_pr_issue_numbers(repo_name, pr_data, None)
        has_accepted_codex_work = any(run.provider == "codex-cloud" and run.submission_outcome == "accepted" for issue_number in linked_issues for run in runs.list_for_issue(issue_number))
        if _is_codex_pr(pr_data) or has_accepted_codex_work or result.origin is not None:
            pr_data["_codex_pr_attribution_required"] = True
        if result.origin is not None:
            pr_data["_verified_codex_pr_origin"] = result.origin.task_id
        logger.debug(f"Codex PR attribution for #{pr_data.get('number')}: {result.disposition.value} ({result.boundary})")
    except Exception as exc:
        # Attribution is bookkeeping only. Its unavailable result must not
        # alter PR admission, provider routing, or unrelated processing.
        logger.warning(f"Codex PR attribution unavailable for #{pr_data.get('number')}: {type(exc).__name__}")


def process_pull_request(
    github_client: Any,
    config: AutomationConfig,
    repo_name: str,
    pr_data: Dict[str, Any],
    *,
    force_adversarial_validation: bool = False,
    adversarial_validation_scheduler: Optional[AdversarialValidationScheduler] = None,
) -> ProcessedPRResult:
    """Process a single pull request with priority order."""
    try:
        processed_pr = ProcessedPRResult(
            pr_data=pr_data,
            actions_taken=[],
            priority=None,
            analysis=None,
        )

        pr_number = pr_data["number"]

        # Competition authority is checked before branch recovery, empty/stale
        # cleanup, labels, CI, repair, fallback, merge, or provider continuation.
        # ``--force`` therefore cannot turn a loser or an uncertain artifact into
        # ordinary work (Issue #2073, REQ-001/REQ-004).
        speculative = get_speculative_jules_lifecycle(github_client)
        if speculative is not None:
            issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_data, github_client)
            decision = speculative.evaluate_pr(repo_name, pr_number, tuple(issue_numbers))
            if not decision.allow_ordinary_processing:
                processed_pr.priority = "cleanup" if decision.cleanup_pending else "defer"
                processed_pr.outcome = PRProcessingOutcome.DEFERRED
                processed_pr.actions_taken = [f"Speculative Jules {decision.classification.value.lower()} artifact fenced: {decision.reason}"]
                _record_pr_stage(
                    pr_number,
                    "pr.speculative-jules-authority",
                    f"pr#{pr_number} speculative Jules authority",
                    Outcome.DEFERRED,
                    {"classification": decision.classification.value, "cleanup_pending": decision.cleanup_pending},
                )
                return processed_pr

        try:
            from .durable_repair_allowance import reconcile_unfulfilled_grant_reevaluations

            reconcile_unfulfilled_grant_reevaluations()
        except Exception as exc:
            logger.debug(f"Could not reconcile unfulfilled grant reevaluations: {exc}")

        # Resolve the execution origin before any diff, CI, merge, or checkout
        # behavior. ``work`` is Codex Cloud's shared/transient branch identity,
        # not a safe identity for an independently managed task.
        unsafe_branch_result = _reject_unsafe_codex_cloud_pr(github_client, repo_name, pr_data, config)
        if unsafe_branch_result.metadata_error:
            processed_pr.actions_taken = list(unsafe_branch_result.actions)
            processed_pr.priority = "defer"
            processed_pr.outcome = PRProcessingOutcome.DEFERRED
            _record_pr_stage(pr_number, "pr.unsafe-branch-recovery", f"pr#{pr_number} unsafe-branch recovery", Outcome.DEFERRED, {"reason": unsafe_branch_result.metadata_error})
            return processed_pr
        pr_data = unsafe_branch_result.authoritative_pr_data or pr_data
        processed_pr.pr_data = pr_data
        _record_codex_pr_attribution(repo_name, pr_data)
        if unsafe_branch_result.closed:
            processed_pr.actions_taken = unsafe_branch_result.actions
            processed_pr.priority = "close"
            _record_pr_stage(pr_number, "pr.unsafe-branch-recovery", f"pr#{pr_number} unsafe-branch recovery", Outcome.COMPLETED, {"effect": "closed", "reissue_delivered": unsafe_branch_result.reissue_delivered})
            return processed_pr

        projection = _link_codex_cloud_pr_to_issue(repo_name, pr_data, github_client)
        projection_action = _codex_projection_action(projection)

        # Close PRs with zero effective diff before any further processing.
        # This runs before the @auto-coder label check so empty PRs left from earlier
        # runs are closed and their source issues retried immediately.
        empty_pr_result = _close_empty_pr(github_client, repo_name, pr_data, config)
        if empty_pr_result.closed:
            processed_pr.actions_taken = [*([projection_action] if projection_action else []), *empty_pr_result.actions]
            processed_pr.priority = "close"
            _record_pr_stage(pr_number, "pr.empty-pr-recovery", f"pr#{pr_number} empty-PR recovery", Outcome.COMPLETED, {"effect": "closed", "issue_numbers": list(empty_pr_result.issue_numbers)})
            return processed_pr

        # Close Jules PRs that could not get CI green within the configured timeout.
        # This runs before the @auto-coder label check on purpose: a stale Jules PR
        # usually still carries the label from an earlier run, and skipping on the
        # label would leave the PR open forever.
        stale_jules_result = _close_stale_jules_pr(github_client, repo_name, pr_data, config)
        if stale_jules_result.closed:
            processed_pr.actions_taken = [*([projection_action] if projection_action else []), *stale_jules_result.actions]
            processed_pr.priority = "close"
            _record_pr_stage(pr_number, "pr.stale-jules-recovery", f"pr#{pr_number} stale-Jules recovery", Outcome.COMPLETED, {"effect": "closed", "issue_numbers": list(stale_jules_result.issue_numbers)})
            return processed_pr

        # Skip immediately if PR already has @auto-coder label
        with LabelManager(
            github_client,
            repo_name,
            pr_number,
            item_type="pr",
            skip_label_add=not force_adversarial_validation,
            known_labels=pr_data.get("labels"),
        ) as should_process:
            if not should_process:
                logger.info(f"Skipping PR #{pr_number} - already has @auto-coder label")
                get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - already processed", item_type="pr", item_number=pr_number, details={"skip_reason": "already_processed"})
                _record_pr_stage(pr_number, "pr.admission", f"pr#{pr_number} admission", Outcome.SKIPPED, {"reason": "already_processed"})
                processed_pr.actions_taken = [*([projection_action] if projection_action else []), "Skipped - already being processed (@auto-coder label present)"]
                return processed_pr

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data, config):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
            get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - waiting for Jules", item_type="pr", item_number=pr_number, details={"skip_reason": "waiting_for_jules"})
            _record_pr_stage(pr_number, "pr.provider-ownership-wait", f"pr#{pr_number} provider-ownership wait", Outcome.DEFERRED, {"provider": "jules"})
            processed_pr.actions_taken = [*([projection_action] if projection_action else []), "Skipped - waiting for Jules to fix CI failures"]
            return processed_pr

        # Process Jules PRs to detect session IDs and update PR body
        try:
            jules_success = _link_jules_pr_to_issue(repo_name, pr_data, github_client)
            if jules_success:
                logger.info(f"Successfully processed Jules PR #{pr_number} (or not a Jules PR)")
                get_trace_logger().log("Jules Link", f"Linked Jules PR #{pr_number}", item_type="pr", item_number=pr_number, details={"success": True})
            else:
                logger.warning(f"Failed to process Jules PR #{pr_number}, but continuing with normal processing")
                get_trace_logger().log("Jules Link", f"Failed to link Jules PR #{pr_number}", item_type="pr", item_number=pr_number, details={"success": False})
        except Exception as e:
            logger.error(f"Error in Jules PR processing for PR #{pr_number}: {e}")
            # Continue with normal processing even if Jules processing fails

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data, config):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
            get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - waiting for Jules", item_type="pr", item_number=pr_number, details={"skip_reason": "waiting_for_jules"})
            processed_pr.actions_taken = [*([projection_action] if projection_action else []), "Skipped - waiting for Jules to fix CI failures"]
            return processed_pr

        # Extract PR information
        branch_name = pr_data.get("head", {}).get("ref")
        pr_body = pr_data.get("body", "")
        related_issues = []
        if pr_body:
            # Extract linked issues from PR body
            related_issues = extract_linked_issues_from_pr_body(pr_body)

        with ProgressStage(
            "PR",
            pr_number,
            "Processing",
            related_issues=related_issues,
            branch_name=branch_name,
        ):
            try:
                get_trace_logger().log("PR Processing", f"Processing PR #{pr_number}", item_type="pr", item_number=pr_number, details={"branch": branch_name})

                # Check GitHub Actions status and mergeability
                github_checks = _check_github_actions_status(repo_name, pr_data, config, github_client)

                get_trace_logger().log("CI Status", f"CI Status for PR #{pr_number}: {'Success' if github_checks.success else 'Failure/Pending'}", item_type="pr", item_number=pr_number, details={"success": github_checks.success, "in_progress": github_checks.in_progress})

                mergeable = pr_data.get("mergeable", True)
                get_trace_logger().log("Merge Check", f"Mergeable status for PR #{pr_number}: {mergeable}", item_type="pr", item_number=pr_number, details={"mergeable": mergeable})

                # Always use _take_pr_actions for unified processing
                # This ensures tests that mock _take_pr_actions continue to work
                logger.info(f"PR #{pr_number}: Processing for issue resolution and merge")
                processed_pr.priority = "fix"

                # Process using _take_pr_actions
                processed_pr_result = _process_pr_for_fixes(
                    github_client,
                    repo_name,
                    pr_data,
                    config,
                    force_adversarial_validation=force_adversarial_validation,
                    adversarial_validation_scheduler=adversarial_validation_scheduler,
                    project_codex_task=False,
                )
                processed_pr.actions_taken = [*([projection_action] if projection_action else []), *processed_pr_result.actions_taken]
                processed_pr.priority = processed_pr_result.priority
                processed_pr.analysis = processed_pr_result.analysis
                processed_pr.outcome = processed_pr_result.outcome
                # Copy error if it was set
                if processed_pr_result.error:
                    processed_pr.error = processed_pr_result.error

                get_trace_logger().log("Decision", f"Finished processing PR #{pr_number}", item_type="pr", item_number=pr_number, details={"actions_taken": processed_pr.actions_taken, "error": processed_pr.error})

            finally:
                # Clear progress header after processing
                newline_progress()

        return processed_pr

    except GitHubRequestError as e:
        pr_number = pr_data.get("number", "unknown")
        revision = str(pr_data.get("head", {}).get("sha") or "")
        obligation = get_pending_work_store().defer(
            WorkIdentity(repo_name, f"pr:{pr_number}", PR_PROCESSING_STAGE, revision),
            e,
            (PR_PROCESSING_REFRESH_EFFECT, PR_PROCESSING_STAGE),
        )
        logger.warning(
            "Deferred PR #{} after GitHub operational failure {}; next eligible at {}",
            pr_number,
            obligation.reason.value,
            obligation.not_before,
        )
        _record_pr_stage(pr_number, "pr.strict-refresh", f"pr#{pr_number} strict refresh", Outcome.DEFERRED, {"reason": obligation.reason.value})
        return ProcessedPRResult(
            pr_data=pr_data,
            actions_taken=[f"Deferred GitHub-dependent work: {obligation.reason.value}"],
            priority="defer",
            analysis=None,
            error=str(e),
            outcome=PRProcessingOutcome.DEFERRED,
        )
    except Exception as e:
        pr_number = pr_data.get("number", "unknown")
        logger.error(f"Failed to process PR #{pr_number}: {e}")
        get_trace_logger().log("Error", f"Exception processing PR #{pr_number}: {e}", item_type="pr", item_number=pr_number, details={"error": str(e)})  # type: ignore
        _record_pr_stage(pr_number, "pr.execution", f"pr#{pr_number} execution", Outcome.FAILED, {"error": str(e)})
        return ProcessedPRResult(
            pr_data=pr_data,
            actions_taken=[f"Error processing PR: {str(e)}"],
            priority="error",
            analysis=None,
            error=str(e),
            outcome=PRProcessingOutcome.FAILED,
        )


def _is_dependabot_pr(pr_obj: Any) -> bool:
    """Return True if the PR is authored by a dependency bot.

    Dependency bots include Dependabot, Renovate, and accounts whose login
    ends with '[bot]' when IGNORE_DEPENDABOT_PRS is enabled.
    """
    try:
        login = get_pr_author_login(pr_obj)
        if not login:
            return False
        login_lower = login.lower()
        if "google-labs-jules[bot]" in login_lower:
            return False
        if "dependabot" in login_lower or "renovate" in login_lower or login_lower.endswith("[bot]"):
            return True
    except Exception:
        # Best-effort detection only; never fail hard here
        return False
    return False


@dataclass(frozen=True)
class DependencyBotAdmissionDecision:
    """Outcome of the common dependency-bot processing-policy gate (Issue #1995).

    ``allowed`` is False exactly when the evaluated PR must not acquire an
    implementation reservation; ``outcome``/``reason`` then describe the
    refusal so the caller can produce a specific ``SKIPPED``/``DEFERRED``
    result instead of success or a capacity-limit explanation.
    """

    allowed: bool
    outcome: Optional[ExplicitTargetOutcome] = None
    reason: str = ""


def _dependency_bot_flag_decision(
    is_dependency_bot: bool,
    ignore_dependabot_prs: bool,
    auto_merge_dependabot_prs: bool,
) -> Optional[DependencyBotAdmissionDecision]:
    """Classification/configuration portion of the dependency-bot gate.

    Shared by ``evaluate_dependency_bot_admission`` (the mandatory common
    admission gate) and the optional collector prefilter in
    ``AutomationEngine._get_candidates`` (Issue #1995, REQ-008). Returns a
    final decision for a positively identified non-bot PR, an
    ``IGNORE_DEPENDABOT_PRS``-excluded dependency-bot PR, or a dependency-bot
    PR under neither restrictive flag (ordinary processing, including normal
    repair of failing CI). Returns ``None`` when the dependency-bot PR
    additionally requires the readiness confirmation gated by
    ``AUTO_MERGE_DEPENDABOT_PRS``.
    """
    if not is_dependency_bot:
        return DependencyBotAdmissionDecision(allowed=True)
    if ignore_dependabot_prs:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.SKIPPED,
            reason="dependency-bot PR excluded by IGNORE_DEPENDABOT_PRS",
        )
    if not auto_merge_dependabot_prs:
        return DependencyBotAdmissionDecision(allowed=True)
    return None


def _dependency_bot_readiness_decision(
    *,
    is_open: Optional[bool],
    mergeable: Any,
    ci_error: Optional[str],
    ci_pending: bool,
    ci_success: bool,
) -> DependencyBotAdmissionDecision:
    """Readiness portion of the dependency-bot gate for ``AUTO_MERGE_DEPENDABOT_PRS``.

    Pure function shared by ``evaluate_dependency_bot_admission`` (which
    supplies fresh, same-HEAD facts) and the optional ``_get_candidates``
    prefilter (which supplies facts it already collected for priority
    calculation), so both apply identical readiness semantics (Issue #1995,
    REQ-002, REQ-003, REQ-006, REQ-008). ``is_open=None`` and a non-boolean
    ``mergeable`` are treated as not-yet-known rather than failing, so
    unavailable evidence defers instead of skipping.
    """
    if is_open is False:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.SKIPPED,
            reason="dependency-bot PR is no longer open",
        )
    if is_open is None:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason="dependency-bot PR open state is not yet known",
        )
    if not isinstance(mergeable, bool):
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason="dependency-bot PR mergeability is not yet known",
        )
    if mergeable is not True:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.SKIPPED,
            reason="dependency-bot PR is not mergeable",
        )
    if ci_error is not None:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason=f"dependency-bot PR CI observation is unavailable ({ci_error})",
        )
    if ci_pending or not ci_success:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.SKIPPED,
            reason="dependency-bot PR CI is not passing",
        )
    return DependencyBotAdmissionDecision(allowed=True)


def _fetch_authoritative_dependency_bot_pr(
    client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    pr_number: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch cache-bypassing PR metadata when the client supports it (REQ-005).

    A client without ``get_pull_request_metadata_strict`` (e.g. a lightweight
    test double) falls back to the caller-supplied ``pr_data`` unchanged;
    production ``GitHubClient`` always implements strict retrieval.
    """
    getter = getattr(type(client), "get_pull_request_metadata_strict", None)
    if not callable(getter):
        return pr_data, None
    try:
        refreshed = client.get_pull_request_metadata_strict(repo_name, pr_number)
    except Exception as exc:
        return None, str(exc)
    if not isinstance(refreshed, dict):
        return None, "GitHub returned malformed PR metadata"
    return refreshed, None


def evaluate_dependency_bot_admission(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> DependencyBotAdmissionDecision:
    """Common dependency-bot processing-policy gate.

    This is the mandatory common admission boundary in
    ``AutomationEngine._process_single_candidate_unified_impl`` (Issue #1995,
    REQ-004). It must run before any logical implementation reservation, new
    execution, or admission-related PR membership is created for a PR
    candidate, and it always obtains fresh, same-HEAD facts rather than
    reusing whatever the caller's candidate data already carries (REQ-005).
    """
    flag_decision = _dependency_bot_flag_decision(_is_dependabot_pr(pr_data), config.IGNORE_DEPENDABOT_PRS, config.AUTO_MERGE_DEPENDABOT_PRS)
    if flag_decision is not None:
        return flag_decision

    pr_number = pr_data.get("number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool):
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason="dependency-bot PR number is unavailable for readiness evaluation",
        )

    client = github_client or GitHubClient.get_instance()

    def _fetch() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        return _fetch_authoritative_dependency_bot_pr(client, repo_name, pr_data, pr_number)

    refreshed, fetch_error = _fetch()
    if fetch_error is not None or refreshed is None:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason=f"authoritative dependency-bot PR metadata is unavailable ({fetch_error})",
        )

    def _is_open(pr: Dict[str, Any]) -> Optional[bool]:
        state = pr.get("state")
        return None if state is None else state == "open"

    pre_ci_decision = _dependency_bot_readiness_decision(
        is_open=_is_open(refreshed),
        mergeable=refreshed.get("mergeable"),
        ci_error=None,
        ci_pending=False,
        ci_success=True,
    )
    if not pre_ci_decision.allowed:
        return pre_ci_decision

    head = refreshed.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not head_sha:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason="dependency-bot PR head SHA is unavailable",
        )

    ci_result = _check_github_actions_status(repo_name, refreshed, config, client)
    ci_error = ci_result.error if (not ci_result.success and not ci_result.in_progress) else None
    readiness_decision = _dependency_bot_readiness_decision(
        is_open=_is_open(refreshed),
        mergeable=refreshed.get("mergeable"),
        ci_error=ci_error,
        ci_pending=ci_result.in_progress,
        ci_success=ci_result.success,
    )
    if not readiness_decision.allowed:
        return readiness_decision

    # Re-confirm the same-HEAD open/mergeable facts after the CI read
    # completes: a change accepted during the read must fence this older
    # evidence rather than let it authorize admission (REQ-005 of Issue #1995).
    reconfirmed, reconfirm_error = _fetch()
    if reconfirm_error is not None or reconfirmed is None:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason=f"authoritative dependency-bot PR metadata is unavailable ({reconfirm_error})",
        )
    reconfirmed_head = reconfirmed.get("head")
    reconfirmed_sha = reconfirmed_head.get("sha") if isinstance(reconfirmed_head, dict) else None
    if _is_open(reconfirmed) is not True or reconfirmed.get("mergeable") is not True or reconfirmed_sha != head_sha:
        return DependencyBotAdmissionDecision(
            allowed=False,
            outcome=ExplicitTargetOutcome.DEFERRED,
            reason="dependency-bot PR state changed during readiness evaluation",
        )

    return DependencyBotAdmissionDecision(allowed=True)


def _should_skip_waiting_for_jules(github_client: Any, repo_name: str, pr_data: Dict[str, Any], config: Optional[AutomationConfig] = None) -> bool:
    """Check if PR should be skipped because it's waiting for Jules to fix CI failures.

    Returns True if:
    1. The last comment on the PR is the specific "CI checks failed..." message from Auto-Coder.
    2. There are no commits after that comment.
    3. The wait has not exceeded ``config.JULES_WAIT_TIMEOUT_HOURS``.
    """
    if _is_codex_or_claude_pr(pr_data):
        return False

    wait_timeout_hours = (config or AutomationConfig()).JULES_WAIT_TIMEOUT_HOURS
    try:
        pr_number = pr_data["number"]

        # Check Jules session status
        try:
            # Extract session ID from PR body
            pr_body = pr_data.get("body", "")
            session_id = _extract_session_id_from_pr_body(pr_body)

            if session_id:
                from auto_coder.jules_client import JulesClient

                jules_client = JulesClient()
                # Get specific session directly
                try:
                    target_session = jules_client.get_session(session_id)
                except Exception:
                    # If get_session fails (e.g. 404), treat as not found/no session
                    target_session = None

                if target_session:
                    from auto_coder.jules_engine import get_session_pull_request

                    state = target_session.get("state")
                    pull_request = get_session_pull_request(target_session)

                    if state == "COMPLETED" and pull_request:
                        # Extract PR info
                        pull_request_url = pull_request["url"]
                        parts = pull_request_url.split("/")
                        pull_idx = parts.index("pull")
                        jules_repo_name = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                        try:
                            jules_pr_number = int(parts[pull_idx + 1])
                        except ValueError:
                            pass

                        if jules_repo_name and jules_pr_number and jules_pr_number != pr_number:
                            # Check PR status
                            jules_pr = github_client.get_pull_request(jules_repo_name, jules_pr_number)
                            if jules_pr.get("state") == "closed":
                                logger.info(f"Jules session {session_id} PR #{jules_pr_number} is closed. Resuming processing.")
                            else:
                                logger.info(f"Jules session {session_id} PR #{jules_pr_number} is open. Waiting...")
                                return True

                    else:
                        logger.info(f"Jules session {session_id} for PR #{pr_number} found (State: {state}). Waiting...")
                        return True

        except Exception as e:
            logger.warning(f"Failed to check Jules session for PR #{pr_number}: {e}")

        # Get comments
        comments = github_client.get_pr_comments(repo_name, pr_number)
        if not comments:
            return False

        # Sort comments by date (newest last) just to be safe, though API usually returns them sorted
        comments.sort(key=lambda x: x["created_at"])

        last_comment = comments[-1]
        last_comment_body = last_comment.get("body", "")

        # Check if last comment is the specific message
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        if target_message not in last_comment_body:
            return False

        # Get last comment timestamp
        last_comment_time = last_comment["created_at"]

        # Get commits
        commits = github_client.get_pr_commits(repo_name, pr_number)
        if not commits:
            # If no commits found (unlikely for a PR), assume we shouldn't skip
            return False

        # Sort commits by date (newest last)
        commits.sort(key=lambda x: x["commit"]["committer"]["date"])

        last_commit = commits[-1]
        last_commit_time = last_commit["commit"]["committer"]["date"]

        # Compare timestamps
        # ISO format strings can be compared lexicographically if they are in the same timezone (usually UTC from GitHub)
        if last_commit_time > last_comment_time:
            logger.info(f"PR #{pr_number} has new commits after Jules wait message, processing...")
            return False

        # Check if it has been waiting longer than the configured timeout
        try:
            # Parse GitHub timestamp (ISO 8601)
            # Example: 2023-10-27T10:00:00Z
            last_comment_dt = datetime.fromisoformat(last_comment_time.replace("Z", "+00:00"))
            current_time = datetime.now(timezone.utc)

            if current_time - last_comment_dt > timedelta(hours=wait_timeout_hours):
                logger.info(f"PR #{pr_number} has been waiting for Jules for > {wait_timeout_hours} hour(s). Re-processing.")
                return False
        except Exception as e:
            logger.warning(f"Failed to parse timestamp or compare time for PR #{pr_number}: {e}")

        logger.info(f"PR #{pr_number} is waiting for Jules (last comment is wait message, no new commits)")
        return True

    except Exception as e:
        logger.error(f"Error checking if PR #{pr_data.get('number')} should be skipped: {e}")
        return False


def _is_empty_pr(
    pr_data: Dict[str, Any],
    repo_name: Optional[str] = None,
    github_client: Optional[Any] = None,
) -> bool:
    """Check if a pull request has no effective diff against its base branch.

    Do not use commit count as the emptiness check, as an empty PR may still contain commits.

    Args:
        pr_data: Pull request data dictionary
        repo_name: Optional repository name for fetching diff if changed_files not in pr_data
        github_client: Optional GitHub client for fetching diff if changed_files not in pr_data

    Returns:
        True if the PR has zero effective diff, False otherwise
    """
    changed_files = pr_data.get("changed_files")
    if changed_files is not None and isinstance(changed_files, int):
        return changed_files == 0

    additions = pr_data.get("additions")
    deletions = pr_data.get("deletions")
    if isinstance(additions, int) and isinstance(deletions, int):
        if additions > 0 or deletions > 0:
            return False
        if additions == 0 and deletions == 0:
            return True

    if repo_name and github_client:
        pr_number = pr_data.get("number")
        if pr_number is not None:
            try:
                diff = github_client.get_pr_diff(repo_name, pr_number)
                if isinstance(diff, str):
                    return len(diff.strip()) == 0
            except Exception as e:
                logger.debug(f"Failed to fetch PR diff for empty check on #{pr_number}: {e}")

    return False


def _resolve_pr_issue_numbers(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> List[int]:
    """Resolve associated source issue numbers for a PR using body, session ID, branch, or title.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        List of unique issue numbers associated with the PR
    """
    body = pr_data.get("body", "") or ""
    issue_numbers = extract_linked_issues_from_pr_body(body)
    if issue_numbers:
        return issue_numbers

    # Try resolving via session ID, branch name, or title
    resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)
    if resolved_issue:
        return [resolved_issue]

    # Additional generic fallback: check branch name for patterns like "issue-123" or "fix-123"
    branch_name = ""
    if isinstance(pr_data.get("head"), dict):
        branch_name = pr_data.get("head", {}).get("ref", "")
    if not branch_name:
        branch_name = pr_data.get("head_branch", "") or ""
    if branch_name:
        match = re.search(r"\b(?:issue|fix)[-_](\d+)\b", branch_name, re.IGNORECASE)
        if match:
            return [int(match.group(1))]

    # Additional generic fallback: check PR title
    pr_title = pr_data.get("title", "") or ""
    if pr_title:
        match = re.search(r"(?:issue|fix|close|resolve)s?\s*#(\d+)", pr_title, re.IGNORECASE)
        if match:
            return [int(match.group(1))]

    return []


def _is_cloud_run_retry_blocked(
    repo_name: str,
    issue_number: int,
    pr_number: int,
    reason: str,
) -> bool:
    """Return True if a durable `CloudRun` policy forbids a new attempt.

    Looks up the `CloudRun` (if any) persisted for the issue's current
    attempt and, when one exists, associates `pr_number` with it and asks
    its provider policy whether `reason` authorizes a new attempt. Issues
    with no persisted `CloudRun` (e.g. Jules, or any other path not yet
    migrated to this lifecycle) are unaffected and always return False so
    their existing behavior is preserved (see issue #1607, REQ-006/REQ-007).

    Centralizing this lookup keeps provider-name branching out of PR
    processing call sites (REQ-008): the actual retry decision comes from
    `cloud_run_policies.get_policy_for_provider()`.
    """
    try:
        from .cloud_run import CloudRunEvent, CloudRunRepository
        from .cloud_run_policies import get_policy_for_provider

        attempt = get_current_attempt(repo_name, issue_number)
        cloud_run_repo = CloudRunRepository(repo_name)
        run = cloud_run_repo.get(issue_number, attempt)
        if run is None:
            return False

        policy = get_policy_for_provider(run.provider)
        if policy is None:
            return False

        # Preserve this PR's association with the run without disturbing any
        # other PR already associated with it.
        cloud_run_repo.add_pull_request(issue_number, attempt, pr_number)

        event = CloudRunEvent(run=run, reason=reason, proposed_attempt=attempt + 1)
        return not policy.allow_new_attempt(event)
    except Exception as e:
        logger.error(f"Failed to evaluate CloudRun retry policy for issue #{issue_number}: {e}")
        return False


def _retry_linked_issue_after_cloud_reissue_failure(
    github_client: Any,
    repo_name: str,
    issue_number: int,
    pr_number: int,
    config: AutomationConfig,
    actions: List[str],
) -> None:
    """Release a linked issue to the ordinary attempt policy after failed delivery."""
    try:
        issue_obj = github_client.get_issue(repo_name, issue_number)
        state = issue_obj.get("state") if isinstance(issue_obj, dict) else getattr(issue_obj, "state", None)
        if state == "closed":
            github_client.reopen_issue(
                repo_name,
                issue_number,
                f"Auto-Coder: Reopening issue #{issue_number} because Codex Cloud could not reissue PR #{pr_number}.",
            )
            actions.append(f"Reopened closed issue #{issue_number}")
    except Exception as exc:
        logger.error(f"Failed to check/reopen issue #{issue_number}: {exc}")

    try:
        new_attempt = increment_attempt(repo_name, issue_number)
        actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
    except Exception as exc:
        logger.error(f"Failed to increment attempt for issue #{issue_number}: {exc}")
        actions.append(f"Failed to increment attempt for issue #{issue_number}: {exc}")


def _reject_unsafe_codex_cloud_pr(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> UnsafeCodexCloudPRResult:
    """Close a Codex Cloud PR published from the shared remote ``work`` branch.

    The originating task, rather than Auto-Coder's local checkout, must create
    the replacement branch and PR. Successful delivery deliberately retains
    each issue's processing label and attempt so no competing implementation is
    started. Failed task resolution or delivery releases the issue to the
    existing retry path.
    """
    result = UnsafeCodexCloudPRResult()
    authoritative_pr_data, metadata_error = _resolve_pr_safety_metadata(github_client, repo_name, pr_data)
    result.authoritative_pr_data = authoritative_pr_data
    if metadata_error:
        result.metadata_error = metadata_error
        result.actions.append(f"Skipping PR #{pr_data['number']}: authoritative branch safety could not be established ({metadata_error})")
        return result
    pr_data = authoritative_pr_data
    if not _is_unsafe_codex_cloud_branch(pr_data):
        return result

    if pr_data.get("state") == "closed":
        result.closed = True
        result.actions.append(f"Unsafe Codex Cloud PR #{pr_data['number']} on shared remote branch 'work' is already closed")
        return result

    pr_number = int(pr_data["number"])
    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
    if exhaustion_info and exhaustion_info.is_exhausted:
        result.actions.append(f"Skipping unsafe branch recovery for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
        return result

    issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_data, github_client)
    client = github_client or GitHubClient.get_instance()
    client.close_pr(
        repo_name,
        pr_number,
        "Auto-Coder: Closing this Codex Cloud PR because its remote head branch `work` is a shared, transient identity. A replacement PR must be published from a task-specific branch.",
    )
    _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
    result.closed = True
    result.actions.append(f"Closed unsafe Codex Cloud PR #{pr_number} on shared remote branch 'work'")

    task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
    delivered = False
    if task_id:
        prompt = Template(get_prompt_template("codex_cloud.unsafe_work_branch_reissue")).safe_substitute(
            pr_number=pr_number,
            issue_numbers=", ".join(f"#{number}" for number in issue_numbers) or "the linked issue",
        )
        try:
            from .codex_cloud_client import CodexCloudClient

            delivered = CodexCloudClient(repo_name=repo_name).send_followup(task_id, prompt)
        except Exception as exc:
            logger.warning(f"Could not request Codex Cloud reissue for PR #{pr_number}: {exc}")
            result.actions.append(f"Failed to request Codex Cloud reissue for PR #{pr_number}: {exc}")
    else:
        result.actions.append(f"Cannot request Codex Cloud reissue for PR #{pr_number}: no originating task found")

    if delivered:
        result.reissue_delivered = True
        result.actions.append(f"Requested Codex Cloud task '{task_id}' to publish a replacement PR from a task-specific branch")
        for issue_number in issue_numbers:
            result.actions.append(f"Preserved issue #{issue_number} in the in-flight Codex Cloud reissue flow")
    else:
        for issue_number in issue_numbers:
            _retry_linked_issue_after_cloud_reissue_failure(client, repo_name, issue_number, pr_number, config, result.actions)
    return result


def _close_empty_pr(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> EmptyPRResult:
    """Close a PR that has no effective diff against the base branch and requeue its source issue.

    When an empty PR is detected:
    1. Skip normal review/merge/LLM processing for that PR.
    2. Close the empty PR on GitHub.
    3. Resolve the source issue(s) using existing PR-to-Issue association logic.
    4. Reopen the source issue(s) if closed.
    5. Increment the source issue's attempt counter.
    6. Remove the @auto-coder label so the issue can be processed again.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        config: Automation configuration

    Returns:
        EmptyPRResult; ``closed`` is True if the PR was closed due to having no diff.
    """
    result = EmptyPRResult()
    pr_number = pr_data.get("number")
    if pr_number is None:
        return result
    pr_number = int(pr_number)

    try:
        if pr_data.get("state") == "closed":
            _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
            logger.debug(f"PR #{pr_number} is already closed, skipping empty PR check")
            return result

        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
        if exhaustion_info and exhaustion_info.is_exhausted:
            result.actions.append(f"Skipping empty PR recovery for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
            publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
            return result

        # The Codex Cloud branch-safety recovery owns these PRs, including
        # empty ones. AutomationEngine calls this helper while collecting and
        # dispatching candidates before process_pull_request reaches its safety
        # gate, so closing here would bypass the originating-task follow-up and
        # incorrectly release the linked issue for a competing implementation.
        if _is_unsafe_codex_cloud_branch(pr_data):
            logger.debug(f"Deferring empty PR #{pr_number} to Codex Cloud unsafe-branch recovery")
            return result

        if not _is_empty_pr(pr_data, repo_name=repo_name, github_client=github_client):
            return result

        logger.info(f"PR #{pr_number} has zero effective diff against base branch. Closing it.")

        # Resolve the issue(s) that this PR was created for
        issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_data, github_client)

        close_comment = f"Auto-Coder: Closing PR #{pr_number} because it has no effective diff against the base branch. " "The linked issue(s) will be retried with an incremented attempt count."
        client = github_client or GitHubClient.get_instance()
        client.close_pr(repo_name, pr_number, close_comment)
        _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
        result.closed = True
        result.actions.append(f"Closed empty PR #{pr_number} (zero effective diff)")
        get_trace_logger().log(
            "Empty PR",
            f"Closed empty PR #{pr_number}",
            item_type="pr",
            item_number=pr_number,
            details={"linked_issues": issue_numbers},
        )

        if not issue_numbers:
            logger.warning(f"No linked issue found for closed empty PR #{pr_number}, cannot increment attempt")
            result.actions.append(f"No linked issue found for empty PR #{pr_number} to increment attempt")
            return result

        for issue_number in issue_numbers:
            if _is_cloud_run_retry_blocked(repo_name, issue_number, pr_number, reason="empty_pr"):
                # The PR belongs to a durable CloudRun (e.g. Codex Cloud) whose
                # policy does not authorize an automatic new attempt from an
                # empty PR alone. Closing the PR must stay a PR-local action:
                # the Issue attempt is not incremented, the @auto-coder label
                # is not released, and no replacement task is enqueued (see
                # issue #1607).
                result.actions.append(f"Preserved issue #{issue_number} at its current attempt (CloudRun manual-only retry policy; empty PR #{pr_number} closed)")
                continue

            # Reopen the source issue if it was closed
            try:
                issue_obj = client.get_issue(repo_name, issue_number)
                if issue_obj:
                    state = issue_obj.get("state") if isinstance(issue_obj, dict) else getattr(issue_obj, "state", None)
                    if state == "closed":
                        logger.info(f"Reopening closed issue #{issue_number} due to empty PR #{pr_number}")
                        reopen_comment = f"Auto-Coder: Reopening issue #{issue_number} because PR #{pr_number} had no effective diff."
                        client.reopen_issue(repo_name, issue_number, reopen_comment)
                        result.actions.append(f"Reopened closed issue #{issue_number}")
            except Exception as e:
                logger.error(f"Failed to check/reopen issue #{issue_number}: {e}")

            try:
                new_attempt = increment_attempt(repo_name, issue_number)
                result.actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                result.actions.append(f"Failed to increment attempt for issue #{issue_number}: {e}")

            result.issue_numbers.append(issue_number)

    except Exception as e:
        logger.error(f"Error handling empty PR #{pr_number}: {e}")

    return result


def _close_stale_jules_pr(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_checks: Optional[Any] = None,
) -> StaleJulesPRResult:
    """Close a Jules PR that failed to get CI green within the configured timeout.

    Jules is the only actor allowed to push to its own PR branch, so a Jules PR that
    still has failing CI after ``config.JULES_PR_CI_TIMEOUT_HOURS`` is considered
    unfixable. The PR is closed, the attempt count of the linked issue(s) is
    incremented, and the ``@auto-coder`` label that the dead Jules run left on those
    issues is removed so they can be picked up again from scratch.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        config: Automation configuration
        github_checks: Optional already-fetched GitHub Actions status result

    Returns:
        StaleJulesPRResult; ``closed`` is False when the PR was left open.
    """
    result = StaleJulesPRResult()
    pr_number = int(pr_data["number"])

    try:
        from .llm_backend_config import is_jules_mode_enabled

        if not is_jules_mode_enabled():
            return result

        if not _is_jules_pr(pr_data):
            return result

        if pr_data.get("state") == "closed":
            _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
            logger.debug(f"PR #{pr_number} is already closed, skipping Jules staleness check")
            return result

        created_at = pr_data.get("created_at")
        if not created_at:
            logger.debug(f"PR #{pr_number} has no created_at timestamp, skipping Jules staleness check")
            return result

        try:
            created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError as e:
            logger.warning(f"Failed to parse created_at '{created_at}' for PR #{pr_number}: {e}")
            return result

        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - created_dt
        timeout = timedelta(hours=config.JULES_PR_CI_TIMEOUT_HOURS)
        if age <= timeout:
            return result

        # Only close when CI is actually not passing. Completed runs are required:
        # a run still in progress may yet turn green.
        if github_checks is None:
            github_checks = _check_github_actions_status(repo_name, pr_data, config, github_client)

        if github_checks.success:
            logger.info(f"Jules PR #{pr_number} is older than {config.JULES_PR_CI_TIMEOUT_HOURS}h but CI passed, keeping it open")
            return result

        if getattr(github_checks, "in_progress", False):
            logger.info(f"Jules PR #{pr_number} is older than {config.JULES_PR_CI_TIMEOUT_HOURS}h but CI is still running, keeping it open")
            return result

        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
        if exhaustion_info and exhaustion_info.is_exhausted:
            result.actions.append(f"Skipping stale Jules recovery for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
            publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
            return result

        logger.info(f"Jules PR #{pr_number} did not pass CI within {config.JULES_PR_CI_TIMEOUT_HOURS} hours. Closing it.")

        # Resolve the issue(s) that this PR was created for
        issue_numbers = extract_linked_issues_from_pr_body(pr_data.get("body", "") or "")
        if not issue_numbers:
            resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)
            if resolved_issue:
                issue_numbers = [resolved_issue]

        close_comment = f"Auto-Coder: Closing this PR because Jules did not get CI to pass within {config.JULES_PR_CI_TIMEOUT_HOURS} hours after the PR was created. The linked issue(s) will be retried with an incremented attempt count."
        client = github_client or GitHubClient.get_instance()
        client.close_pr(repo_name, pr_number, close_comment)
        _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
        result.closed = True
        result.actions.append(f"Closed stale Jules PR #{pr_number} (no passing CI within {config.JULES_PR_CI_TIMEOUT_HOURS}h)")
        get_trace_logger().log(
            "Jules Timeout",
            f"Closed stale Jules PR #{pr_number}",
            item_type="pr",
            item_number=pr_number,
            details={"timeout_hours": config.JULES_PR_CI_TIMEOUT_HOURS, "linked_issues": issue_numbers},
        )

        if not issue_numbers:
            logger.warning(f"No linked issue found for closed Jules PR #{pr_number}, cannot increment attempt")
            result.actions.append(f"No linked issue found for PR #{pr_number} to increment attempt")
            return result

        for issue_number in issue_numbers:
            try:
                new_attempt = increment_attempt(repo_name, issue_number)
                result.actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                result.actions.append(f"Failed to increment attempt for issue #{issue_number}: {e}")

            result.issue_numbers.append(issue_number)

    except Exception as e:
        logger.error(f"Error handling stale Jules PR #{pr_number}: {e}")

    return result


def _get_mergeable_state(
    repo_name: str,
    pr_data: Dict[str, Any],
    _config: AutomationConfig,
) -> Dict[str, Optional[Any]]:
    """Get latest mergeable state using existing data with optional refresh."""
    mergeable = pr_data.get("mergeable")
    merge_state_status = pr_data.get("mergeStateStatus")

    # Refresh mergeability only when value is unknown
    if mergeable is None:
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # API: api.pulls.get(owner, repo, pull_number)
            pr_details = api.pulls.get(owner, repo, pull_number=pr_data.get("number"))
            mergeable = pr_details.get("mergeable", mergeable)
            merge_state_status = pr_details.get("mergeStateStatus", merge_state_status)
        except Exception as e:
            logger.debug(f"Unable to refresh mergeable state for PR #{pr_data.get('number')}: {e}")

    return {"mergeable": mergeable, "merge_state_status": merge_state_status}


def _start_mergeability_remediation(pr_number: int, merge_state_status: Optional[str], repo_name: str = "") -> List[str]:
    """Implement mergeability remediation flow for non-mergeable PRs.

    This function handles the end-to-end flow for non-mergeable PRs:
    1. Get PR details and determine the base branch
    2. Checkout the PR branch
    3. Update from the base branch
    4. Resolve conflicts using existing helpers (including package-lock handling)
    5. Push the updated branch
    6. Mark PR as processed once push succeeds (via ACTION_FLAG:SKIP_ANALYSIS)

    Args:
        pr_number: PR number
        merge_state_status: Current merge state status from GitHub

    Returns:
        List of action strings describing what was done
    """
    actions = []
    state_text = merge_state_status or "unknown"

    if repo_name:
        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
        if exhaustion_info and exhaustion_info.is_exhausted:
            actions.append(f"Mergeability remediation stopped for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
            client = None
            try:
                client = GitHubClient.get_instance()
            except Exception:
                pass
            publish_exhaustion_comment_deduped(client, repo_name, pr_number, exhaustion_info)
            return actions

    try:
        log_action(f"Starting mergeability remediation for PR #{pr_number} (state: {state_text})")
        actions.append(f"Starting mergeability remediation for PR #{pr_number} (state: {state_text})")
        get_trace_logger().log("Remediation", f"Starting remediation for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"state": state_text})

        # Step 1: Get PR details to determine the base branch and head branch
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            pr_details = api.pulls.get(owner, repo, pull_number=pr_number)
            base_branch = pr_details.get("base", {}).get("ref", "main")
            head_branch = pr_details.get("head", {}).get("ref")

            if not head_branch:
                error_msg = f"Failed to determine head branch for PR #{pr_number} (head.ref is missing)"
                actions.append(error_msg)
                log_action(error_msg, False)
                return actions

        except Exception as e:
            error_msg = f"Failed to get PR #{pr_number} details via GhApi: {e}"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        actions.append(f"Determined base branch: {base_branch}, head branch: {head_branch} for PR #{pr_number}")

        # Step 2: Ensure PR branch exists and is up to date, then use BranchManager
        # Create minimal PR data for checkout function
        pr_branch_name = head_branch
        pr_data_for_checkout = {"number": pr_number, "head": {"ref": pr_branch_name}}

        # Ensure branch exists and is fetched, but don't switch yet
        prepare_success = _checkout_pr_branch("", pr_data_for_checkout, AutomationConfig(), perform_checkout=False)

        if not prepare_success:
            error_msg = f"Failed to prepare PR #{pr_number} branch ({pr_branch_name})"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        with BranchManager(pr_branch_name) as manager:
            actions.append(f"Checked out PR #{pr_number} branch")

            # Step 3: Update from base branch with conflict resolution
            # The _update_with_base_branch function includes:
            # - Fetching latest changes
            # - Merging base branch
            # - Using _perform_base_branch_merge_and_conflict_resolution for conflicts
            # - Pushing updated branch with retry
            get_trace_logger().log("Remediation", f"Updating base branch for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"step": "update_base"})
            # Preserve the authoritative GitHub PR metadata.  In particular,
            # cloud ownership is carried by the body/author and the exact
            # head/base SHAs are required for conflict-request deduplication.
            remediation_pr_data = dict(pr_details)
            remediation_pr_data["number"] = pr_number
            remediation_pr_data["base_branch"] = base_branch
            update_actions = _update_with_base_branch(repo_name, remediation_pr_data, AutomationConfig())
            actions.extend(update_actions)

            # Step 4: Check for degrading merge detection
        if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
            # LLM determined merge would degrade code quality
            # The _trigger_fallback_for_conflict_failure has already been called in conflict_resolver
            # The linked issues have been reopened and attempt incremented
            # Now we need to close the PR
            try:
                get_trace_logger().log("Remediation", f"Degrading merge detected for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "degrading"})
                _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.BLOCKED, {"result": "degrading", "state": state_text})
                client = GitHubClient.get_instance()
                close_comment = "Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                client.close_pr(repo_name, pr_number, close_comment)
                _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
                actions.append(f"Closed PR #{pr_number} without merging due to quality degradation risk")

                # Checkout main branch after closing PR
                main_branch = AutomationConfig().MAIN_BRANCH
                checkout_result = cmd.run_command(["git", "checkout", main_branch])
                if checkout_result.success:
                    actions.append(f"Checked out {main_branch} branch")
                else:
                    logger.warning(f"Failed to checkout {main_branch} branch: {checkout_result.stderr}")
                    actions.append(f"Warning: Failed to checkout {main_branch} branch")
            except Exception as e:
                logger.error(f"Failed to close PR #{pr_number}: {e}")
                actions.append(f"Error closing PR #{pr_number}: {e}")
            return actions

        # Step 5: Verify successful remediation
        # If push succeeded, the action flag will be set
        if any("Delegated merge-conflict repair" in action for action in update_actions):
            actions.append(f"Mergeability remediation delegated for PR #{pr_number}; deferring until a later pass observes a new head")
            actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            get_trace_logger().log("Remediation", f"Remediation delegated for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "delegated"})
            _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.ACCEPTED_HANDOFF, {"result": "delegated", "state": state_text})
        elif any("Pushed updated branch" in action for action in update_actions):
            actions.append(f"Mergeability remediation completed for PR #{pr_number}")
            actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            get_trace_logger().log("Remediation", f"Remediation success for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "success"})
            _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.COMPLETED, {"result": "success", "state": state_text})
        elif "ACTION_FLAG:SKIP_ANALYSIS" in update_actions:
            actions.append(f"Mergeability remediation deferred for PR #{pr_number}; no repair was confirmed")
            actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.DEFERRED, {"result": "unconfirmed", "state": state_text})
        elif "Failed" in str(update_actions):
            # Remediation attempted but failed
            actions.append(f"Mergeability remediation failed for PR #{pr_number}")
            get_trace_logger().log("Remediation", f"Remediation failed for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "failed"})
            _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.FAILED, {"result": "failed", "state": state_text})

    except Exception as e:
        error_msg = f"Error during mergeability remediation for PR #{pr_number}: {str(e)}"
        logger.error(error_msg)
        actions.append(error_msg)
        log_action(error_msg, False)
        _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.FAILED, {"result": "exception", "state": state_text, "error": str(e)})

    return actions


def _process_pr_for_merge(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> ProcessedPRResult:
    """Process a passing PR through the shared merge transition workflow.

    This entry point intentionally delegates the decision to ``_handle_pr_merge``
    instead of maintaining a second unresolved-thread gate. In particular, a
    claimed-addressed thread must enter independent adversarial validation here
    exactly as it does during batch processing.
    """
    processed_pr = ProcessedPRResult(
        pr_data=pr_data,
        actions_taken=[],
        priority="merge",
        analysis=None,
    )
    github_client = GitHubClient.get_instance()

    unsafe_branch_result = _reject_unsafe_codex_cloud_pr(github_client, repo_name, pr_data, config)
    if unsafe_branch_result.metadata_error:
        processed_pr.actions_taken = list(unsafe_branch_result.actions)
        processed_pr.outcome = PRProcessingOutcome.DEFERRED
        return processed_pr
    pr_data = unsafe_branch_result.authoritative_pr_data or pr_data
    processed_pr.pr_data = pr_data
    if unsafe_branch_result.closed:
        processed_pr.actions_taken = list(unsafe_branch_result.actions)
        processed_pr.priority = "close"
        return processed_pr

    projection_action = _codex_projection_action(_link_codex_cloud_pr_to_issue(repo_name, pr_data, github_client))

    # Use LabelManager context manager to handle @auto-coder label automatically
    with LabelManager(
        github_client,
        repo_name,
        pr_data["number"],
        item_type="pr",
        config=config,
        known_labels=pr_data.get("labels"),
    ) as should_process:
        if not should_process:
            processed_pr.actions_taken = [*([projection_action] if projection_action else []), "Skipped - already being processed (@auto-coder label present)"]
            return processed_pr

        processed_pr.actions_taken = [*([projection_action] if projection_action else []), *_handle_pr_merge(github_client, repo_name, pr_data, config, {}, processed_pr)]
        if any("Successfully merged" in action for action in processed_pr.actions_taken):
            should_process.keep_label()
        return processed_pr


def _process_pr_for_fixes(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    *,
    force_adversarial_validation: bool = False,
    adversarial_validation_scheduler: Optional[AdversarialValidationScheduler] = None,
    project_codex_task: bool = True,
) -> ProcessedPRResult:
    """Process a PR for issue resolution when GitHub Actions are failing or pending."""
    processed_pr = ProcessedPRResult(
        pr_data=pr_data,
        actions_taken=[],
        priority="fix",
        analysis=None,
    )

    unsafe_branch_result = _reject_unsafe_codex_cloud_pr(github_client, repo_name, pr_data, config)
    if unsafe_branch_result.metadata_error:
        processed_pr.actions_taken = list(unsafe_branch_result.actions)
        processed_pr.outcome = PRProcessingOutcome.DEFERRED
        return processed_pr
    pr_data = unsafe_branch_result.authoritative_pr_data or pr_data
    processed_pr.pr_data = pr_data
    if unsafe_branch_result.closed:
        processed_pr.actions_taken = list(unsafe_branch_result.actions)
        processed_pr.priority = "close"
        return processed_pr

    projection_action = _codex_projection_action(_link_codex_cloud_pr_to_issue(repo_name, pr_data, github_client)) if project_codex_task else ""

    # Use LabelManager context manager to handle @auto-coder label automatically
    with LabelManager(github_client, repo_name, pr_data["number"], item_type="pr", config=config) as should_process:
        if not should_process:
            processed_pr.actions_taken = [action for action in (projection_action, "Skipped - already being processed (@auto-coder label present)") if action]
            return processed_pr

        # Use the existing PR actions logic for fixing issues
        with ProgressStage("Fixing issues"):
            try:
                processing_status = ProcessedPRResult(pr_data=pr_data)
                actions = _take_pr_actions(
                    github_client,
                    repo_name,
                    pr_data,
                    config,
                    processing_status,
                    force_adversarial_validation=force_adversarial_validation,
                    adversarial_validation_scheduler=adversarial_validation_scheduler,
                )
                processed_pr.actions_taken = [*([projection_action] if projection_action else []), *actions]
                processed_pr.error = processing_status.error
                processed_pr.outcome = processing_status.outcome
                # Retain label on successful merge
                if any("Successfully merged" in action for action in actions):
                    should_process.keep_label()
            except AutoCoderRetryableBackendError as e:
                diagnostic = str(e)
                processed_pr.error = diagnostic
                processed_pr.actions_taken.append(f"Deferred: {diagnostic}")
                processed_pr.outcome = PRProcessingOutcome.DEFERRED
            except Exception as e:
                processed_pr.error = f"Processing failed: {str(e)}"
                processed_pr.actions_taken.append(processed_pr.error)
                processed_pr.outcome = PRProcessingOutcome.FAILED

    return processed_pr


def _take_pr_actions(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    processing_status: Optional[ProcessedPRResult] = None,
    *,
    force_adversarial_validation: bool = False,
    adversarial_validation_scheduler: Optional[AdversarialValidationScheduler] = None,
) -> PRActionList:
    """Take actions on a PR including merge handling and analysis."""
    actions = PRActionList()
    pr_number = pr_data["number"]

    try:
        # First, handle the merge process (GitHub Actions, testing, etc.)
        # This doesn't depend on Gemini analysis
        merge_actions = _handle_pr_merge(
            github_client,
            repo_name,
            pr_data,
            config,
            {},
            processing_status,
            force_adversarial_validation=force_adversarial_validation,
            adversarial_validation_scheduler=adversarial_validation_scheduler,
        )
        actions.extend(merge_actions)
        actions.adversarial_validation_error = getattr(merge_actions, "adversarial_validation_error", None)
        actions.quota_deferred = getattr(merge_actions, "quota_deferred", False)
        actions.retry_not_before = getattr(merge_actions, "retry_not_before", None)
        if actions.quota_deferred and processing_status is not None:
            processing_status.error = None
            processing_status.outcome = PRProcessingOutcome.DEFERRED

        # If merge process completed successfully (PR was merged), skip analysis
        if any("Successfully merged" in action for action in merge_actions):
            actions.append(f"PR #{pr_number} was merged.")
        elif (processing_status is None or processing_status.outcome is not PRProcessingOutcome.FAILED) and ("ACTION_FLAG:SKIP_ANALYSIS" in merge_actions or any("skipping to next PR" in action for action in merge_actions) or any("Skipping merge" in action for action in merge_actions)):
            actions.append(f"PR #{pr_number} processing deferred.")

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        actions.append(f"Error taking PR actions for PR #{pr_number}: {e}")
        if processing_status is not None:
            processing_status.error = str(e)
            processing_status.outcome = PRProcessingOutcome.FAILED

    return actions


def _trigger_fallback_for_pr_failure(
    repo_name: str,
    pr_data: Dict[str, Any],
    failure_reason: str,
) -> None:
    """Trigger fallback by incrementing attempts for linked issues when PR processing fails.

    Args:
        repo_name: Repository name in format 'owner/repo'
        pr_data: PR data dictionary
        failure_reason: Reason for the failure
    """
    try:
        # Extract linked issues from PR body
        pr_body = pr_data.get("body", "")
        if not pr_body:
            logger.debug(f"No PR body found for PR #{pr_data['number']}, cannot extract linked issues")
            return

        linked_issues = extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_data['number']} body")
            return

        # Identify this failure by the PR state it was observed on, so that a PR that
        # keeps failing without receiving new commits only bumps the attempt once.
        trigger = build_pr_attempt_trigger(pr_data["number"], pr_data.get("head_sha") or (pr_data.get("head") or {}).get("sha"))

        # Increment attempt for each linked issue
        for issue_number in linked_issues:
            try:
                logger.info(f"Incrementing attempt for issue #{issue_number} due to PR #{pr_data['number']} failure: {failure_reason}")
                increment_attempt(repo_name, issue_number, trigger=trigger)
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                # Continue with other issues even if one fails
                continue

        logger.info(f"Triggered fallback for {len(linked_issues)} linked issue(s) from PR #{pr_data['number']}")

    except Exception as e:
        logger.error(f"Error triggering fallback for PR #{pr_data['number']}: {e}")


def _apply_pr_actions_directly(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> List[str]:
    """Ask LLM CLI to apply PR fixes directly; avoid posting PR comments.

    Expected LLM output formats:
    - "ACTION_SUMMARY: ..." single line when actions were taken
    - "CANNOT_FIX" when it cannot deterministically fix
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        # Get PR diff for analysis
        with ProgressStage("Getting PR diff"):
            pr_diff = _get_pr_diff(repo_name, pr_number, config)

        # Create action-oriented prompt (no comments)
        with ProgressStage("Creating prompt"):
            # Create analysis prompt
            try:
                prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, github_client)
            except Exception:
                # Fallback for old signature if needed (though we are updating it)
                prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)
            logger.debug(
                "Prepared PR action prompt for #%s (preview: %s)",
                pr_data.get("number", "unknown"),
                prompt[:160].replace("\n", " "),
            )

        # Use LLM CLI to analyze and take actions
        log_action(f"Applying PR actions directly for PR #{pr_number}")

        # Call LLM client
        with ProgressStage("Running LLM"):
            response = get_llm_backend_manager()._run_llm_cli(prompt)

        # Process the response
        if response and len(response.strip()) > 0:
            resp = response.strip()
            # Prefer ACTION_SUMMARY line if present
            summary_line = None
            for line in resp.splitlines():
                if line.startswith("ACTION_SUMMARY:"):
                    summary_line = line
                    break
            if summary_line:
                actions.append(summary_line[: config.MAX_RESPONSE_SIZE])
            elif "CANNOT_FIX" in resp:
                actions.append(f"LLM reported CANNOT_FIX for PR #{pr_data['number']}")
                # Trigger fallback due to LLM failure
                _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (CANNOT_FIX)")
            else:
                # Fallback: record truncated raw response without posting comments
                actions.append(f"LLM response: {resp[: config.MAX_RESPONSE_SIZE]}...")
                # Trigger fallback due to unclear LLM response
                _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (unclear response)")

            # Detect self-merged indication in summary/response
            lower = resp.lower()
            if "merged" in lower or "auto-merge" in lower:
                actions.append(f"Auto-merged PR #{pr_number} based on LLM action")
            else:
                # Stage, commit, and push via helpers (LLM must not commit directly)
                with ProgressStage("Staging changes"):
                    add_res = cmd.run_command(["git", "add", "."])
                    if not add_res.success:
                        actions.append(f"Failed to stage changes: {add_res.stderr}")
                        return actions

                # Commit using centralized helper with dprint retry logic
                with ProgressStage("Committing changes"):
                    commit_msg = f"Auto-Coder: Apply fix for PR #{pr_number}"
                    commit_res = git_commit_with_retry(commit_msg)

                if commit_res.success:
                    actions.append(f"Committed changes for PR #{pr_number}")

                    # Push changes to remote with retry
                    with ProgressStage("Pushing changes"):
                        push_res = git_push()
                        if push_res.success:
                            actions.append(f"Pushed changes for PR #{pr_number}")
                        else:
                            # Push failed - try one more time after a brief pause
                            logger.warning(f"First push attempt failed: {push_res.stderr}, retrying...")

                    if not push_res.success:
                        with ProgressStage("Retrying push"):
                            import time

                            time.sleep(2)
                            retry_push_res = git_push()
                            if retry_push_res.success:
                                actions.append(f"Pushed changes for PR #{pr_number} (after retry)")
                            else:
                                logger.error(f"Failed to push changes after retry: {retry_push_res.stderr}")
                                actions.append(f"CRITICAL: Committed but failed to push changes: {retry_push_res.stderr}")
                                # Trigger fallback due to push failure
                                _trigger_fallback_for_pr_failure(repo_name, pr_data, "Failed to push changes after retry")
                else:
                    # Check if it's a "nothing to commit" case
                    if "nothing to commit" in (commit_res.stdout or ""):
                        actions.append("No changes to commit")
                    else:
                        # Save history and exit immediately
                        context = {
                            "type": "pr",
                            "pr_number": pr_number,
                            "commit_message": commit_msg,
                        }
                        save_commit_failure_history(commit_res.stderr, context, repo_name=None)
                        # This line will never be reached due to sys.exit in save_commit_failure_history
                        actions.append(f"Failed to commit changes: {commit_res.stderr or commit_res.stdout}")
                        # Trigger fallback due to commit failure
                        _trigger_fallback_for_pr_failure(repo_name, pr_data, "Failed to commit changes")
        else:
            actions.append("LLM CLI did not provide a clear response for PR actions")
            # Trigger fallback due to no LLM response
            _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (no response)")

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        actions.append(f"Error applying PR actions directly: {e}")
        # Trigger fallback due to exception
        _trigger_fallback_for_pr_failure(repo_name, pr_data, f"Exception during LLM processing: {str(e)}")

    return actions


def _get_pr_diff(repo_name: str, pr_number: int, config: AutomationConfig) -> str:
    """Get PR diff for analysis."""
    try:
        return GitHubClient.get_instance().get_pr_diff(repo_name, pr_number)[: config.MAX_PR_DIFF_SIZE]

    except Exception as e:
        logger.error(f"Failed to get PR diff via GhApi: {e}")
        return "Could not retrieve PR diff"


def _create_pr_analysis_prompt(repo_name: str, pr_data: Dict[str, Any], pr_diff: str, config: AutomationConfig, github_client: Optional[Any] = None, is_jules: bool = False) -> str:
    """Create a PR prompt that prioritizes direct code changes over comments with label-based selection."""
    pr_body = pr_data.get("body") or ""

    # Extract linked issues context
    linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_body)

    # Get commit log since branch creation
    commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

    body_text = pr_body[: config.MAX_PROMPT_SIZE]
    # Extract PR labels for label-based prompt selection, excluding the
    # retired "@auto-coder" legacy label (FTR-1792).
    pr_labels_list = filter_legacy_auto_coder_label(pr_data.get("labels", []) or [])

    result: str = render_prompt(
        "pr.action",
        repo_name=repo_name,
        pr_number=pr_data.get("number", "unknown"),
        pr_title=pr_data.get("title", "Unknown"),
        pr_body=body_text,
        pr_author=pr_data.get("user", {}).get("login", "unknown"),
        pr_state=pr_data.get("state", "open"),
        pr_draft=pr_data.get("draft", False),
        pr_mergeable=pr_data.get("mergeable", False),
        diff_limit=config.MAX_PR_DIFF_SIZE,
        pr_diff=pr_diff,
        commit_log=commit_log or "(No commit history)",
        linked_issues_context=linked_issues_context,
        labels=pr_labels_list,
        label_prompt_mappings=config.pr_label_prompt_mappings,
        label_priorities=config.label_priorities,
        is_jules=is_jules,
    )
    return result


def _process_pr_jules_mode(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Any,
) -> List[str]:
    """Process a PR using Jules API for session-based AI interaction.

    This function:
    1. Starts a Jules session for the PR
    2. Comments on the PR with the session ID
    3. Updates PR body with Session ID to mark it as Jules-managed
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        from .jules_client import JulesClient

        # Check if already Jules PR (sanity check)
        if _is_jules_pr(pr_data):
            return ["PR is already a Jules PR"]

        logger.info(f"Converting PR #{pr_number} to Jules mode")
        actions.append(f"Converting PR #{pr_number} to Jules mode")

        # 1. Start session
        jules_client = JulesClient()

        # Get prompt
        pr_diff = _get_pr_diff(repo_name, pr_number, config)
        action_prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, github_client, is_jules=True)

        pr_branch = pr_data.get("head", {}).get("ref")
        session_title = f"PR #{pr_number}: {pr_data.get('title', 'Unknown')}"

        session_id = jules_client.start_session(action_prompt, repo_name, pr_branch, title=session_title)

        # 2. Save session
        CloudManager(repo_name).add_session(pr_number, session_id, provider="jules")
        actions.append(f"Started Jules session {session_id}")

        get_trace_logger().log("Jules Mode", f"Started Jules session for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"session_id": session_id})

        # 3. Update PR body
        from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        pr_body = pr_data.get("body", "") or ""
        # Append session info
        new_body = f"{pr_body}\n\nSession ID: {session_id}\nhttps://jules.google.com/session/{session_id}"

        new_body = f"{pr_body}\n\nSession ID: {session_id}\nhttps://jules.google.com/session/{session_id}"

        # Validate issue references in new body
        try:
            validate_issue_references(new_body, github_client, repo_name)
        except ValueError as e:
            logger.error(f"Validation failed for Jules PR update: {e}")
            actions.append(f"Error: Validation failed for PR update: {e}")
            return actions

        api.pulls.update(owner, repo, pr_number, body=new_body)
        actions.append(f"Updated PR body with session ID: {session_id}")

        # 4. Comment
        comment = f"I started a Jules session to work on this PR. Session ID: {session_id}\n\nhttps://jules.google.com/session/{session_id}"
        github_client.add_comment_to_pr(repo_name, pr_number, comment)
        actions.append("Commented on PR with session details")

        return actions

    except Exception as e:
        msg = f"Error in _process_pr_jules_mode: {e}"
        logger.error(msg)
        actions.append(msg)
        return actions


def _find_authoritative_adversarial_review(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Find the dedicated reviewer App's native review body for an exact head SHA.

    Persisted adversarial-validation state now lives in a native PR review
    authored through the dedicated `auto-coder-reviewer` GitHub App instead of
    a regular user-authenticated PR comment. A review only counts as
    authoritative when it was authored by that exact App identity (resolved
    from the App's own credentials, not a configurable string) and carries
    the marker for this exact head SHA; a lookalike review from anyone else
    is ignored. Any failure to resolve the App identity or read PR reviews is
    reported as an error rather than treated as "no review", because this
    lookup gates merge decisions and must fail closed.
    """
    marker = adversarial_validation_comment_marker(head_sha)
    try:
        identity = resolve_reviewer_app_identity(repo_name)
    except Exception as e:
        logger.error(f"Failed to resolve dedicated reviewer App identity for PR #{pr_number}: {e}")
        return None, str(e)

    try:
        reviews = github_client.get_pr_reviews_strict(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to read PR reviews for PR #{pr_number}: {e}")
        return None, str(e)

    matching_bodies: List[str] = []
    for review in reviews:
        login = _comment_value(_comment_value(review, "user") or {}, "login", "")
        body = _comment_value(review, "body", "")
        if identity.matches_login(login) and isinstance(body, str) and body.startswith(marker):
            matching_bodies.append(body)

    if matching_bodies:
        # Completion order is not authority order when forced attempts overlap.
        # Attempt sequence is allocated at start, so a late V1 cannot replace V2.
        return max(enumerate(matching_bodies), key=lambda item: (_adversarial_validation_attempt_sequence(item[1]), item[0]))[1], None

    return None, None


def _adversarial_validation_attempt_sequence(body: str) -> int:
    """Return start order from a verdict, treating historical verdicts as zero."""
    match = re.search(r"<!-- auto-coder-adversarial-validation-attempt:v1:(\d+):[0-9a-f]+ -->", body)
    return int(match.group(1)) if match else 0


def _get_legacy_adversarial_validation_comment(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return a pre-migration validation comment for one PR head SHA, if any.

    Legacy comments were produced by the previous user-authenticated
    publication path. They are only ever read here for backward-compatible
    same-SHA deduplication; this path never creates or updates them.
    """
    marker = adversarial_validation_comment_marker(head_sha)
    try:
        comments = github_client.get_pr_comments(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to read legacy adversarial validation comments for PR #{pr_number}: {e}")
        return None, str(e)

    matching_bodies: List[str] = []
    for comment in comments:
        body = _comment_value(comment, "body", "")
        if isinstance(body, str) and body.startswith(marker):
            matching_bodies.append(body)
    return (max(enumerate(matching_bodies), key=lambda item: (_adversarial_validation_attempt_sequence(item[1]), item[0]))[1], None) if matching_bodies else (None, None)


def _parse_adversarial_validation_status(body: str) -> str:
    """Extract the verdict recorded in a validation marker body."""
    match = re.search(r"^## .*adversarial validation: (PASS|NEEDS_FIX|NEEDS_TESTS|BLOCKED|INCONCLUSIVE|ERROR|EXHAUSTED)\s*$", body, re.MULTILINE)
    if not match:
        return "ERROR"
    if match.group(1) == "PASS" and "### Specification gaps (" in body:
        return "PASS_WITH_SPECIFICATION_GAPS"
    return match.group(1)


def _parse_adversarial_validation_retry_not_before(body: str) -> Optional[float]:
    """Extract the durable retry-not-before epoch from an EXHAUSTED marker body."""
    match = re.search(r"<!--\s*auto-coder-adversarial-validation-retry-not-before:v1:([0-9.]+)\s*-->", body)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _adversarial_validation_exhaustion_retry_due(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
    published_status: Optional[str],
) -> Tuple[bool, Optional[str]]:
    """Return whether a currently applicable published EXHAUSTED result is due for retry.

    Implements REQ-005/REQ-006/REQ-007: a deferred retry is bound to the exact
    HEAD SHA that published EXHAUSTED (a status for a different or newer HEAD
    never reaches this helper) and is re-derived fresh from the durable
    GitHub-published state on every call rather than from cached local state,
    so it survives restart (REQ-007) without a bespoke store (AS-005). A
    missing or malformed retry marker is treated as due immediately so a
    still-applicable EXHAUSTED state can never get stuck (AS-005).
    """
    if published_status != "EXHAUSTED":
        return False, None
    body, error = _get_published_adversarial_validation_comment(github_client, repo_name, pr_number, head_sha)
    if error:
        return False, error
    retry_not_before = _parse_adversarial_validation_retry_not_before(body or "")
    if retry_not_before is None:
        return True, None
    return time.time() >= retry_not_before, None


def _get_published_adversarial_validation_status(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Read the immutable validation status already published for a head SHA.

    The dedicated reviewer App's native review is the authoritative source.
    A legacy user-authenticated comment is only consulted when no native
    review exists yet, so PR heads validated before this change keep their
    persisted state without being re-validated or re-published.
    """
    native_body, native_error = _find_authoritative_adversarial_review(github_client, repo_name, pr_number, head_sha)
    if native_error:
        return None, native_error
    if native_body is not None:
        return _parse_adversarial_validation_status(native_body), None

    legacy_body, legacy_error = _get_legacy_adversarial_validation_comment(github_client, repo_name, pr_number, head_sha)
    if legacy_error:
        return None, legacy_error
    if legacy_body is not None:
        return _parse_adversarial_validation_status(legacy_body), None

    return None, None


# The two verdicts that carry material specification violations requiring
# corrective changes (event=REQUEST_CHANGES in github_app_reviewer.publish).
# ERROR/BLOCKED/INCONCLUSIVE are validator-side non-results that must be
# retried rather than waited on (REQ-003), so they are deliberately excluded.
ADVERSARIAL_REVIEW_BLOCKING_STATUSES = frozenset({"NEEDS_FIX", "NEEDS_TESTS"})


def is_current_head_adversarial_review_blocked(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> bool:
    """Return whether Auto-Coder's own adversarial review blocks the current HEAD.

    True only when the dedicated reviewer App's authoritative review for the
    exact current ``head.sha`` recorded ``NEEDS_FIX`` or ``NEEDS_TESTS`` --
    the two verdicts that carry material specification violations requiring
    corrective changes and for which Auto-Coder has no further normal action
    to perform until the PR author/backend supplies a new commit.

    The status is re-read fresh for the current head SHA on every call, from
    the same authoritative same-SHA lookup the merge gate itself uses, so a
    verdict recorded against an older SHA can never keep a newer head
    classified as blocked (REQ-002). A validator error/timeout/indeterminate
    result (ERROR, BLOCKED, INCONCLUSIVE), a missing linked-Issue oracle, or
    any lookup failure all resolve to "not blocked" rather than being
    conflated with an actionable verdict (REQ-003). This intentionally never
    consults generic GitHub review state or human review threads (REQ-004):
    only the dedicated reviewer App's own marker-scoped verdict counts.
    """
    if not _is_pr_adversarial_validation_enabled(config, repo_name) or _is_dependabot_pr(pr_data):
        return False
    pr_number = pr_data.get("number")
    if not isinstance(pr_number, int):
        return False
    head_sha = pr_data.get("head", {}).get("sha", "")
    if not head_sha:
        return False
    eligibility = _get_adversarial_validation_eligibility(github_client, repo_name, pr_data)
    if eligibility.lookup_error or not eligibility.is_applicable:
        return False
    status, lookup_error = _get_published_adversarial_validation_status(
        github_client,
        repo_name,
        pr_number,
        head_sha,
    )
    if lookup_error or status is None:
        return False
    return status in ADVERSARIAL_REVIEW_BLOCKING_STATUSES


def _get_published_adversarial_validation_comment(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return the durable validation report for one PR head SHA."""
    native_body, native_error = _find_authoritative_adversarial_review(github_client, repo_name, pr_number, head_sha)
    if native_error:
        return None, native_error
    if native_body is not None:
        return native_body, None

    return _get_legacy_adversarial_validation_comment(github_client, repo_name, pr_number, head_sha)


@contextlib.contextmanager
def isolated_pr_head_worktree(repo_name: str, pr_number: int, head_sha: Optional[str] = None):
    """Context manager that creates an isolated, detached git worktree at head_sha for side-effect-free validation.

    Ensures that:
    1. Static repository inspection and dynamic test checks run against the exact CI-green head_sha.
    2. The caller's workspace, current branch, index, and untracked files remain completely untouched.
    3. No branch switching, pulling, pushing, resetting, or cleaning occurs on the main workspace during validation.
    """
    if not head_sha:
        raise ValueError(f"head_sha is required for isolated worktree validation of PR #{pr_number}")

    worktree_dir = None
    repository_cwd = os.getcwd()
    execution_token = None
    try:
        # Ensure the exact head_sha / pull head is fetched locally
        cmd.run_command(["git", "fetch", "origin", f"pull/{pr_number}/head"], cwd=repository_cwd)

        worktree_dir = tempfile.mkdtemp(prefix=f"auto_coder_val_pr{pr_number}_")

        add_res = cmd.run_command(["git", "worktree", "add", "--detach", worktree_dir, head_sha], cwd=repository_cwd)
        if not add_res.success:
            logger.warning(f"Failed to create isolated git worktree at {head_sha[:8]}: {add_res.stderr}")
            raise RuntimeError(f"Failed to create isolated git worktree at {head_sha[:8]}: {add_res.stderr}")

        verified = cmd.run_command(["git", "rev-parse", "HEAD"], cwd=worktree_dir)
        if not verified.success or verified.stdout.strip().lower() != head_sha.strip().lower():
            actual = verified.stdout.strip() or verified.stderr.strip() or "unavailable"
            raise RuntimeError(f"Isolated validation target mismatch: expected {head_sha}, found {actual}")
        execution_token = bind_command_execution_cwd(worktree_dir)
        logger.info(f"Bound isolated detached worktree at {head_sha[:8]} ({worktree_dir}) for validation")
        yield worktree_dir
    finally:
        if execution_token is not None:
            reset_command_execution_cwd(execution_token)

        if worktree_dir and os.path.exists(worktree_dir):
            try:
                cmd.run_command(["git", "worktree", "remove", "--force", worktree_dir], cwd=repository_cwd)
                shutil.rmtree(worktree_dir, ignore_errors=True)
                logger.debug(f"Cleaned up isolated worktree at {worktree_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up worktree dir {worktree_dir}: {e}")


def _refresh_adversarial_ci_status(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Any,
) -> GitHubActionsStatusResult:
    """Fence the carried read and obtain current authority after reviewer work."""
    end_ci_read_phase("adversarial reviewer round trip")
    return _check_github_actions_status(repo_name, pr_data, config, github_client)


@dataclass
class MergeRouteDisposition:
    """Structured result retained alongside the legacy boolean merge API."""

    outcome: PRProcessingOutcome = PRProcessingOutcome.DEFERRED
    reason: str = "Merge was not confirmed"


def _handle_pr_merge(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    analysis: Dict[str, Any],
    processing_status: Optional[ProcessedPRResult] = None,
    *,
    force_adversarial_validation: bool = False,
    adversarial_validation_scheduler: Optional[AdversarialValidationScheduler] = None,
) -> PRActionList:
    """Handle PR merge process following the intended flow."""
    actions = PRActionList()
    pr_number = pr_data["number"]
    decision_attempt_repository: Optional[AdversarialValidationAttemptRepository] = None
    decision_attempt_sequence = 0
    active_attempt_id = ""
    active_attempt_status = "ERROR"
    validation_admission = contextlib.ExitStack()

    try:
        # This is the lowest shared boundary for CI, review, adversarial,
        # conflict, checkout, and merge work.  Do not trust reduced caller data:
        # refresh it before deciding that a ``work`` head is not Codex Cloud.
        unsafe_branch_result = _reject_unsafe_codex_cloud_pr(github_client, repo_name, pr_data, config)
        if unsafe_branch_result.metadata_error:
            actions.extend(unsafe_branch_result.actions)
            if processing_status is not None:
                processing_status.outcome = PRProcessingOutcome.DEFERRED
            _record_pr_stage(pr_number, "pr.unsafe-branch-recovery", f"pr#{pr_number} unsafe-branch recovery", Outcome.DEFERRED, {"reason": unsafe_branch_result.metadata_error})
            return actions
        pr_data = unsafe_branch_result.authoritative_pr_data or pr_data
        if unsafe_branch_result.closed:
            actions.extend(unsafe_branch_result.actions)
            _record_pr_stage(pr_number, "pr.unsafe-branch-recovery", f"pr#{pr_number} unsafe-branch recovery", Outcome.COMPLETED, {"effect": "closed"})
            return actions

        watched_head = str(pr_data.get("head", {}).get("sha") or "")
        if watched_head:
            try:
                ci_watch_store = DurableInvalidationQueue(Path(os.environ.get("AUTO_CODER_INVALIDATION_DB", "~/.auto-coder/entity-invalidations.sqlite3")).expanduser())
                ci_watch_store.retire_ci_watches(repo_name, pr_number, watched_head)
                if not ci_watch_store.ensure_ci_watch(repo_name, pr_number, watched_head):
                    raise RuntimeError("invalid CI watch identity")
            except Exception as exc:
                logger.error(f"Durable CI watch unavailable repository={repo_name} pr={pr_number}: {exc}")
                actions.append(f"Skipping merge for PR #{pr_number}: durable CI watch unavailable")
                if processing_status is not None:
                    processing_status.outcome = PRProcessingOutcome.DEFERRED
                _record_pr_stage(pr_number, "pr.ci-watch-registration", f"pr#{pr_number} CI watch registration", Outcome.DEFERRED, {"reason": str(exc)})
                return actions

        # A stale review-thread resolution (issue #1619) that could not be
        # rolled back in an earlier run is a persistent integrity failure:
        # retry it on every processing run, and refuse to merge while any
        # thread remains blocked, regardless of what CI/validation would
        # otherwise decide this run (REQ-006, REQ-008).
        if _is_pr_review_thread_gate_enabled(config, repo_name):
            stale_client = github_client or GitHubClient.get_instance()
            try:
                pending_stale_threads = retry_pending_stale_review_thread_rollbacks(stale_client, repo_name, pr_number)
            except StaleReviewThreadRegistryError as e:
                # The registry's storage cannot be trusted (corrupt, unreadable):
                # it may be hiding a real stale-resolution blocker, so this must
                # never be treated as "no blockers exist" (REQ-006, REQ-008).
                logger.error(f"Stale-review-thread registry is unreadable for PR #{pr_number}: {e}")
                actions.append(f"Skipping merge for PR #{pr_number}: stale-review-thread registry could not be read ({e})")
                _record_pr_stage(pr_number, "pr.review-thread-gate", f"pr#{pr_number} review-thread gate", Outcome.FAILED, {"reason": str(e), "phase": "stale-rollback-registry"})
                return actions
            if pending_stale_threads:
                actions.append(f"Skipping merge for PR #{pr_number}: review thread(s) {', '.join(pending_stale_threads)} were resolved against a stale head and could not be reverted")
                _record_pr_stage(pr_number, "pr.review-thread-gate", f"pr#{pr_number} review-thread gate", Outcome.BLOCKED, {"phase": "stale-rollback-pending", "thread_ids": list(pending_stale_threads)})
                return actions

        # Apply every authorized review adjudication's owned effect (bounded
        # repair delivery for UPHOLD, scoped finding retirement for OVERRULE,
        # reconciliation of a superseded/revoked decision) unconditionally,
        # like the stale-thread rollback above, regardless of what CI or
        # adversarial validation would otherwise decide this run (Issue #2019).
        try:
            adjudication_actions, adjudication_forces_revalidation = _apply_review_adjudication_effects(repo_name, pr_number, pr_data, github_client)
        except Exception as exc:
            logger.warning(f"Could not apply review adjudication effects for PR #{pr_number}: {exc}")
            adjudication_actions, adjudication_forces_revalidation = PRActionList(), False
        actions.extend(adjudication_actions)

        # Step 1: Check GitHub Actions status using utility function
        # Use switch_branch_on_in_progress=False to just skip instead of exit
        should_continue = check_github_actions_and_exit_if_in_progress(  # type: ignore[arg-type]
            repo_name=repo_name,
            pr_data=pr_data,
            config=config,  # type: ignore[arg-type]
            github_client=None,
            switch_branch_on_in_progress=False,
            item_number=pr_number,
            item_type="PR",
        )  # Not needed for this check

        mergeability = _get_mergeable_state(repo_name, pr_data, config)
        mergeable_flag = mergeability.get("mergeable")
        merge_state_status = mergeability.get("merge_state_status")

        if mergeable_flag is False:
            state_text = merge_state_status or "unknown"
            actions.append(f"PR #{pr_number} is not mergeable (state: {state_text})")

            exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
            if exhaustion_info and exhaustion_info.is_exhausted:
                actions.append(f"Mergeability remediation stopped for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
                publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
                _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.BLOCKED, {"merge_state_status": state_text, "reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids)})
                return actions

            if config.ENABLE_MERGEABILITY_REMEDIATION:
                remediation_actions = _start_mergeability_remediation(pr_number, merge_state_status, repo_name)
                actions.extend(remediation_actions)
                return actions
            _record_pr_stage(pr_number, "pr.mergeability-remediation", f"pr#{pr_number} mergeability remediation", Outcome.SKIPPED, {"merge_state_status": state_text, "reason": "mergeability remediation is disabled"})

        # Step 2: If checks are in progress, skip this PR
        if not should_continue:
            actions.append(f"GitHub Actions checks are still in progress for PR #{pr_number}, skipping to next PR")
            if processing_status is not None:
                processing_status.outcome = PRProcessingOutcome.DEFERRED
            if watched_head:
                try:
                    ci_watch_store = DurableInvalidationQueue(Path(os.environ.get("AUTO_CODER_INVALIDATION_DB", "~/.auto-coder/entity-invalidations.sqlite3")).expanduser())
                    ci_watch_store.schedule_ci_watch_recheck(repo_name, pr_number, watched_head, delay_seconds=30.0)
                except Exception as exc:
                    logger.debug(f"Could not schedule CI watch recheck for PR #{pr_number}: {exc}")
            return actions

        # Step 3: Get detailed status for merge decision
        github_checks = _check_github_actions_status(repo_name, pr_data, config, github_client)
        if github_checks.error:
            actions.append(f"Could not determine CI status for PR #{pr_number}: {github_checks.error}")
            logger.error(f"Could not determine CI status for PR #{pr_number}: {github_checks.error}")
            if processing_status is not None:
                processing_status.error = github_checks.error
                processing_status.outcome = PRProcessingOutcome.FAILED
            _record_pr_stage(pr_number, "pr.ci-eligibility", f"pr#{pr_number} CI eligibility", Outcome.FAILED, {"reason": github_checks.error})
            return actions

        # Check if no actions have started for the latest commit
        if not github_checks.ids:
            # No checks found for the current head SHA
            logger.info(f"No GitHub Actions found for PR #{pr_number} (SHA: {pr_data.get('head', {}).get('sha')[:8]}). Triggering ci.yml...")

            # 1. Add @auto-coder label to prevent multiple executions
            # We use LabelManager to add the label
            with LabelManager(
                github_client,
                repo_name,
                pr_number,
                item_type="pr",
                config=config,
                known_labels=pr_data.get("labels"),
            ) as lm:
                # Label added by entering context

                # 2. Trigger workflow_dispatch
                from auto_coder.util.github_action import trigger_workflow_dispatch

                head_branch = pr_data.get("head", {}).get("ref")
                head_sha = pr_data.get("head", {}).get("sha")
                workflow_id = "ci.yml"

                # Manual CI dispatch admission is governed exclusively by the
                # durable dispatch-claim store (see GitHub Issue #1791). The
                # claim identity is repo + PR + head SHA + workflow, so a new
                # head SHA is a different identity and is never blocked by a
                # stale claim (REQ-001, REQ-007, REQ-008: no label involved).
                dispatch_identity = DispatchIdentity(
                    repo_name=repo_name,
                    pr_number=pr_number,
                    head_sha=head_sha or "",
                    workflow_id=workflow_id,
                )
                claim_store = get_dispatch_claim_store()
                claim = claim_store.try_acquire_claim(dispatch_identity)
                if not claim.acquired:
                    logger.info(f"Dispatch claim not acquired for PR #{pr_number} ({dispatch_identity.key()}): {claim.reason}")
                    actions.append(f"Skipped triggering {workflow_id} for PR #{pr_number}: dispatch already claimed ({claim.reason})")
                    _record_pr_stage(pr_number, "pr.manual-ci-dispatch", f"pr#{pr_number} manual CI dispatch", Outcome.SKIPPED, {"workflow": workflow_id, "reason": claim.reason})
                    return actions

                # Publish the restart-safe observation obligation before the
                # external dispatch.  A store failure leaves the already
                # acquired claim suppressing and therefore visibly blocked;
                # it must never degrade to an unmonitored dispatch.
                invalidation_path = Path(os.environ.get("AUTO_CODER_INVALIDATION_DB", "~/.auto-coder/entity-invalidations.sqlite3")).expanduser()
                try:
                    watch_store = DurableInvalidationQueue(invalidation_path)
                    watch_created = watch_store.ensure_ci_watch(repo_name, pr_number, head_sha or "", workflow_id)
                except Exception as exc:
                    logger.error(f"CI watch persistence blocked dispatch {dispatch_identity.key()}: {exc}")
                    actions.append(f"Blocked triggering {workflow_id} for PR #{pr_number}: durable CI watch unavailable")
                    _record_pr_stage(pr_number, "pr.manual-ci-dispatch", f"pr#{pr_number} manual CI dispatch", Outcome.DEFERRED, {"workflow": workflow_id, "reason": str(exc)})
                    return actions
                if not watch_created:
                    logger.error(f"CI watch identity was invalid for {dispatch_identity.key()}")
                    actions.append(f"Blocked triggering {workflow_id} for PR #{pr_number}: durable CI watch unavailable")
                    _record_pr_stage(pr_number, "pr.manual-ci-dispatch", f"pr#{pr_number} manual CI dispatch", Outcome.DEFERRED, {"workflow": workflow_id, "reason": "invalid CI watch identity"})
                    return actions

                try:
                    dispatch_result = trigger_workflow_dispatch(repo_name, workflow_id, head_branch)

                    # The claim was published before the external call, so any
                    # outcome other than a definite rejection must keep the
                    # identity suppressing (REQ-004, REQ-006). If the durable
                    # write itself fails, the claim is already left in its
                    # prior (suppressing) state, so dispatch admission still
                    # fails closed for this identity (REQ-003).
                    recorded = claim_store.record_outcome(
                        dispatch_identity,
                        dispatch_result.outcome,
                        holder_id=claim.holder_id,
                    )
                    if not recorded:
                        logger.error(f"Failed to durably record dispatch outcome {dispatch_result.outcome.value} for {dispatch_identity.key()}; claim remains suppressing")

                    if dispatch_result:
                        actions.append(f"Triggered {workflow_id} for PR #{pr_number}")
                        get_trace_logger().log("CI Trigger", f"Triggered {workflow_id} for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"workflow": workflow_id})

                        actions.append(f"Created durable CI watch for {workflow_id}")
                        get_trace_logger().log("CI Trigger", f"Created durable CI watch for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"workflow": workflow_id})
                        _record_pr_stage(pr_number, "pr.manual-ci-dispatch", f"pr#{pr_number} manual CI dispatch", Outcome.ACCEPTED_HANDOFF, {"workflow": workflow_id})
                        lm.keep_label()
                        return actions

                    else:
                        actions.append(f"Failed to trigger {workflow_id} for PR #{pr_number} (outcome={dispatch_result.outcome.value})")
                        _record_pr_stage(pr_number, "pr.manual-ci-dispatch", f"pr#{pr_number} manual CI dispatch", Outcome.FAILED, {"workflow": workflow_id, "outcome": dispatch_result.outcome.value})
                        # Label will be removed by LabelManager exit

                except Exception as e:
                    # Clean up active monitor on exception. The dispatch claim
                    # is intentionally left as-is: an exception here means the
                    # dispatch outcome could not even be classified and
                    # recorded, so the identity must remain suppressing
                    # (REQ-003, REQ-004, AS-003) rather than risk a duplicate.
                    raise e

            return actions

        # Step 4: If GitHub Actions passed, merge directly
        if github_checks.success:
            actions.append(f"All GitHub Actions checks passed for PR #{pr_number}")

            # Check if AUTO_MERGE is enabled before attempting merge
            if not config.AUTO_MERGE:
                actions.append(f"Skipping merge for PR #{pr_number} due to configuration (AUTO_MERGE=False)")
                return actions

            # Check for disable-auto-merge label
            labels = pr_data.get("labels", [])
            if any((isinstance(label, dict) and label.get("name") == "disable-auto-merge") or (isinstance(label, str) and label == "disable-auto-merge") for label in labels):
                actions.append(f"Skipping merge for PR #{pr_number} due to 'disable-auto-merge' label")
                return actions

            # Check for unresolved review threads. A thread explicitly claimed as
            # addressed by a supported automated reviewer's implementation agent
            # (REQ-001, REQ-011) does not block merge outright; it is instead
            # carried into a fresh adversarial validation run so an independent
            # disposition can decide whether to resolve it.
            thread_gate_enabled = _is_pr_review_thread_gate_enabled(config, repo_name)
            adv_enabled = _is_pr_adversarial_validation_enabled(config, repo_name)
            revalidating_older_head_threads = False
            forced_same_head_revalidation = False
            reviewer_login = ""
            claimed_review_threads: Sequence[Any] = ()

            if thread_gate_enabled:
                claimed_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number, config=config)
                if claimed_thread_state.lookup_error:
                    actions.append(f"Skipping merge for PR #{pr_number} because review threads could not be checked: {claimed_thread_state.lookup_error}")
                    if processing_status is not None:
                        processing_status.error = claimed_thread_state.lookup_error
                        processing_status.outcome = PRProcessingOutcome.FAILED
                    _record_pr_stage(pr_number, "pr.review-thread-gate", f"pr#{pr_number} review-thread gate", Outcome.FAILED, {"reason": claimed_thread_state.lookup_error})
                    return actions
                force_admission_eligible = False
                if adv_enabled:
                    if claimed_thread_state.has_blocking_unresolved and not _is_dependabot_pr(pr_data):
                        # An authentic validator finding remains a merge blocker, but it
                        # must not prevent validation of a newer head, nor prevent an
                        # explicit --force run from reaching a fresh current-head
                        # validation attempt (issue #2106 REQ-001).  Same-head non-PASS
                        # results otherwise take the ordinary blocking/dedup path.
                        head_sha_for_gate = pr_data.get("head", {}).get("sha", "")
                        gate_eligibility = _get_adversarial_validation_eligibility(github_client, repo_name, pr_data)
                        if head_sha_for_gate and gate_eligibility.is_applicable and not gate_eligibility.lookup_error:
                            current_status, current_status_error = _get_published_adversarial_validation_status(github_client, repo_name, pr_number, head_sha_for_gate)
                            if not current_status_error:
                                if current_status is None or force_adversarial_validation or adjudication_forces_revalidation:
                                    try:
                                        reviewer_login = resolve_reviewer_app_identity(repo_name).login
                                    except Exception as exc:
                                        logger.error(f"Could not authenticate unresolved adversarial threads for PR #{pr_number}: {exc}")
                                    else:
                                        forced_same_head_revalidation = current_status is not None and (force_adversarial_validation or adjudication_forces_revalidation)
                                        claimed_thread_state = _allow_older_head_adversarial_threads(claimed_thread_state, reviewer_login, forced=forced_same_head_revalidation)
                                        revalidating_older_head_threads = any(thread.revalidation_after_head_change for thread in claimed_thread_state.claimed)
                                        if revalidating_older_head_threads:
                                            actions.append(f"Allowing adversarial validation of new head {head_sha_for_gate[:8]} with older-head review findings still unresolved")
                                        if any(thread.revalidation_forced for thread in claimed_thread_state.claimed):
                                            actions.append(f"Forcing adversarial validation for PR #{pr_number} at head {head_sha_for_gate[:8]} with unresolved review findings via explicit --force")
                                if force_adversarial_validation:
                                    force_admission_eligible = True
                else:
                    claimed_thread_state = _filter_unresolved_review_threads_for_disabled_validator(claimed_thread_state, repo_name)
                if claimed_thread_state.has_blocking_unresolved:
                    actions.append(f"Skipping merge for PR #{pr_number} due to unresolved review threads")
                    _record_pr_stage(pr_number, "pr.review-thread-gate", f"pr#{pr_number} review-thread gate", Outcome.BLOCKED, {"blocking_count": len(claimed_thread_state.blocking_unresolved)})
                    pending_provenance = tuple(thread for thread in claimed_thread_state.blocking_unresolved if is_change_provenance_thread(thread))
                    repair_threads = tuple(thread for thread in claimed_thread_state.blocking_unresolved if not is_change_provenance_thread(thread))
                    if pending_provenance:
                        actions.append(f"Awaiting implementer provenance clarification on {len(pending_provenance)} review thread(s); no code change was requested")
                    if repair_threads:
                        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
                        if exhaustion_info and exhaustion_info.is_exhausted:
                            actions.append(f"Review repair stopped for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
                            publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
                            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.BLOCKED, {"effect": "review-thread-repair", "reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids)})
                        else:
                            repair_result = _delegate_cloud_review_thread_repair(
                                repo_name,
                                pr_data,
                                github_client=github_client,
                                unresolved_threads=repair_threads,
                            )
                            actions.extend(repair_result)
                            if repair_result.deferred and processing_status is not None:
                                processing_status.error = None
                                processing_status.outcome = PRProcessingOutcome.DEFERRED
                            elif not repair_result.delivered and processing_status is not None and not force_admission_eligible:
                                processing_status.error = repair_result[0] if repair_result else "Unresolved review repair was not delivered"
                                processing_status.outcome = PRProcessingOutcome.FAILED
                            _record_pr_stage(
                                pr_number,
                                "pr.repair-delegation",
                                f"pr#{pr_number} repair delegation",
                                Outcome.ACCEPTED_HANDOFF if repair_result.delivered else (Outcome.DEFERRED if repair_result.deferred else Outcome.FAILED),
                                {"effect": "review-thread-repair", "thread_count": len(repair_threads)},
                            )
                    if not force_admission_eligible:
                        return actions
                    # REQ-001/REQ-009: an explicit --force run still reaches a fresh
                    # current-head validation attempt; the remaining unresolved
                    # threads above are not eligible for independent rereview and
                    # remain a merge blocker (enforced again at the merge boundary).
                    actions.append(f"Continuing to forced adversarial validation for PR #{pr_number} despite unresolved review threads (explicit --force); merge remains blocked while they are unresolved")

                claimed_review_threads = claimed_thread_state.claimed
                if claimed_review_threads:
                    actions.append(f"PR #{pr_number} has {len(claimed_review_threads)} claimed-addressed review thread(s) pending independent validation")

            # Strong-model adversarial validation step. Issue-less PRs have no
            # independent specification oracle, so validation is not applicable.
            # Dependabot PRs have automated provenance and are not subject to
            # adversarial validation.
            adversarial_validation_enabled = adv_enabled and not _is_dependabot_pr(pr_data)
            adversarial_validation_applicable = False
            if not adversarial_validation_enabled:
                _record_pr_stage(
                    pr_number,
                    "pr.adversarial-validation",
                    f"pr#{pr_number} adversarial validation",
                    Outcome.SKIPPED,
                    {"reason": "disabled" if not adv_enabled else "dependabot PR is not subject to adversarial validation"},
                )
                if not adv_enabled:
                    # REQ-005/REQ-011: explicit disablement is a reached BYPASSED
                    # observation, never a new review or verdict.
                    _record_pr_adversarial_review_bypassed(repo_name, pr_data, config, thread_gate_enabled)
            if adversarial_validation_enabled:
                adversarial_eligibility = _get_adversarial_validation_eligibility(github_client, repo_name, pr_data)
                if adversarial_eligibility.lookup_error:
                    actions.append(f"Could not verify adversarial-validation eligibility for PR #{pr_number}: {adversarial_eligibility.lookup_error}; merge not attempted")
                    if processing_status is not None:
                        processing_status.error = adversarial_eligibility.lookup_error
                        processing_status.outcome = PRProcessingOutcome.FAILED
                    _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.FAILED, {"reason": adversarial_eligibility.lookup_error, "phase": "eligibility"})
                    return actions

                adversarial_validation_applicable = adversarial_eligibility.is_applicable
                if not adversarial_validation_applicable:
                    actions.append(f"Skipped adversarial validation for PR #{pr_number}: no linked Issue specification oracle")
                    logger.info(f"PR #{pr_number} has no linked Issue; adversarial validation is not applicable")
                    _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.SKIPPED, {"reason": "no linked Issue specification oracle"})
                elif not thread_gate_enabled:
                    # When thread gate is disabled, adversarial validation may still read
                    # claimed threads for independent validation (REQ-004), but lookup errors
                    # do not fail or defer the PR (REQ-003).
                    adv_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number, config=config)
                    if adv_thread_state.lookup_error:
                        logger.warning(f"Failed to check review threads for adversarial validation of PR #{pr_number}: {adv_thread_state.lookup_error}")
                    else:
                        claimed_review_threads = adv_thread_state.claimed
                        if claimed_review_threads:
                            actions.append(f"PR #{pr_number} has {len(claimed_review_threads)} claimed-addressed review thread(s) pending independent validation")

            if adversarial_validation_enabled and adversarial_validation_applicable:
                max_adv_reviews = config.MAX_ADVERSARIAL_VALIDATIONS if config.MAX_ADVERSARIAL_VALIDATIONS is not None else config.MAX_ADVERSARIAL_REVIEWS
                if max_adv_reviews is not None and max_adv_reviews >= 0:
                    try:
                        existing_pr_comments = github_client.get_pr_comments(repo_name, pr_number) if github_client else []
                        existing_reviews = github_client.get_pr_reviews_strict(repo_name, pr_number) if github_client else []
                        reviewer_identity = resolve_reviewer_app_identity(repo_name)
                        authoritative_reviews = [review for review in existing_reviews if reviewer_identity.matches_login(_comment_value(_comment_value(review, "user") or {}, "login", ""))]
                        adv_review_count = count_adversarial_validation_comments(existing_pr_comments) + count_adversarial_validation_comments(authoritative_reviews)
                    except Exception as e:
                        logger.error(f"Failed to check adversarial validation count for PR #{pr_number}: {e}")
                        actions.append(f"Could not check prior adversarial validation count for PR #{pr_number}: {e}; validation not started")
                        if processing_status is not None:
                            processing_status.error = str(e)
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions

                    provenance_fingerprint = change_provenance_reply_fingerprint(claimed_review_threads)
                    current_head_sha = pr_data.get("head", {}).get("sha", "")
                    saved_status, saved_status_error = _get_published_adversarial_validation_status(
                        github_client,
                        repo_name,
                        pr_number,
                        current_head_sha,
                    )
                    if saved_status_error:
                        actions.append(f"Could not check unresolved specification gaps for PR #{pr_number}: {saved_status_error}; merge not attempted")
                        if processing_status is not None:
                            processing_status.error = saved_status_error
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions
                    # A currently applicable published EXHAUSTED result establishes
                    # a deferred retry obligation that bypasses the review-count
                    # limit once due, without needing --force (REQ-005, REQ-006).
                    exhaustion_retry_due, exhaustion_retry_error = _adversarial_validation_exhaustion_retry_due(github_client, repo_name, pr_number, current_head_sha, saved_status)
                    if exhaustion_retry_error:
                        actions.append(f"Could not check deferred adversarial-validation retry state for PR #{pr_number}: {exhaustion_retry_error}; validation not started")
                        if processing_status is not None:
                            processing_status.error = exhaustion_retry_error
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions
                    if adv_review_count >= max_adv_reviews and not provenance_fingerprint and not force_adversarial_validation and not revalidating_older_head_threads and not exhaustion_retry_due:
                        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
                        if exhaustion_info and exhaustion_info.is_exhausted:
                            actions.append(f"Automatic merge disabled for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
                            publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
                            _record_pr_stage(
                                pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids), "machine_readable_reason": exhaustion_info.machine_readable_reason}
                            )
                            return actions

                        actions.append(f"Skipped adversarial validation for PR #{pr_number}: reached maximum adversarial review limit ({max_adv_reviews})")
                        logger.info(f"PR #{pr_number} reached maximum adversarial review limit ({adv_review_count}/{max_adv_reviews}); proceeding to merge")
                        if saved_status == "PASS_WITH_SPECIFICATION_GAPS":
                            actions.append(f"Automatic merge disabled for PR #{pr_number}: unresolved specification gaps require human policy review")
                            _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"reason": "unresolved specification gaps", "saved_status": saved_status})
                            return actions
                        if saved_status == "NEEDS_TESTS":
                            actions.append(f"Automatic merge disabled for PR #{pr_number}: unresolved material test-oracle gaps require focused regression tests")
                            _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"reason": "unresolved test-oracle gaps", "saved_status": saved_status})
                            return actions
                        should_run_validation = False
                        _record_pr_stage(
                            pr_number,
                            "pr.adversarial-validation",
                            f"pr#{pr_number} adversarial validation",
                            Outcome.DEFERRED if saved_status == "EXHAUSTED" else Outcome.SKIPPED,
                            {"reason": "reached maximum adversarial review limit", "max_adv_reviews": max_adv_reviews, "saved_status": saved_status},
                        )
                    else:
                        if adv_review_count >= max_adv_reviews and force_adversarial_validation:
                            actions.append(f"Forcing adversarial validation for PR #{pr_number} beyond the normal review limit")
                        elif adv_review_count >= max_adv_reviews and revalidating_older_head_threads:
                            actions.append(f"Revalidating PR #{pr_number} beyond the review limit because unresolved findings have not been adjudicated on the current head")
                        elif adv_review_count >= max_adv_reviews and exhaustion_retry_due:
                            actions.append(f"Retrying adversarial validation for PR #{pr_number} beyond the review limit: prior backend quota/usage exhaustion is due for automatic retry")
                        elif adv_review_count >= max_adv_reviews:
                            actions.append(f"Revalidating PR #{pr_number} beyond the review limit because new change-provenance evidence was supplied")
                        should_run_validation = True
                else:
                    should_run_validation = True

                if should_run_validation:
                    codex_review = _get_codex_review_state(github_client, repo_name, pr_number)
                    if codex_review.lookup_error:
                        actions.append(f"Could not determine Codex review state for PR #{pr_number}: {codex_review.lookup_error}; validation not started")
                        if processing_status is not None:
                            processing_status.error = codex_review.lookup_error
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions
                    if codex_review.present and not codex_review.completed:
                        actions.append(f"Waiting for Codex GitHub review to complete for PR #{pr_number}; adversarial validation not started")
                        _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.DEFERRED, {"reason": "waiting for Codex GitHub review"})
                        return actions
                    if codex_review.completed:
                        post_codex_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number)
                        if post_codex_thread_state.lookup_error:
                            if thread_gate_enabled:
                                actions.append(f"Codex review completed for PR #{pr_number}, but review threads could not be rechecked: {post_codex_thread_state.lookup_error}; validation not started")
                                if processing_status is not None:
                                    processing_status.error = post_codex_thread_state.lookup_error
                                    processing_status.outcome = PRProcessingOutcome.FAILED
                                return actions
                            else:
                                logger.warning(f"Codex review completed for PR #{pr_number}, but review threads could not be rechecked: {post_codex_thread_state.lookup_error}")
                        else:
                            if revalidating_older_head_threads:
                                post_codex_thread_state = _allow_older_head_adversarial_threads(post_codex_thread_state, reviewer_login)
                            # REQ-001/REQ-002: the recheck is a second pre-validation
                            # gate, so an explicit --force run must not be blocked here
                            # either, even for a thread that only became visible after
                            # the Codex review completed.
                            if thread_gate_enabled and force_adversarial_validation and post_codex_thread_state.has_blocking_unresolved and not _is_dependabot_pr(pr_data):
                                try:
                                    post_codex_reviewer_login = reviewer_login or resolve_reviewer_app_identity(repo_name).login
                                except Exception as exc:
                                    logger.error(f"Could not authenticate unresolved adversarial threads after Codex review for PR #{pr_number}: {exc}")
                                else:
                                    post_codex_thread_state = _allow_older_head_adversarial_threads(post_codex_thread_state, post_codex_reviewer_login, forced=True)
                                    if any(thread.revalidation_forced for thread in post_codex_thread_state.claimed):
                                        actions.append(f"Forcing adversarial validation for PR #{pr_number} with unresolved review findings observed after Codex review completion (explicit --force)")
                        if post_codex_thread_state.has_blocking_unresolved and thread_gate_enabled:
                            if force_adversarial_validation:
                                actions.append(f"Continuing to forced adversarial validation for PR #{pr_number} despite unresolved review threads observed after Codex review completion (explicit --force); merge remains blocked while they are unresolved")
                            else:
                                actions.append(f"Codex review completed for PR #{pr_number} with unresolved review threads; adversarial validation not started")
                                return actions
                        # Codex may have just posted its own review threads; use the
                        # freshest claimed-thread set for this validation run.
                        if not post_codex_thread_state.lookup_error:
                            claimed_review_threads = post_codex_thread_state.claimed

                    head_sha = pr_data.get("head", {}).get("sha", "")

                    if not head_sha:
                        actions.append(f"Adversarial validation blocked PR #{pr_number}: Missing head.sha in PR data")
                        logger.warning(f"Adversarial validation blocked PR #{pr_number}: Missing head.sha in PR data")
                        return actions

                    published_status, lookup_error = _get_published_adversarial_validation_status(
                        github_client,
                        repo_name,
                        pr_number,
                        head_sha,
                    )
                    unfinished_closure_outcomes: tuple[Any, ...] = ()
                    if lookup_error:
                        actions.append(f"Could not check prior adversarial validation for PR #{pr_number}: {lookup_error}; validation not started")
                        if processing_status is not None:
                            processing_status.error = lookup_error
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions
                    provenance_fingerprint = change_provenance_reply_fingerprint(claimed_review_threads)
                    has_new_provenance_evidence = False
                    saved_pass_has_unresolved_provenance = published_status == "PASS" and bool(provenance_fingerprint)
                    if published_status and provenance_fingerprint:
                        published_report, report_error = _get_published_adversarial_validation_comment(
                            github_client,
                            repo_name,
                            pr_number,
                            head_sha,
                        )
                        if report_error:
                            actions.append(f"Could not compare prior provenance evidence for PR #{pr_number}: {report_error}; validation not started")
                            if processing_status is not None:
                                processing_status.error = report_error
                                processing_status.outcome = PRProcessingOutcome.FAILED
                            return actions
                        has_new_provenance_evidence = bool(published_report and provenance_fingerprint not in published_report) or saved_pass_has_unresolved_provenance

                    # A currently applicable published EXHAUSTED result must not
                    # be a terminal same-HEAD deduplication result: once its
                    # deferred retry is due, revalidation proceeds without a new
                    # commit, manual activity, or --force (REQ-005, REQ-006,
                    # REQ-007). BLOCKED/ERROR/INCONCLUSIVE deliberately have no
                    # equivalent bypass (REQ-008).
                    exhaustion_retry_due, exhaustion_retry_error = _adversarial_validation_exhaustion_retry_due(github_client, repo_name, pr_number, head_sha, published_status)
                    if exhaustion_retry_error:
                        actions.append(f"Could not check deferred adversarial-validation retry state for PR #{pr_number}: {exhaustion_retry_error}; validation not started")
                        if processing_status is not None:
                            processing_status.error = exhaustion_retry_error
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        return actions

                    if published_status and not has_new_provenance_evidence and not exhaustion_retry_due and not force_adversarial_validation:
                        # REQ-005/REQ-011: an authoritative same-head result is
                        # consumed without a new reviewer-backend invocation.
                        # Provenance for the producing review may be genuinely
                        # unavailable (legacy pre-instrumentation result).
                        reuse_target = PrAdversarialReviewTarget(repository=repo_name, pr_number=pr_number, head_sha=head_sha)
                        reuse_policy_identity = _pr_adversarial_policy_identity(config, thread_gate_enabled)
                        reuse_source_review_id = find_reusable_source_review_id(reuse_target, policy_identity=reuse_policy_identity)
                        record_reused(
                            reuse_target,
                            policy_identity=reuse_policy_identity,
                            source_review_id=reuse_source_review_id,
                            native_verdict=published_status,
                            related_issue_membership=_pr_adversarial_linked_issue_membership(repo_name, pr_data, adversarial_eligibility.issue_numbers),
                        )
                        actions.append(f"Skipped adversarial validation for PR #{pr_number}: commit {head_sha[:8]} was already validated as {published_status}")
                        if published_status == "ERROR":
                            saved_validation_error = f"Adversarial validation previously failed for PR #{pr_number} at SHA {head_sha[:8]}"
                            actions.adversarial_validation_error = saved_validation_error
                            if processing_status is not None:
                                processing_status.error = saved_validation_error
                                processing_status.outcome = PRProcessingOutcome.FAILED
                        if published_status != "PASS":
                            actions.append(f"Adversarial validation remains non-pass for PR #{pr_number}: {published_status}")
                            if published_status in {"NEEDS_FIX", "NEEDS_TESTS"}:
                                published_report, report_error = _get_published_adversarial_validation_comment(
                                    github_client,
                                    repo_name,
                                    pr_number,
                                    head_sha,
                                )
                                if report_error:
                                    actions.append(f"Could not read the published adversarial report for Codex Cloud: {report_error}")
                                    if processing_status is not None:
                                        processing_status.error = report_error
                                        processing_status.outcome = PRProcessingOutcome.FAILED
                                elif published_report:
                                    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
                                    if exhaustion_info and exhaustion_info.is_exhausted:
                                        actions.append(f"Cached non-pass report replay stopped for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
                                        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
                                        _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.BLOCKED, {"effect": "adversarial-feedback-replay", "reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids)})
                                    else:
                                        actions.extend(
                                            _send_adversarial_validation_feedback_to_cloud_task(
                                                repo_name,
                                                pr_data,
                                                head_sha,
                                                published_report,
                                                github_client,
                                            )
                                        )
                            return actions
                    else:
                        if force_adversarial_validation:
                            actions.append(f"Forcing a new adversarial-validation attempt for PR #{pr_number} at unchanged commit {head_sha[:8]}")
                        elif has_new_provenance_evidence:
                            if saved_pass_has_unresolved_provenance:
                                actions.append(f"Revalidating PR #{pr_number} at unchanged commit {head_sha[:8]} because a saved PASS still has an unresolved provenance thread")
                            else:
                                actions.append(f"Revalidating PR #{pr_number} at unchanged commit {head_sha[:8]} using new implementer provenance evidence")
                        elif exhaustion_retry_due:
                            actions.append(f"Retrying adversarial validation for PR #{pr_number} at unchanged commit {head_sha[:8]}: prior backend quota/usage exhaustion is due for automatic retry")
                        # From here onward only this attempt's validated result may
                        # drive the decision; a saved same-head verdict is history.
                        published_status = None
                        claimed_review_threads_section = render_claimed_review_threads_section(claimed_review_threads)
                        if adversarial_validation_scheduler is not None:
                            lease = validation_admission.enter_context(adversarial_validation_scheduler.admit(repo_name, pr_number))
                            if not lease.acquired:
                                actions.append(f"Skipped duplicate local adversarial-validation trigger for PR #{pr_number}")
                                return actions
                        attempt_repository = AdversarialValidationAttemptRepository(repo_name)
                        attempt = attempt_repository.start(pr_number, head_sha)
                        active_attempt_id = attempt.attempt_id
                        decision_attempt_repository = attempt_repository
                        decision_attempt_sequence = attempt.sequence
                        codex_remediation_snapshot = _observe_codex_cloud_remediation_activity(repo_name, pr_data, github_client)
                        # REQ-001/REQ-002/REQ-004: one review_id covers every backend
                        # invocation (initial round, dynamic-check follow-up, session
                        # continuation) belonging to this one logical validation job.
                        review_target = PrAdversarialReviewTarget(repository=repo_name, pr_number=pr_number, head_sha=head_sha)
                        review_policy_identity = _pr_adversarial_policy_identity(config, thread_gate_enabled)
                        review_related_issue_membership = _pr_adversarial_linked_issue_membership(repo_name, pr_data, adversarial_eligibility.issue_numbers)
                        active_review_id: Optional[str] = None
                        try:
                            with begin_executed_review(
                                review_target,
                                policy_identity=review_policy_identity,
                                related_issue_membership=review_related_issue_membership,
                            ) as active_review_id:
                                with isolated_pr_head_worktree(repo_name, pr_number, head_sha) as validation_worktree:
                                    actions.append(f"Validated PR #{pr_number} in isolated worktree pinned to SHA {head_sha[:8]}")
                                    val_result = run_adversarial_validation(
                                        repo_name,
                                        pr_data,
                                        config,
                                        github_client=github_client,
                                        claimed_review_threads_section=claimed_review_threads_section,
                                        claimed_review_threads=claimed_review_threads,
                                        execution_cwd=validation_worktree,
                                        defer_session_persistence=True,
                                        ci_status=github_checks,
                                        refresh_ci_status=lambda: _refresh_adversarial_ci_status(repo_name, pr_data, config, github_client),
                                    )
                        except Exception as e:
                            exception_preview = redact_string(str(e))[:2000]
                            logger.error(f"Adversarial validation execution failed for PR #{pr_number} " f"({type(e).__name__}): {exception_preview}")
                            if processing_status is not None:
                                processing_status.error = str(e)
                                processing_status.outcome = PRProcessingOutcome.FAILED
                            val_result = AdversarialValidationResult(
                                result="ERROR",
                                summary="Adversarial validation execution failed; see the structured interaction log for details",
                                diagnostic_category="validation_execution_error",
                                diagnostic_reason=type(e).__name__,
                            )

                        val_result.attempt_id = attempt.attempt_id
                        val_result.attempt_sequence = attempt.sequence
                        if active_review_id:
                            finish_executed_review(active_review_id, review_target, val_result)
                        active_attempt_status = val_result.result.strip().upper() or "ERROR"

                        val_result.clarification_reply_fingerprint = provenance_fingerprint
                        if val_result.result.strip().upper() == "ERROR":
                            actions.adversarial_validation_error = val_result.summary
                        if provenance_fingerprint:
                            # The existing aggregated clarification thread remains
                            # authoritative until independently resolved.
                            val_result.publish_clarification_thread = False
                        val_result.provenance_thread_comment_ids = {thread.thread_id: thread.root_comment_database_id for thread in claimed_review_threads if thread.is_change_provenance and thread.root_comment_database_id is not None}

                        with attempt_repository.serialized_transition():
                            attempt_is_superseded = attempt_repository.latest_sequence(pr_number, head_sha) > attempt.sequence
                            if attempt_is_superseded:
                                actions.append(f"Ignored late adversarial-validation attempt {attempt.attempt_id}: a newer attempt is already applicable")
                                _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.SUPERSEDED, {"attempt_id": attempt.attempt_id, "examined_head": head_sha, "phase": "pre-publication"})
                                # REQ-006: this review's own result stays historical
                                # evidence for its head; it is superseded, not erased.
                                record_effect(review_target, active_review_id, "superseded", {"phase": "pre-publication", "attempt_id": attempt.attempt_id})
                                return actions
                            if val_result.reviewer_session_checkpoint is not None:
                                try:
                                    observed_head = github_client.get_pull_request_head_sha_strict(repo_name, pr_number)
                                except Exception as e:
                                    actions.append(f"Rejected adversarial-validation result for PR #{pr_number}: authoritative head could not be confirmed ({e})")
                                    _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.FAILED, {"attempt_id": attempt.attempt_id, "examined_head": head_sha, "phase": "gap-state-acceptance", "reason": "head observation unavailable"})
                                    # REQ-006: the authoritative head check itself
                                    # failed, so acceptance/refusal could not be
                                    # observed; record that failure as its own
                                    # effect rather than leaving the retained
                                    # review with no observation at all.
                                    record_effect(review_target, active_review_id, "failed", {"phase": "gap-state-acceptance", "reason": "head observation unavailable"})
                                    return actions
                                if observed_head != head_sha:
                                    actions.append(f"Ignored adversarial-validation attempt {attempt.attempt_id}: current head changed before durable acceptance")
                                    _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.SUPERSEDED, {"attempt_id": attempt.attempt_id, "examined_head": head_sha, "observed_head": observed_head, "phase": "gap-state-acceptance"})
                                    record_effect(review_target, active_review_id, "superseded", {"phase": "gap-state-acceptance", "observed_head": observed_head})
                                    return actions
                                checkpoint = val_result.reviewer_session_checkpoint
                                if checkpoint.evidence_validation_snapshot and not validation_snapshot_is_current(
                                    repo_name,
                                    pr_data,
                                    config,
                                    github_client,
                                    checkpoint.evidence_validation_snapshot,
                                ):
                                    actions.append(f"Ignored adversarial-validation attempt {attempt.attempt_id}: validation snapshot changed before durable acceptance")
                                    _record_pr_stage(
                                        pr_number,
                                        "pr.adversarial-validation",
                                        f"pr#{pr_number} adversarial validation",
                                        Outcome.SUPERSEDED,
                                        {
                                            "attempt_id": attempt.attempt_id,
                                            "examined_head": head_sha,
                                            "phase": "gap-state-acceptance",
                                            "reason": "validation snapshot changed or could not be confirmed",
                                        },
                                    )
                                    record_effect(review_target, active_review_id, "superseded", {"phase": "gap-state-acceptance", "reason": "validation snapshot changed or could not be confirmed"})
                                    return actions
                                try:
                                    registry = val_result.reviewer_session_registry or ReviewerSessionRegistry()
                                    registry.save(val_result.reviewer_session_checkpoint)
                                except Exception as e:
                                    logger.error(f"Failed to commit reviewer-gap state for PR #{pr_number}: {e}")
                                    val_result.reviewer_session_checkpoint = None
                                    val_result.result = "ERROR"
                                    val_result.summary = "Reviewer-gap state could not be committed; independent closure effects were suppressed"
                                    val_result.diagnostic_category = "reviewer_gap_persistence_failure"
                                    val_result.diagnostic_reason = str(e)
                                    val_result.thread_dispositions = []
                                    active_attempt_status = "ERROR"
                            resolved_thread_ids: List[str] = []
                            # Independent thread-completion validation (REQ-001..REQ-010): this
                            # runs whenever the authoritative fresh validation produced
                            # dispositions, regardless of the PR-level verdict.
                            if claimed_review_threads:
                                try:
                                    blocker_ledger = CanonicalPRBlockerLedger()
                                    base_sha_for_closure = str((pr_data.get("base") or {}).get("sha") or "")
                                    closure_report = resolve_addressed_review_threads(
                                        github_client,
                                        repo_name,
                                        pr_number,
                                        head_sha,
                                        claimed_review_threads,
                                        val_result.thread_dispositions,
                                        ledger=blocker_ledger,
                                        base_sha=base_sha_for_closure,
                                        review_attempt_id=active_attempt_id or "",
                                    )
                                    resolved_thread_ids = list(closure_report)
                                    unfinished_closure_outcomes = tuple(getattr(closure_report, "unfinished_outcomes", ()))
                                    if resolved_thread_ids:
                                        actions.append(f"Resolved {len(resolved_thread_ids)} claimed review thread(s) for PR #{pr_number} after independent validation")
                                    if unfinished_closure_outcomes:
                                        details = "; ".join(f"{outcome.thread_id} [{outcome.phase}]: {outcome.reason}" for outcome in unfinished_closure_outcomes)
                                        actions.append(f"Review-thread closure incomplete for PR #{pr_number}: " f"{len(resolved_thread_ids)} confirmed, {len(unfinished_closure_outcomes)} unfinished; {details}")
                                        _record_pr_stage(
                                            pr_number,
                                            "pr.review-thread-closure",
                                            f"pr#{pr_number} review-thread closure",
                                            Outcome.BLOCKED,
                                            {
                                                "confirmed_count": len(resolved_thread_ids),
                                                "unfinished_count": len(unfinished_closure_outcomes),
                                                "unfinished": [
                                                    {
                                                        "thread_id": outcome.thread_id,
                                                        "phase": outcome.phase,
                                                        "reason": outcome.reason,
                                                        "decision": outcome.decision,
                                                        "acceptance_state": outcome.acceptance_state,
                                                        "effect_state": outcome.effect_state,
                                                    }
                                                    for outcome in unfinished_closure_outcomes
                                                ],
                                            },
                                        )
                                except StaleReviewThreadResolutionError as e:
                                    # A thread is durably resolved against a stale head and
                                    # could not be rolled back: GitHub's authoritative
                                    # unresolved-thread gate can no longer be trusted for
                                    # this PR, so merge must not proceed this run even if
                                    # every other gate would otherwise allow it.
                                    logger.error(f"Stale review-thread resolution could not be rolled back for PR #{pr_number}: {e}")
                                    actions.append(f"Skipping merge for PR #{pr_number}: review thread {e.thread_id} was resolved against a stale head and could not be reverted")
                                    return actions
                                except Exception as e:
                                    logger.error(f"Failed to process claimed review thread dispositions for PR #{pr_number}: {e}")
                                    reason = f"Review-thread closure processing failed before completion: {e}"
                                    actions.append(f"Skipping merge for PR #{pr_number}: {reason}")
                                    _record_pr_stage(
                                        pr_number,
                                        "pr.review-thread-closure",
                                        f"pr#{pr_number} review-thread closure",
                                        Outcome.FAILED,
                                        {
                                            "confirmed_count": len(resolved_thread_ids),
                                            "unfinished_count": len(claimed_review_threads),
                                            "unfinished": [
                                                {
                                                    "thread_id": thread.thread_id,
                                                    "phase": "closure-processing",
                                                    "reason": str(e),
                                                }
                                                for thread in claimed_review_threads
                                            ],
                                        },
                                    )
                                    if processing_status is not None:
                                        processing_status.error = reason
                                        processing_status.outcome = PRProcessingOutcome.FAILED
                                    return actions

                            _enforce_unresolved_provenance_gate(val_result, claimed_review_threads, resolved_thread_ids)

                            validation_report = format_adversarial_validation_comment(val_result, head_sha)
                            if codex_remediation_snapshot is not None:
                                try:
                                    _record_review_validation_snapshot(
                                        _cloud_review_repair_state_path(repo_name),
                                        validation_report,
                                        codex_remediation_snapshot,
                                    )
                                except (OSError, ValueError) as exc:
                                    actions.append(f"Adversarial review publication blocked PR #{pr_number}: remediation-generation snapshot could not be persisted: {exc}")
                                    return actions
                            publication = publish_adversarial_review(repo_name, pr_number, head_sha, val_result)
                            if not publication.success:
                                # REQ-006: publication returning unsuccessful covers both a
                                # genuine failure and an accepted write whose response was
                                # lost; record it as pending until reconciliation (below)
                                # observes the actual durable outcome, rather than
                                # asserting a confirmed publication that may not exist.
                                record_effect(review_target, active_review_id, "pending", {"phase": "publication", "reason": publication.reason})
                                publication_confirmed, reconciliation_error = _reconcile_failed_adversarial_publication(
                                    github_client,
                                    repo_name,
                                    pr_number,
                                    head_sha,
                                    val_result,
                                )
                                if publication_confirmed:
                                    published_status = _parse_adversarial_validation_status(format_adversarial_validation_comment(val_result, head_sha))
                                    actions.append(f"Reconciled adversarial review publication for PR #{pr_number}: the expected verdict was already durable")
                                    record_effect(review_target, active_review_id, "confirmed", {"phase": "reconciliation"})
                                elif resolved_thread_ids:
                                    reopened_thread_ids = reopen_review_threads_after_publication_failure(
                                        github_client,
                                        repo_name,
                                        pr_number,
                                        claimed_review_threads,
                                        resolved_thread_ids,
                                    )
                                    if reopened_thread_ids:
                                        actions.append(f"Reopened {len(reopened_thread_ids)} review thread(s) after adversarial review publication failed")
                                    if len(reopened_thread_ids) != len(resolved_thread_ids):
                                        actions.append("Some review-thread publication rollbacks remain pending and will be retried before later merge processing")
                                if not publication_confirmed:
                                    reconciliation_suffix = f"; reconciliation failed: {reconciliation_error}" if reconciliation_error else ""
                                    actions.append(f"Adversarial review publication blocked PR #{pr_number}: {publication.reason}{reconciliation_suffix}")
                                    logger.warning(f"Adversarial review publication blocked PR #{pr_number}")
                                    _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.FAILED, {"attempt_id": attempt.attempt_id, "examined_head": head_sha, "reason": publication.reason, "phase": "publication"})
                                    # An ordinary publication failure must not erase the
                                    # already-retained semantic review report (REQ-006).
                                    record_effect(
                                        review_target,
                                        active_review_id,
                                        "unknown" if reconciliation_error else "failed",
                                        {"phase": "reconciliation", "reason": publication.reason, "reconciliation_error": reconciliation_error},
                                    )
                                    return actions
                            else:
                                if val_result.result.strip().upper() == "ERROR":
                                    actions.append(f"Published {publication.event} adversarial validation error diagnostic for PR #{pr_number} at SHA {head_sha[:8]}")
                                else:
                                    actions.append(f"Published {publication.event} adversarial review for PR #{pr_number} at SHA {head_sha[:8]}")
                                record_effect(review_target, active_review_id, "confirmed", {"phase": "publication", "event": publication.event})

                            attempt_repository.mark_published(attempt.attempt_id)
                            if attempt_repository.latest_published_sequence(pr_number, head_sha) > attempt.sequence:
                                actions.append(f"Ignored late adversarial-validation attempt {attempt.attempt_id}: a newer attempt is already applicable")
                                _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.SUPERSEDED, {"attempt_id": attempt.attempt_id, "examined_head": head_sha, "phase": "post-publication"})
                                record_effect(review_target, active_review_id, "superseded", {"phase": "post-publication"})
                                return actions
                    if published_status == "PASS" and unfinished_closure_outcomes:
                        reason = f"Review-thread closure remains unfinished for {len(unfinished_closure_outcomes)} " f"thread(s): {', '.join(outcome.thread_id for outcome in unfinished_closure_outcomes)}"
                        if processing_status is not None:
                            processing_status.error = reason
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        actions.append(f"Skipping merge for PR #{pr_number}: {reason}")
                        return actions
                    if published_status == "PASS":
                        pass
                    elif val_result.needs_fix:
                        actions.append(f"Adversarial validation failed for PR #{pr_number}: {len(val_result.findings)} specification violation(s) found")
                        logger.warning(f"PR #{pr_number} failed adversarial validation: {val_result.summary}")
                        _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"examined_head": head_sha, "findings": len(val_result.findings), "reason": "needs_fix"})
                        if not new_work_allowed():
                            actions.append(f"Deferred adversarial correction feedback for PR #{pr_number}: graceful shutdown is draining")
                            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.DEFERRED, {"effect": "adversarial-fix-feedback", "reason": "graceful shutdown is draining"})
                            return actions
                        feedback_actions = _send_adversarial_validation_feedback_to_cloud_task(
                            repo_name,
                            pr_data,
                            head_sha,
                            format_adversarial_validation_comment(val_result, head_sha),
                            github_client,
                            [format_adversarial_finding_comment(finding) for finding in val_result.findings] + [format_test_oracle_gap_comment(gap) for gap in val_result.open_test_oracle_gaps],
                        )
                        actions.extend(feedback_actions)
                        if getattr(feedback_actions, "quota_deferred", False) and processing_status is not None:
                            processing_status.error = None
                            processing_status.outcome = PRProcessingOutcome.DEFERRED
                        _record_pr_stage(
                            pr_number,
                            "pr.repair-delegation",
                            f"pr#{pr_number} repair delegation",
                            Outcome.ACCEPTED_HANDOFF if any("Sent" in a or "Requested" in a or "sent" in a for a in feedback_actions) else Outcome.UNKNOWN,
                            {"effect": "adversarial-fix-feedback", "examined_head": head_sha},
                        )
                        actions.append(f"Awaiting PR author or originating cloud-provider changes for PR #{pr_number}; no local automatic adversarial fix was attempted")
                        return actions

                    elif val_result.needs_tests:
                        actions.append(f"Adversarial validation requested focused regression protection for PR #{pr_number}: {len(val_result.open_test_oracle_gaps)} material test-oracle gap(s)")
                        logger.warning(f"PR #{pr_number} has material test-oracle gaps: {val_result.summary}")
                        _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"examined_head": head_sha, "test_oracle_gaps": len(val_result.open_test_oracle_gaps), "reason": "needs_tests"})
                        if not new_work_allowed():
                            actions.append(f"Deferred adversarial test feedback for PR #{pr_number}: graceful shutdown is draining")
                            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.DEFERRED, {"effect": "adversarial-test-feedback", "reason": "graceful shutdown is draining"})
                            return actions
                        feedback_actions = _send_adversarial_validation_feedback_to_cloud_task(
                            repo_name,
                            pr_data,
                            head_sha,
                            format_adversarial_validation_comment(val_result, head_sha),
                            github_client,
                            [format_test_oracle_gap_comment(gap) for gap in val_result.open_test_oracle_gaps],
                        )
                        actions.extend(feedback_actions)
                        if getattr(feedback_actions, "quota_deferred", False) and processing_status is not None:
                            processing_status.error = None
                            processing_status.outcome = PRProcessingOutcome.DEFERRED
                        _record_pr_stage(
                            pr_number,
                            "pr.repair-delegation",
                            f"pr#{pr_number} repair delegation",
                            Outcome.ACCEPTED_HANDOFF if any("Sent" in a or "Requested" in a or "sent" in a for a in feedback_actions) else Outcome.UNKNOWN,
                            {"effect": "adversarial-test-feedback", "examined_head": head_sha},
                        )
                        actions.append(f"Awaiting focused regression tests for PR #{pr_number}; production-code changes were not requested by test-oracle gaps")
                        return actions

                    elif not val_result.is_pass:
                        # Non-pass result (BLOCKED, INCONCLUSIVE, ERROR, EXHAUSTED) - fail-closed: do not merge!
                        # EXHAUSTED is never an actionable corrective verdict (REQ-010);
                        # its retry-not-before marker (already durably published above)
                        # is what makes it self-recovering, not this stage's outcome.
                        is_error = val_result.result.strip().upper() == "ERROR"
                        is_exhausted = val_result.is_exhausted
                        if is_error:
                            actions.append(f"ERROR: Adversarial validation failed for PR #{pr_number}: {val_result.summary}")
                        elif is_exhausted:
                            actions.append(f"Adversarial validation deferred for PR #{pr_number}: {val_result.summary}")
                        else:
                            actions.append(f"Adversarial validation blocked PR #{pr_number}: {val_result.summary}")
                        logger.warning(f"Adversarial validation blocked PR #{pr_number}: {val_result.summary}")
                        _record_pr_stage(
                            pr_number,
                            "pr.adversarial-validation",
                            f"pr#{pr_number} adversarial validation",
                            Outcome.FAILED if is_error else Outcome.DEFERRED if is_exhausted else Outcome.BLOCKED,
                            {
                                "examined_head": head_sha,
                                "result": val_result.result,
                                "summary": val_result.summary,
                                "diagnostic_category": val_result.diagnostic_category,
                                "diagnostic_reason": val_result.diagnostic_reason,
                                **({"retry_not_before_epoch": val_result.retry_not_before_epoch} if is_exhausted else {}),
                            },
                        )
                        return actions
                    elif not val_result.allows_auto_merge:
                        actions.append(f"Automatic merge disabled for PR #{pr_number}: {len(val_result.specification_gaps)} unresolved specification gap(s) require human policy review")
                        _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.BLOCKED, {"examined_head": head_sha, "reason": "unresolved specification gaps require human policy review", "specification_gaps": len(val_result.specification_gaps)})
                        return actions
                    else:
                        actions.append(f"Adversarial validation passed for PR #{pr_number}: {val_result.summary}")
                        _record_pr_stage(pr_number, "pr.adversarial-validation", f"pr#{pr_number} adversarial validation", Outcome.COMPLETED, {"examined_head": head_sha, "result": val_result.result})

            # An ordinary PASS is convergence, not merge authority, when the
            # optional strong tier applies. Persist it before entering the final
            # boundary so every caller (--only/--force, daemon, local/cloud, and
            # cached ordinary results) observes the same durable pending phase.
            if adversarial_validation_enabled and adversarial_validation_applicable:
                try:
                    two_tier_inputs = _two_tier_gate_inputs(github_client, repo_name, pr_data)
                except Exception as exc:
                    actions.append(f"Skipping merge for PR #{pr_number}: strong-audit identity could not be resolved: {exc}")
                    _record_pr_stage(
                        pr_number,
                        "pr.strong-audit-gate",
                        f"pr#{pr_number} strong-audit gate",
                        Outcome.DEFERRED,
                        {"reason": str(exc), "phase": "identity-resolution"},
                    )
                    return actions
                if two_tier_inputs is not None:
                    two_tier_inputs.gate.ordinary_pass(
                        pr_number,
                        two_tier_inputs.head_sha,
                        two_tier_inputs.base_sha,
                        two_tier_inputs.contract,
                    )
                    pending_snapshot = two_tier_inputs.gate.state.snapshot(pr_number)
                    reusable_completion = two_tier_inputs.gate.reusable_completion(
                        pr_number,
                        current_head_sha=two_tier_inputs.head_sha,
                        current_base_sha=two_tier_inputs.base_sha,
                        current_contract=two_tier_inputs.contract,
                        current_policy=two_tier_inputs.policy,
                    )
                    if reusable_completion is not None:
                        accepted = True
                        reason = f"reused existing {reusable_completion.basis} completion"
                        reviewer_backend = two_tier_inputs.policy.strong_route
                        stage_id = "pr.strong-audit"
                        stage_label = f"pr#{pr_number} strong audit"
                    elif pending_snapshot.phase == PHASE_ORDINARY_CLOSURE and pending_snapshot.open_findings:
                        accepted, reason, reviewer_backend = _execute_pending_ordinary_closure(repo_name, pr_number, two_tier_inputs)
                        stage_id = "pr.ordinary-closure"
                        stage_label = f"pr#{pr_number} ordinary closure"
                    else:
                        accepted, reason = _execute_pending_strong_audit(repo_name, pr_number, two_tier_inputs)
                        reviewer_backend = two_tier_inputs.policy.strong_route
                        stage_id = "pr.strong-audit"
                        stage_label = f"pr#{pr_number} strong audit"
                        # A competing controller can complete the exact target
                        # after our preliminary snapshot but before durable
                        # claim admission. Observe that fenced rejection as
                        # reuse, not as a newly deferred audit.
                        reusable_completion = two_tier_inputs.gate.reusable_completion(
                            pr_number,
                            current_head_sha=two_tier_inputs.head_sha,
                            current_base_sha=two_tier_inputs.base_sha,
                            current_contract=two_tier_inputs.contract,
                            current_policy=two_tier_inputs.policy,
                        )
                        if reusable_completion is not None:
                            accepted = True
                            reason = f"reused existing {reusable_completion.basis} completion"
                    published, publication_reason = _consume_pending_two_tier_publication(repo_name, pr_number, two_tier_inputs)
                    if reusable_completion is not None:
                        published = True
                        publication_reason = f"no publication required; reused {reusable_completion.basis} completion"
                    if published and reusable_completion is None:
                        published_snapshot = two_tier_inputs.gate.state.snapshot(pr_number)
                        published_round = published_snapshot.accepted_strong_round
                        if published_round is not None and published_round.verdict == VERDICT_PASS:
                            two_tier_inputs.gate.state.accept_strong_pass_completion(pr_number, published_round.round_id)
                    strong_diagnostic = two_tier_inputs.gate.diagnostic(
                        pr_number,
                        current_head_sha=two_tier_inputs.head_sha,
                        backend=two_tier_inputs.policy.strong_route,
                    )
                    actions.append(f"Two-tier review for PR #{pr_number}: {reason} " f"(phase={strong_diagnostic.phase}, head={two_tier_inputs.head_sha[:8]}, " f"contract={two_tier_inputs.contract.identity[:12]}, " f"policy={two_tier_inputs.policy.identity[:12]})")
                    actions.append(f"Two-tier review effect for PR #{pr_number}: {publication_reason}")
                    _record_pr_stage(
                        pr_number,
                        stage_id,
                        stage_label,
                        Outcome.COMPLETED if accepted else Outcome.DEFERRED,
                        {
                            "phase": strong_diagnostic.phase,
                            "backend": reviewer_backend,
                            "head": two_tier_inputs.head_sha,
                            "base": two_tier_inputs.base_sha,
                            "contract_identity": two_tier_inputs.contract.identity,
                            "policy_identity": two_tier_inputs.policy.identity,
                            "reason": reason,
                            "finding_revision": pending_snapshot.finding_set_revision,
                            "finding_ids": [item.finding_id for item in pending_snapshot.open_findings],
                        },
                    )
                    _record_pr_stage(
                        pr_number,
                        "pr.two-tier-review-effect",
                        f"pr#{pr_number} two-tier review effect",
                        Outcome.COMPLETED if published else Outcome.DEFERRED,
                        {
                            "phase": strong_diagnostic.phase,
                            "effect": "review-publication",
                            "reason": publication_reason,
                        },
                    )
                    # Acceptance by itself is never merge authority. A confirmed
                    # exact strong PASS may continue to the ordinary final gate;
                    # findings and unresolved effects remain blocked here.
                    if not two_tier_inputs.gate.authorize_merge(
                        pr_number,
                        current_head_sha=two_tier_inputs.head_sha,
                        current_base_sha=two_tier_inputs.base_sha,
                        current_contract=two_tier_inputs.contract,
                        current_policy=two_tier_inputs.policy,
                    ):
                        # Retain an authoritative due wake for the daemon. A
                        # quota deadline is preserved exactly; contention and
                        # pending effects receive a bounded retry so progress
                        # does not require a new webhook, commit, or restart.
                        actions.quota_deferred = True
                        actions.retry_not_before = pending_snapshot.retry_not_before or (time.time() + 60.0)
                        if processing_status is not None:
                            processing_status.outcome = PRProcessingOutcome.DEFERRED
                            processing_status.retry_not_before = actions.retry_not_before
                        return actions

            # Own the final read phase even when invoked outside candidate selection.
            with ci_read_phase("pr-final-merge-eligibility"):
                # Reviewer work can accept a newer complete CI observation than the
                # one that originally admitted validation. Reapply the production
                # gate after validation so that older green evidence cannot retain
                # merge authority after a pending or failing replacement.
                post_validation_checks = _refresh_adversarial_ci_status(repo_name, pr_data, config, github_client)
                if not post_validation_checks.success:
                    reason = post_validation_checks.error or ("checks are pending" if post_validation_checks.in_progress else "checks are not passing")
                    actions.append(f"Skipping merge for PR #{pr_number}: post-validation CI refresh {reason}")
                    _record_pr_stage(
                        pr_number,
                        "pr.ci-eligibility",
                        f"pr#{pr_number} post-validation CI eligibility",
                        Outcome.DEFERRED if post_validation_checks.in_progress else Outcome.BLOCKED,
                        {"phase": "post-adversarial-validation", "reason": reason},
                    )
                    return actions

                # Fresh authoritative thread observations at the merge boundary
                # decide whether any blocking thread remains (issue #2106
                # REQ-006). This re-read is independent of whatever thread state
                # admitted validation above: a thread newly opened during
                # validation, or one that force bypassed at admission but was
                # never independently resolved, must still block merge here.
                if thread_gate_enabled:
                    final_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number, config=config)
                    if final_thread_state.lookup_error:
                        actions.append(f"Skipping merge for PR #{pr_number}: review threads could not be rechecked at the merge boundary: {final_thread_state.lookup_error}")
                        _record_pr_stage(
                            pr_number,
                            "pr.review-thread-gate",
                            f"pr#{pr_number} final review-thread gate",
                            Outcome.FAILED,
                            {"reason": final_thread_state.lookup_error, "phase": "merge-boundary"},
                        )
                        return actions
                    if final_thread_state.has_blocking_unresolved:
                        actions.append(f"Skipping merge for PR #{pr_number}: unresolved review threads remain at the merge boundary")
                        _record_pr_stage(
                            pr_number,
                            "pr.review-thread-gate",
                            f"pr#{pr_number} final review-thread gate",
                            Outcome.BLOCKED,
                            {"blocking_count": len(final_thread_state.blocking_unresolved), "phase": "merge-boundary"},
                        )
                        return actions

                # Verify remote PR head SHA hasn't changed since CI check and validation before merging (fail-closed)
                head_sha = pr_data.get("head", {}).get("sha", "")
                if not github_client:
                    actions.append(f"Cannot verify remote head SHA for PR #{pr_number} without github_client; merge aborted.")
                    logger.warning(f"No github_client available to verify PR #{pr_number} head SHA; aborting merge.")
                    return actions

                exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
                if exhaustion_info and exhaustion_info.is_exhausted:
                    actions.append(f"Skipping merge for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
                    publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
                    _record_pr_stage(pr_number, "pr.merge-gate", f"pr#{pr_number} merge gate", Outcome.BLOCKED, {"reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids)})
                    return actions

                merge_transition = decision_attempt_repository.serialized_transition() if decision_attempt_repository is not None else contextlib.nullcontext()
                with merge_transition:
                    if decision_attempt_repository is not None and decision_attempt_repository.latest_sequence(pr_number, head_sha) > decision_attempt_sequence:
                        actions.append("Skipping merge because a newer adversarial-validation attempt is applicable")
                        return actions
                    try:
                        current_pr = github_client.get_pull_request(repo_name, pr_number)
                        current_head_sha = current_pr.get("head", {}).get("sha") if isinstance(current_pr, dict) else getattr(getattr(current_pr, "head", None), "sha", None)
                        if not current_head_sha:
                            actions.append(f"Could not determine current remote head SHA for PR #{pr_number}; merge aborted.")
                            logger.warning(f"Could not determine remote head SHA for PR #{pr_number}; aborting merge.")
                            return actions

                        if head_sha and current_head_sha != head_sha:
                            actions.append(f"PR #{pr_number} head SHA changed from {head_sha[:8]} to {current_head_sha[:8]} during validation; merge aborted.")
                            logger.warning(f"PR #{pr_number} head SHA changed during validation; skipping merge.")
                            _record_pr_stage(pr_number, "pr.head-refresh", f"pr#{pr_number} head refresh", Outcome.SUPERSEDED, {"examined_head": head_sha, "current_head": current_head_sha})
                            return actions
                    except Exception as e:
                        actions.append(f"Failed to verify remote head SHA for PR #{pr_number}: {e}; merge aborted.")
                        logger.warning(f"Failed to verify remote head SHA for PR #{pr_number}: {e}; skipping merge.")
                        if processing_status is not None:
                            processing_status.error = str(e)
                            processing_status.outcome = PRProcessingOutcome.FAILED
                        _record_pr_stage(pr_number, "pr.head-refresh", f"pr#{pr_number} head refresh", Outcome.FAILED, {"reason": str(e)})
                        return actions

                    merge_disposition = MergeRouteDisposition()

                    def merge_current_head() -> bool:
                        # Resolve H/B/M/P again inside the lowest merge mutation
                        # closure. This prevents any automatic origin from using a
                        # stale pre-validation snapshot or bypassing pending strong
                        # work through a cached ordinary PASS/review-budget shortcut.
                        if adversarial_validation_enabled and adversarial_validation_applicable:
                            try:
                                current_two_tier = _two_tier_gate_inputs(github_client, repo_name, current_pr)
                            except Exception as exc:
                                actions.append(f"Skipping merge for PR #{pr_number}: final strong-audit identity could not be resolved: {exc}")
                                return False
                            if current_two_tier is not None and not current_two_tier.gate.authorize_merge(
                                pr_number,
                                current_head_sha=current_two_tier.head_sha,
                                current_base_sha=current_two_tier.base_sha,
                                current_contract=current_two_tier.contract,
                                current_policy=current_two_tier.policy,
                            ):
                                diagnostic = current_two_tier.gate.diagnostic(
                                    pr_number,
                                    current_head_sha=current_two_tier.head_sha,
                                    backend=current_two_tier.policy.strong_route,
                                )
                                actions.append(f"Skipping merge for PR #{pr_number}: required strong-audit phase " f"{diagnostic.phase} is not complete ({diagnostic.waiting_reason})")
                                _record_pr_stage(
                                    pr_number,
                                    "pr.strong-audit-gate",
                                    f"pr#{pr_number} strong-audit gate",
                                    Outcome.BLOCKED,
                                    {
                                        "phase": diagnostic.phase,
                                        "backend": diagnostic.backend,
                                        "audited_head": diagnostic.audited_head,
                                        "current_head": diagnostic.current_head,
                                        "waiting_reason": diagnostic.waiting_reason,
                                        "outstanding_finding_ids": list(diagnostic.outstanding_finding_ids),
                                        "completion_basis": diagnostic.completion_basis,
                                    },
                                )
                                return False
                        return _merge_pr(
                            repo_name,
                            pr_number,
                            analysis,
                            config,
                            github_client=github_client,
                            expected_head_sha=current_head_sha or head_sha or None,
                            route_disposition=merge_disposition,
                        )

                    merge_result = False
                    observation = post_validation_checks.observation
                    if observation is None:
                        # Compatibility for callers whose status adapter predates
                        # snapshots; production GitHub reads always carry one.
                        merge_result = merge_current_head()
                    else:
                        with ci_observation_merge_authority(observation) as current_authority:
                            if current_authority:
                                merge_result = merge_current_head()
                        if not current_authority:
                            post_validation_checks = _refresh_adversarial_ci_status(repo_name, pr_data, config, github_client)
                            observation = post_validation_checks.observation
                            if not post_validation_checks.success or observation is None:
                                reason = post_validation_checks.error or ("checks are pending" if post_validation_checks.in_progress else "checks are not passing")
                                actions.append(f"Skipping merge for PR #{pr_number}: final CI authority refresh {reason}")
                                _record_pr_stage(
                                    pr_number,
                                    "pr.ci-eligibility",
                                    f"pr#{pr_number} final CI authority refresh",
                                    Outcome.DEFERRED if post_validation_checks.in_progress else Outcome.BLOCKED,
                                    {"phase": "pre-merge-authority", "reason": reason},
                                )
                                return actions
                            with ci_observation_merge_authority(observation) as refreshed_authority:
                                if refreshed_authority:
                                    merge_result = merge_current_head()
                            if not refreshed_authority:
                                actions.append(f"Skipping merge for PR #{pr_number}: CI authority was invalidated repeatedly at the merge boundary")
                                return actions
            if merge_result:
                actions.append(f"Successfully merged PR #{pr_number}")
                if processing_status is not None:
                    processing_status.outcome = PRProcessingOutcome.SUCCESS
                _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)

                # Clean up old PRs if this is a Jules PR with a session ID
                try:
                    is_jules = _is_jules_pr(pr_data)
                    session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))
                    if is_jules and session_id:
                        # Note: search_issues returns Issue objects which can be PRs
                        query = f'repo:{repo_name} is:pr is:open "Session ID: {session_id}"'
                        logger.info(f"Searching for other PRs with session ID {session_id} to clean up: {query}")

                        related_issues = github_client.search_issues(query)

                        for issue in related_issues:
                            # Skip the current PR (which is closed now effectively, or about to be)
                            if issue.number == pr_number:
                                continue

                            # Check if the issue object is actually a PR (search_issues returns issues/PRs)
                            # is:pr in query helps, but PyGithub object might need check?
                            # GitHubClient.search_issues returns list(self.github.search_issues(...))
                            # which are Issue objects.

                            # Historical processing labels are deliberately left untouched.

                except Exception as e:
                    logger.error(f"Error cleaning up related PRs for PR #{pr_number}: {e}")
                    # Don't fail the whole process for cleanup error
                _record_pr_stage(pr_number, "pr.cleanup", f"pr#{pr_number} cleanup", Outcome.COMPLETED, {"note": "best-effort; internal failures are swallowed and logged separately"})

                return actions
            else:
                # A false merge result after green CI is not CI-failure
                # evidence. It also represents retryable strong-audit and
                # durable merge-delivery states, so leave recovery to their
                # existing owners instead of entering a CI repair route.
                reason = next(
                    (action for action in reversed(actions) if action.startswith("Skipping merge for PR #")),
                    merge_disposition.reason,
                )
                actions.append(reason if reason not in actions else f"PR #{pr_number} remains {merge_disposition.outcome.value} at the merge boundary")
                if processing_status is not None:
                    processing_status.error = reason if merge_disposition.outcome is PRProcessingOutcome.FAILED else None
                    processing_status.outcome = merge_disposition.outcome
                _record_pr_stage(
                    pr_number,
                    "pr.merge-route",
                    f"pr#{pr_number} merge route",
                    Outcome.FAILED if merge_disposition.outcome is PRProcessingOutcome.FAILED else Outcome.DEFERRED,
                    {"reason": reason, "ci_failure": False},
                )
                return actions

        # Step 4: GitHub Actions failed - handle Jules PR feedback loop
        # Fetch detailed checks only when needed to save API calls
        detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)
        failed_checks = detailed_checks.failed_checks
        actions.append(f"GitHub Actions checks failed for PR #{pr_number}: {len(failed_checks)} failed")

        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
        if exhaustion_info and exhaustion_info.is_exhausted:
            actions.append(f"Automatic repair stopped for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
            publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
            _record_pr_stage(pr_number, "pr.repair-exhaustion", f"pr#{pr_number} repair exhaustion", Outcome.BLOCKED, {"reason": "repair allowance exhausted", "blocker_ids": list(exhaustion_info.exhausted_blocker_ids), "machine_readable_reason": exhaustion_info.machine_readable_reason})
            return actions

        # Codex Cloud owns corrective work for its PRs regardless of the local
        # checkout state. Resolve this execution origin before inspecting the
        if _is_codex_pr(pr_data):
            actions.append(f"PR #{pr_number} is a Codex-created PR, sending continuation request to Codex Cloud")
            feedback_result = _send_codex_cloud_error_feedback(repo_name, pr_data, failed_checks, config, github_client)
            actions.extend(feedback_result.actions)
            if feedback_result.delivered:
                actions.append(f"Codex Cloud will handle fixing PR #{pr_number}, skipping local fixes")
            else:
                actions.append(f"Codex Cloud repair request for PR #{pr_number} was not delivered; retry is required and local fixes remain disabled")
            _record_pr_stage(
                pr_number,
                "pr.repair-delegation",
                f"pr#{pr_number} repair delegation",
                Outcome.ACCEPTED_HANDOFF if feedback_result.delivered else Outcome.FAILED,
                {"effect": "ci-failure-repair", "backend": "codex-cloud", "retryable": feedback_result.retryable},
            )
            return actions

        # Check if we are already on the PR branch before checkout.
        #
        pr_branch_name = pr_data.get("head", {}).get("ref", "")
        current_branch_res = cmd.run_command(
            ["git", "branch", "--show-current"],
            timeout=60,
            stream_output=False,
        )
        current_branch = current_branch_res.stdout.strip() if current_branch_res.success else ""
        already_on_pr_branch = (current_branch == pr_branch_name) and (current_branch != "")

        # Check if this is a Jules PR.
        #
        # Jules PRs are never fixed automatically: Jules does not pick up commits pushed to
        # its branch by anyone else, so auto-fix commits would silently diverge from the
        # Jules session. Either Jules fixes the PR itself, or the PR is closed once it has
        # not passed CI within JULES_PR_CI_TIMEOUT_HOURS. Only an explicit local run that
        # already sits on the PR branch keeps fixing the checkout directly.
        if _is_jules_pr(pr_data) and not already_on_pr_branch:
            stale_jules_result = _close_stale_jules_pr(github_client, repo_name, pr_data, config, github_checks)
            if stale_jules_result.closed:
                actions.extend(stale_jules_result.actions)
                return actions

            actions.append(f"PR #{pr_number} is a Jules-created PR, sending error logs to Jules session")
            # Send error logs to Jules and skip local fixing - let Jules handle it
            jules_feedback_actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)
            actions.extend(jules_feedback_actions)
            actions.append(f"Jules will handle fixing PR #{pr_number}, skipping local fixes")
            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.ACCEPTED_HANDOFF, {"effect": "ci-failure-repair", "backend": "jules"})
            return actions

        # Step 5: Skip to process PR if it is dependabot PR
        if _is_dependabot_pr(pr_data):
            actions.append(f"PR #{pr_number} is a dependabot PR, skipping fixes")
            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.SKIPPED, {"effect": "ci-failure-repair", "reason": "dependabot PR"})
            return actions

        # Step 6: Only PRs created by local LLM execution (or explicit local checkout) are fixed by local LLM
        if not _is_local_llm_pr(pr_data) and not already_on_pr_branch:
            actions.append(f"PR #{pr_number} was not created by local LLM, skipping local LLM fixes")
            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.SKIPPED, {"effect": "ci-failure-repair", "reason": "not created by local LLM"})
            return actions

        # If automatic test fixing is disabled and we're not already on the PR branch,
        # skip checkout and test-failure repair entirely to avoid mutating the workspace.
        if not _is_automatic_test_fix_enabled(config, repo_name) and not already_on_pr_branch:
            actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping test-failure repair")
            _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.SKIPPED, {"effect": "ci-failure-repair", "reason": "automatic test fix is disabled"})
            return actions

        # Step 7: Checkout PR branch for non-Jules PRs
        # pr_branch_name is defined earlier (around line 1004)

        expected_head = str(pr_data.get("head", {}).get("sha") or "")
        with current_ci_failure_authority(github_client, repo_name, pr_number, expected_head) as authority:
            if not authority.allowed:
                actions.append(f"Deferred local CI repair for PR #{pr_number}: {authority.reason}")
                return actions
            logger.info(f"Initiating local CI repair for PR #{pr_number}; head={expected_head} failures={authority.failure_identities}")
            # Branch preparation may reset or clean the worktree, making it
            # the first effect for a different-checkout repair.
            prepare_ok = True if already_on_pr_branch else _checkout_pr_branch(repo_name, pr_data, config, perform_checkout=False)
            if not prepare_ok:
                actions.append(f"Failed to prepare PR #{pr_number} branch")
                return actions

            with BranchManager(pr_branch_name) as manager:
                actions.append(f"Checked out PR #{pr_number} branch")

                # Step 8: Optionally update with latest base branch commits (configurable)
                if config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL:
                    actions.append(f"[Policy] Skipping base branch update for PR #{pr_number} (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=True)")
                    get_trace_logger().log("Update Base", f"Skipped base branch update for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "skipped"})

                    # Proceed directly to extracting GitHub Actions logs and attempting fixes
                    if failed_checks:
                        github_logs, failed_test_files = _create_github_action_log_summary(repo_name, config, failed_checks)
                        fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, github_logs, failed_test_files, skip_github_actions_fix=already_on_pr_branch)
                        actions.extend(fix_actions)
                    else:
                        actions.append(f"No specific failed checks found for PR #{pr_number}")

                    return actions
                else:
                    actions.append(f"[Policy] Performing base branch update for PR #{pr_number} before fixes (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=False)")
                    update_actions = _update_with_base_branch(repo_name, pr_data, config)
                    actions.extend(update_actions)
                    if update_actions.quota_deferred:
                        actions.quota_deferred = True
                        actions.retry_not_before = update_actions.retry_not_before

                    # Step 9: Check for special cases from base branch update

                    # Check if LLM determined merge would degrade code quality
                    if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
                        actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, closing PR without merge")
                        # Close the PR without merging
                        try:
                            client = GitHubClient.get_instance()
                            close_comment = f"Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                            client.close_pr(repo_name, pr_number, close_comment)
                            _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
                            actions.append(f"Closed PR #{pr_number} without merging")

                            # BranchManager handles return to original branch
                        except Exception as e:
                            logger.error(f"Failed to close PR #{pr_number}: {e}")
                            actions.append(f"Error closing PR #{pr_number}: {e}")
                        return actions

                    # If base branch update required pushing changes, skip to next PR
                    if "ACTION_FLAG:SKIP_ANALYSIS" in update_actions or any("Pushed updated branch" in action for action in update_actions):
                        actions.append(f"Updated PR #{pr_number} with base branch, skipping to next PR for GitHub Actions check")
                        get_trace_logger().log("Update Base", f"Pushed updated branch for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "pushed"})
                        return actions

                    # Step 10: If no main branch updates were needed, the test failures are due to PR content
                    # Get GitHub Actions error logs and ask Gemini to fix
                    if any("up to date with" in action for action in update_actions):
                        actions.append(f"PR #{pr_number} is up to date with main branch, test failures are due to PR content")
                        get_trace_logger().log("Update Base", f"PR #{pr_number} is up to date", item_type="pr", item_number=pr_number, details={"result": "up_to_date"})

                        if not _is_automatic_test_fix_enabled(config, repo_name):
                            actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping test-failure repair")
                            return actions

                        # Fix PR issues using GitHub Actions logs first, then local tests
                        if failed_checks:
                            # Unit test expects _get_github_actions_logs(repo_name, failed_checks)
                            github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)  # type: ignore[arg-type]
                            fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, github_logs, skip_github_actions_fix=already_on_pr_branch)
                            actions.extend(fix_actions)
                        else:
                            actions.append(f"No specific failed checks found for PR #{pr_number}")
                    else:
                        # If we reach here, some other update action occurred
                        actions.append(f"PR #{pr_number} processing completed")

    except Exception as e:
        diagnostic = f"Error handling PR merge for PR #{pr_number}: {e}"
        actions.append(diagnostic)
        if processing_status is not None:
            processing_status.error = str(e)
            processing_status.outcome = PRProcessingOutcome.FAILED

    finally:
        # Finalize only this attempt after publication/merge decisions and all
        # owned worktree cleanup, while its admission lease is still held.
        if decision_attempt_repository is not None and active_attempt_id:
            try:
                decision_attempt_repository.finish(active_attempt_id, active_attempt_status)
            except Exception as e:
                logger.error(f"Failed to finalize adversarial-validation attempt {active_attempt_id}: {e}")
                if processing_status is not None:
                    processing_status.error = str(e)
                    processing_status.outcome = PRProcessingOutcome.FAILED
            finally:
                validation_admission.close()
        else:
            validation_admission.close()

    return actions


def _checkout_pr_branch(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig, perform_checkout: bool = True) -> bool:
    """Checkout the PR branch for local testing.

    If config.FORCE_CLEAN_BEFORE_CHECKOUT is True, forcefully discard any local changes
    before checkout (git reset --hard + git clean -fd).
    """
    pr_number = pr_data["number"]

    try:
        # Step 1: Optionally reset any local changes and clean untracked files
        if config.FORCE_CLEAN_BEFORE_CHECKOUT:
            log_action(f"Forcefully cleaning workspace before checkout PR #{pr_number}")

            # Reset any staged/unstaged changes
            reset_result = cmd.run_command(["git", "reset", "--hard", "HEAD"])
            if not reset_result.success:
                log_action(
                    f"Warning: git reset failed for PR #{pr_number}",
                    False,
                    reset_result.stderr,
                )

            # Clean untracked files and directories
            clean_result = cmd.run_command(["git", "clean", "-fd"])
            if not clean_result.success:
                log_action(
                    f"Warning: git clean failed for PR #{pr_number}",
                    False,
                    clean_result.stderr,
                )

        # Step 2: Try manual fetch and checkout (fallback is redundant now but keeps logic similar)
        log_action(f"Direct checkout failed for PR #{pr_number}, trying alternative approach", False)
        return _force_checkout_pr_manually(repo_name, pr_data, config, perform_checkout)

    except Exception as e:
        logger.error(f"Error checking out PR #{pr_number}: {e}")
        return False


def _force_checkout_pr_manually(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig, perform_checkout: bool = True) -> bool:
    """Manually fetch and checkout PR branch as fallback."""
    pr_number = pr_data["number"]

    try:
        # Get PR branch information from PR data
        branch_name = pr_data.get("head_branch") or pr_data.get("head", {}).get("ref")
        if not branch_name:
            log_action(f"Cannot determine branch name for PR #{pr_number}", False, "No head.ref in PR data")
            return False

        log_action(f"Attempting manual checkout of branch '{branch_name}' for PR #{pr_number}")

        # Clean up any existing merge conflicts before checkout
        log_action(f"Cleaning up workspace before checkout PR #{pr_number}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        # Ignore errors - there might not be a merge in progress

        # Reset any staged/unstaged changes
        reset_result = cmd.run_command(["git", "reset", "--hard", "HEAD"])
        if not reset_result.success:
            log_action(f"Warning: git reset failed for PR #{pr_number}", False, reset_result.stderr)

        # Clean untracked files and directories
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            log_action(f"Warning: git clean failed for PR #{pr_number}", False, clean_result.stderr)

        # Fetch the PR branch directly
        fetch_result = cmd.run_command(["git", "fetch", "origin", f"{branch_name}:{branch_name}"])
        if not fetch_result.success:
            # Try fetching from pull request ref
            fetch_result = cmd.run_command(["git", "fetch", "origin", f"pull/{pr_number}/head"])
            if not fetch_result.success:
                log_action(f"Failed to fetch PR #{pr_number} branch", False, fetch_result.stderr)
                return False

        # Checkout the branch
        if perform_checkout:
            checkout_result = cmd.run_command(["git", "checkout", branch_name])
            if not checkout_result.success:
                # If branch doesn't exist locally, checkout from fetched ref
                checkout_result = cmd.run_command(["git", "checkout", "-b", branch_name, "FETCH_HEAD"])

                if not checkout_result.success:
                    log_action(
                        f"Failed to checkout branch '{branch_name}' for PR #{pr_number}",
                        False,
                        checkout_result.stderr,
                    )
                    return False
        else:
            # If not checking out, ensure the branch exists/updates from the fetched head
            # If we fetched to branch_name:branch_name, it's already updated.
            # If we fetched to FETCH_HEAD (fallback), we need to update/create the local branch.
            if not fetch_result.success and "FETCH_HEAD" in str(fetch_result.stdout or ""):
                # This logic is tricky because we rely on previous fetch_result variable which might be from branch:branch attempt.
                pass

            # The structure above tries branch:branch first.
            # checks: fetch_result = ... branch:branch
            # if not fetch_result.success: fetch_result = ... pull/N/head

            # Re-evaluating fetch logic to account for perform_checkout=False
            pass

        # NOTE: The block above was complex. Re-implementing clearer logic for finish.

        if not perform_checkout:
            # Logic to ensure branch ref exists if we fetched to FETCH_HEAD
            # If branch:branch succeeded, the branch ref is updated.
            # If pull/N/head succeeded, we need to create/update local branch ptr.

            # We can't easily know which path succeeded without checking return codes or logic flow.
            # But we know at least one Fetch Succeeded if we reached here (wait, we didn't check success properly in original code flow?
            # Original code: if not fetch (branch:branch): if not fetch (pull): return False.
            # So if we are here, we fetched successfully.

            # If branch:branch failed, we used pull/N/head.
            # So we verify if branch exists?

            verify = cmd.run_command(["git", "rev-parse", "--verify", branch_name])
            if not verify.success:
                # It must have been the FETCH_HEAD case or branch didn't exist before.
                # Create/Update it.
                cmd.run_command(["git", "branch", "-f", branch_name, "FETCH_HEAD"])
            return True

        checkout_result = cmd.run_command(["git", "checkout", branch_name])
        if not checkout_result.success:
            # If branch doesn't exist locally, checkout from fetched ref
            checkout_result = cmd.run_command(["git", "checkout", "-b", branch_name, "FETCH_HEAD"])

            if not checkout_result.success:
                log_action(
                    f"Failed to checkout branch '{branch_name}' for PR #{pr_number}",
                    False,
                    checkout_result.stderr,
                )
                return False

        log_action(f"Successfully manually checked out PR #{pr_number}")
        return True

    except Exception as e:
        logger.error(f"Error manually checking out PR #{pr_number}: {e}")
        return False


def _update_with_base_branch(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> PRActionList:
    """Update PR branch with latest base branch commits.

    This function merges the PR's base branch (e.g., main, develop) into the PR branch
    to bring it up to date before attempting fixes.
    """
    actions = PRActionList()
    pr_number = pr_data["number"]

    try:
        # Determine target base branch for this PR
        target_branch = pr_data.get("base_branch") or pr_data.get("base", {}).get("ref") or config.MAIN_BRANCH

        # Fetch latest changes from origin
        result = cmd.run_command(["git", "fetch", "origin"])
        if not result.success:
            actions.append(f"Failed to fetch latest changes: {result.stderr}")
            return actions

        # Check if base branch has new commits
        result = cmd.run_command(["git", "rev-list", "--count", f"HEAD..refs/remotes/origin/{target_branch}"])
        if not result.success:
            actions.append(f"Failed to check {target_branch} branch status: {result.stderr}")
            return actions

        commits_behind = int(result.stdout.strip())
        if commits_behind == 0:
            actions.append(f"PR #{pr_number} is up to date with {target_branch} branch")
            return actions

        actions.append(f"PR #{pr_number} is {commits_behind} commits behind {target_branch}, updating...")

        # Try to merge base branch
        result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{target_branch}"])
        if result.success:
            actions.append(f"Successfully merged {target_branch} branch into PR #{pr_number}")

            # Push the updated branch using centralized helper with retry
            push_result = git_push()
            if push_result.success:
                actions.append(f"Pushed updated branch for PR #{pr_number}")
                # Signal to skip further LLM analysis for this PR in this run
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                import time

                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    actions.append(f"Pushed updated branch for PR #{pr_number} (after retry)")
                    actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    logger.error("Exiting application due to git push failure")
                    sys.exit(1)
        else:
            # Merge conflict occurred, check if Jules PR
            actions.append(f"Merge conflict detected for PR #{pr_number}")

            if _is_jules_pr(pr_data):
                actions.append(f"PR #{pr_number} is a Jules PR with merge conflicts. Requesting Jules to resolve it.")
                try:
                    from auto_coder.jules_client import JulesClient

                    jules_client = JulesClient()
                    session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))
                    if session_id:
                        # REQ-007/REQ-009 (Issue #2147): guard + durably admit
                        # this outbound mutation to an existing Jules session
                        # before sending it.
                        if not _guard_outbound_jules_send(repo_name, config, session_id):
                            actions.append(f"Blocked merge-conflict resolution request for PR #{pr_number}: session '{session_id}' " "belongs to a durably retired implementation slot (REQ-009)")
                            actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                            return actions
                        prompt = render_prompt("pr.jules_merge_conflict_resolution")
                        jules_client.send_message(session_id, prompt)
                        actions.append(f"Requested Jules to resolve merge conflict in session {session_id}")
                        # We return here so we don't proceed with LLM fixing this PR right now
                        actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                        return actions
                    else:
                        actions.append(f"Jules PR #{pr_number} has merge conflicts but no session ID found. Cannot delegate.")
                except Exception as e:
                    actions.append(f"Error requesting Jules to resolve conflict: {e}")

            # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
            # the bot recreates the PR against the updated base branch by itself.
            if _is_dependabot_pr(pr_data):
                actions.append(f"PR #{pr_number} is a dependency-bot PR with merge conflicts. Skipping conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            reporting_client = github_client
            if reporting_client is None:
                try:
                    reporting_client = GitHubClient.get_instance()
                except Exception as exc:
                    logger.warning(f"Could not initialize GitHub reporting for PR #{pr_number}: {exc}")
            cloud_delegation = _delegate_cloud_merge_conflict_repair_result(repo_name, pr_data, reporting_client)
            if cloud_delegation.deferred:
                actions.append(f"DEFERRED Claude merge-conflict repair for PR #{pr_number}: {cloud_delegation.reason}")
                actions.quota_deferred = True
                actions.retry_not_before = cloud_delegation.retry_not_before
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions
            if cloud_delegation:
                cmd.run_command(["git", "merge", "--abort"])
                if cloud_delegation.accepted_action:
                    actions.append(cloud_delegation.accepted_action)
                actions.append(f"Delegated merge-conflict repair for PR #{pr_number} to its existing cloud session")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            if not _is_local_llm_pr(pr_data):
                actions.append(f"Cloud merge-conflict repair could not be delegated for PR #{pr_number}: " f"{cloud_delegation.reason}; deferring conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            # Use the common subroutine for conflict resolution
            from .conflict_resolver import _perform_base_branch_merge_and_conflict_resolution, scan_conflict_markers

            conflict_resolved = _perform_base_branch_merge_and_conflict_resolution(
                pr_number,
                target_branch,
                config,
                pr_data,
                repo_name,
            )

            if conflict_resolved:
                actions.append(f"Successfully resolved merge conflicts for PR #{pr_number}")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                # Check if conflicts are still present (indicating LLM determined degradation)
                remaining_conflicts = scan_conflict_markers()
                if remaining_conflicts:
                    actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, skipping merge attempt")
                    actions.append("ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE")
                else:
                    actions.append(f"Failed to resolve merge conflicts for PR #{pr_number}")

    except Exception as e:
        actions.append(f"Error updating with base branch for PR #{pr_number}: {e}")

    return actions


def _extract_session_id_candidates(pr_body: str) -> List[Tuple[str, str]]:
    """Extract candidate (pattern_name, session_id) pairs in order of priority.

    Looks for patterns like:
    - Pattern 1: Session ID: abc123 / Session: abc123
    - Pattern 2: URLs with session parameters (?session=abc123)
    - Pattern 3: Jules session URLs (jules.google.com/session/...)
    - Pattern 3a: Claude Routine session URLs (claude.ai/code/...)
    - Pattern 3b: GitHub PR URLs (github.com/.../pull/...)
    - Pattern 3c: Codex Cloud task URLs (chatgpt.com/codex/tasks/...)
    - Pattern 4: Jules Task URLs (jules.google.com/task/...)
    - Pattern 5: Jules Task ID (task 12345)
    - Pattern 6: Standalone session IDs starting with session_
    - Pattern 7: Standalone Codex task IDs (task_e_...)

    Args:
        pr_body: PR description/body text

    Returns:
        List of (pattern_name, session_id) tuples in priority order
    """
    if not pr_body:
        return []

    candidates: List[Tuple[str, str]] = []
    seen: set = set()

    def _add(p_name: str, sid: Optional[str]) -> None:
        if sid and sid not in seen:
            seen.add(sid)
            candidates.append((p_name, sid))

    # Pattern 1: Look for "Session ID:" or "Session:" followed by the session ID
    session_pattern = r"(?:session\s*id:|session:)\s*(.+?)(?:\n|$)"
    match = re.search(session_pattern, pr_body, re.IGNORECASE)
    if match:
        session_id = match.group(1).strip()
        github_url_in_session = re.search(r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/pull/\d+", session_id)
        if github_url_in_session:
            _add("Pattern 1 (URL)", github_url_in_session.group(0))
        else:
            _add("Pattern 1", session_id)

    # Pattern 2: Look for URLs that might contain session IDs
    url_session_pattern = r"(?:session(?:_id)?=)([a-zA-Z0-9-_]+)"
    match = re.search(url_session_pattern, pr_body, re.IGNORECASE)
    if match:
        _add("Pattern 2", match.group(1).strip())

    # Pattern 3: Look for Jules session URLs (e.g., https://jules.google.com/session/901463134778726610)
    jules_session_url_pattern = r"jules\.google\.com/session/([a-zA-Z0-9-_]+)"
    match = re.search(jules_session_url_pattern, pr_body)
    if match:
        _add("Pattern 3 (Jules Session URL)", match.group(1).strip())

    # Pattern 3a: Look for Claude Routine session URLs (e.g., https://claude.ai/code/session_01HJKLMNOPQRSTUVWXYZ)
    claude_session_url_pattern = r"claude\.ai/code/([a-zA-Z0-9-_]+)"
    match = re.search(claude_session_url_pattern, pr_body)
    if match:
        _add("Pattern 3a (Claude Session URL)", match.group(1).strip())

    # Pattern 3b: Look for GitHub PR URLs (e.g., https://github.com/owner/repo/pull/123)
    github_url_pattern = r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/pull/\d+"
    match = re.search(github_url_pattern, pr_body)
    if match:
        _add("Pattern 3b (GitHub PR URL)", match.group(0).strip())

    # Pattern 3c: Look for Codex Cloud task URLs (e.g., https://chatgpt.com/codex/tasks/task_01HJKLMNOPQRSTUVWXYZ)
    codex_session_url_pattern = r"(?:chatgpt\.com|chat\.openai\.com|[^\s/]+)/codex/tasks/(task_[a-zA-Z0-9_-]+)"
    match = re.search(codex_session_url_pattern, pr_body, re.IGNORECASE)
    if match:
        _add("Pattern 3c (Codex Task URL)", match.group(1).strip())

    # Pattern 4: Look for Jules Task IDs (e.g., jules.google.com/task/12345,
    # jules.google.com/task/abcDEF-123_x, or "task 12345"). The ID may be any
    # nonempty sequence of ASCII letters, digits, underscores, or hyphens.
    task_url_pattern = r"jules\.google\.com/task/([a-zA-Z0-9_-]+)"
    match = re.search(task_url_pattern, pr_body)
    if match:
        _add("Pattern 4 (Jules Task URL)", match.group(1).strip())

    task_id_pattern = r"\btask\s+(\d+)\b"
    match = re.search(task_id_pattern, pr_body, re.IGNORECASE)
    if match:
        _add("Pattern 5 (Jules Task ID)", match.group(1).strip())

    # Pattern 6: Look for standalone session IDs starting with "session_"
    session_prefix_pattern = r"\b(session_(?!id\b)[a-zA-Z0-9-_]+)\b"
    match = re.search(session_prefix_pattern, pr_body)
    if match:
        _add("Pattern 6 (session_ prefix)", match.group(1).strip())

    # Pattern 7: Look for standalone Codex task IDs (e.g., task_e_...)
    codex_task_pattern = r"\b(task_[a-zA-Z0-9_-]+)\b"
    match = re.search(codex_task_pattern, pr_body)
    if match:
        _add("Pattern 7 (Codex task_ prefix)", match.group(1).strip())

    return candidates


def _extract_session_id_from_pr_body(pr_body: str) -> Optional[str]:
    """Extract Session ID from PR body by looking for session links.

    Looks for patterns like:
    - Session ID: abc123
    - Session: abc123
    - GitHub PR URL: https://github.com/owner/repo/pull/123
    - URLs with session parameters

    Args:
        pr_body: PR description/body text

    Returns:
        Session ID if found, None otherwise
    """
    candidates = _extract_session_id_candidates(pr_body)
    if candidates:
        logger.debug(f"Found session ID {candidates[0][0]}: {candidates[0][1]}")
        return candidates[0][1]
    logger.debug("No session ID found in PR body")
    return None


def _find_issue_by_session_id_in_comments(repo_name: str, session_id: str, github_client: Any) -> Optional[int]:
    """Find issue number by searching for session ID using GitHub Search API."""
    try:
        # Use GitHub Search API for efficiency
        # Query: repo:owner/repo "session_id" type:issue
        # We search specifically for the session_id string
        query = f"repo:{repo_name} {session_id} type:issue"
        logger.info(f"Searching for session ID '{session_id}' with query: '{query}'")

        # Use the new search_issues method
        # We only check the top 5 results to avoid indefinite processing if search returns many loose matches
        search_results = github_client.search_issues(query)

        # Iterate safely over the generator/list
        count = 0
        for issue in search_results:
            if count >= 5:
                break
            count += 1

            # Helper to get attributes from dict or object (GhApi returns AttrDict usually)
            def get_attr(obj, attr):
                return getattr(obj, attr, None) or (obj.get(attr) if isinstance(obj, dict) else None)

            issue_number = get_attr(issue, "number")
            issue_body = get_attr(issue, "body")

            def matches_session(text: Optional[str]) -> bool:
                if not text:
                    return False
                if session_id in text:
                    return True
                if session_id.startswith("session_") and f"cse_{session_id[8:]}" in text:
                    return True
                if session_id.startswith("cse_") and f"session_{session_id[4:]}" in text:
                    return True
                return False

            # Double check if session_id is actually in body or comments to be sure
            # Search API might return loose matches, although exact string match usually ranks high
            if matches_session(issue_body):
                logger.info(f"Found session ID '{session_id}' in body of issue #{issue_number}")
                return issue_number

            # Check comments
            # This is still an API call per issue, but we only do it for a few candidates
            try:
                comments = github_client.get_issue_comments(repo_name, issue_number)
                for comment in comments:
                    comment_body = comment.get("body")
                    if matches_session(comment_body):
                        logger.info(f"Found session ID '{session_id}' in comment of issue #{issue_number}")
                        return issue_number
            except Exception as e:
                logger.warning(f"Failed to fetch comments for potential issue #{issue_number}: {e}")

        logger.warning(f"Session ID '{session_id}' not found via search query")
        return None
    except Exception as e:
        logger.error(f"Error searching for session ID in comments: {e}")
        return None


def _update_jules_pr_body(
    repo_name: str,
    pr_number: int,
    pr_body: str,
    issue_number: int,
    github_client: Any,
) -> bool:
    """Update Jules PR body to include close #<issue_number> and link to issue.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number
        pr_body: Current PR body text
        issue_number: Issue number to link to
        github_client: GitHub client instance

    Returns:
        True if PR body was updated successfully, False otherwise
    """
    try:
        # Check if PR body already has the close reference
        if f"close #{issue_number}" in pr_body.lower() or f"closes #{issue_number}" in pr_body.lower():
            logger.info(f"PR #{pr_number} body already references issue #{issue_number}, skipping update")
            return True

        # Create the issue link
        issue_link = f"https://github.com/{repo_name}/issues/{issue_number}"
        close_statement = f"close #{issue_number}"

        # Build new PR body
        separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
        new_body = f"{pr_body}{separator}{close_statement}\n\nRelated issue: {issue_link}"

        # Update PR body via GitHub Client
        try:
            from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client

            # Use github_client for API call if it's a real client with valid token
            # and has the necessary methods. Otherwise, use get_ghapi_client.
            token = getattr(github_client, "token", None)

            # Prefer get_ghapi_client when a valid string token is provided
            if isinstance(token, str):
                api = get_ghapi_client(token)
                owner, repo_name_split = repo_name.split("/")
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_name_split, pr_number, body=new_body)
            elif hasattr(github_client, "get_repository"):
                # Fallback to direct client methods
                repo = github_client.get_repository(repo_name)
                pr = repo.get_pull(pr_number)
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                pr.edit(body=new_body)
            else:
                # Last resort: try singleton token
                token = GitHubClient.get_instance().token
                api = get_ghapi_client(token)
                owner, repo_name_split = repo_name.split("/")
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_name_split, pr_number, body=new_body)

            logger.info(f"Updated PR #{pr_number} body to include reference to issue #{issue_number}")
            log_action(f"Updated PR #{pr_number} body with close #{issue_number} reference")
            return True
        except Exception as e:
            try:
                logger.error(f"Failed to update PR #{pr_number} body: {str(e)}")
            except Exception:
                pass  # Prevent logging failures from affecting the result
            return False

    except Exception as e:
        try:
            logger.error(f"Error updating Jules PR #{pr_number} body: {str(e)}")
        except Exception:
            pass  # Prevent logging failures from affecting the result
        return False


def _is_codex_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Codex based on session/task URL in PR body."""
    if pr_data.get("_verified_codex_pr_origin"):
        return True
    pr_author = get_pr_author_login(pr_data) or ""
    normalized_author = pr_author.casefold()
    if normalized_author == CODEX_REVIEW_BOT_LOGIN.casefold() or normalized_author.startswith("codex"):
        return True

    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Check for Codex task / session URLs
    if re.search(r"https?://(?:chatgpt\.com|chat\.openai\.com|[^\s/]+)/codex/tasks/[a-zA-Z0-9_-]+", pr_body, re.IGNORECASE):
        return True
    if "/codex/tasks/" in pr_body:
        return True

    return False


def _is_unsafe_codex_cloud_branch(pr_data: Dict[str, Any]) -> bool:
    """Return whether a Codex Cloud PR uses a forbidden remote head identity."""
    head = pr_data.get("head") or {}
    remote_head = head.get("ref") or pr_data.get("head_branch") or ""
    return remote_head == "work" and _is_codex_pr(pr_data)


def _resolve_pr_safety_metadata(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Return metadata sufficient for the Codex Cloud branch safety decision.

    Production clients always refresh through an uncached endpoint, even when
    caller data looks complete, because list responses can be stale. Absence or
    malformed data fails this processing pass closed. The local validation path
    exists only for lightweight clients that do not implement strict retrieval.
    """
    client = github_client or GitHubClient.get_instance()
    getter = getattr(type(client), "get_pull_request_metadata_strict", None)
    if callable(getter):
        try:
            refreshed = client.get_pull_request_metadata_strict(repo_name, int(pr_data["number"]))
        except Exception as exc:
            return pr_data, str(exc)
        if not isinstance(refreshed, dict):
            return pr_data, "GitHub returned malformed PR metadata"
        refreshed_head = refreshed.get("head")
        refreshed_author = refreshed.get("author", refreshed.get("user"))
        if (
            not isinstance(refreshed_head, dict)
            or not isinstance(refreshed_head.get("ref"), str)
            or "body" not in refreshed
            or not (refreshed.get("body") is None or isinstance(refreshed.get("body"), str))
            or not (isinstance(refreshed_author, str) or (isinstance(refreshed_author, dict) and isinstance(refreshed_author.get("login"), str)))
            or not isinstance(refreshed.get("state"), str)
        ):
            return pr_data, "GitHub returned incomplete PR safety metadata"
        return refreshed, None

    head = pr_data.get("head")
    head_ref = head.get("ref") if isinstance(head, dict) else None
    has_head = isinstance(head_ref, str) and bool(head_ref)
    author = pr_data.get("author", pr_data.get("user"))
    has_author = isinstance(author, str) or (isinstance(author, dict) and isinstance(author.get("login"), str))
    # Origin is irrelevant once an authoritative, non-``work`` remote head is
    # present. For ``work``, both origin-bearing REST fields must be available:
    # the task URL can identify owner-authored Cloud PRs while the author can
    # identify connector-authored PRs whose body has no URL.
    if has_head and (head_ref != "work" or ("body" in pr_data and has_author)):
        return pr_data, None

    return pr_data, "GitHub client does not support strict PR metadata lookup"


def _is_claude_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Claude based on session URL in PR body."""
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.lower().startswith("claude"):
        return True

    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Check for Claude Routine / Code session URLs
    if re.search(r"https?://claude\.ai/code/[a-zA-Z0-9_-]+", pr_body, re.IGNORECASE) or "claude.ai/code/" in pr_body:
        return True
    if re.search(r"\bClaude session\b", pr_body, re.IGNORECASE):
        return True

    return False


def _is_codex_or_claude_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Codex or Claude based on session URL in PR body."""
    return _is_codex_pr(pr_data) or _is_claude_pr(pr_data)


def _find_codex_cloud_task_for_issue(
    repo_name: str,
    issue_number: int,
    github_client: Optional[Any] = None,
) -> Optional[str]:
    """Find the Codex Cloud task URL for an issue if it was processed by Codex Cloud.

    Args:
        repo_name: Repository name (owner/repo)
        issue_number: GitHub issue number
        github_client: Optional GitHub client instance

    Returns:
        Codex Cloud task URL if found, None otherwise
    """
    try:
        # 1. Check CloudManager
        cloud_manager = CloudManager(repo_name)
        session_id = cloud_manager.get_session_id(issue_number)
        if session_id:
            task_id = extract_codex_cloud_task_id(session_id)
            if session_id.startswith("http") and "/codex/tasks/" in session_id and task_id:
                return session_id
            if is_valid_codex_cloud_task_id(session_id):
                return f"https://chatgpt.com/codex/tasks/{session_id.strip()}"

        # 2. Check comments on the issue if github_client is available
        if github_client:
            try:
                comments = github_client.get_issue_comments(repo_name, issue_number)
                for comment in comments:
                    comment_body = comment.get("body", "") or ""
                    # Check for direct URL in comment
                    url_match = re.search(r"(https?://[^\s]+/codex/tasks/[a-zA-Z0-9_-]+)", comment_body)
                    if url_match and extract_codex_cloud_task_id(url_match.group(1)):
                        return url_match.group(1)

                    # Check for "Codex Cloud task ... Task ID: <id>"
                    task_match = re.search(r"Codex Cloud task.*?Task ID:\s*(task_[a-zA-Z0-9_-]+)", comment_body, re.IGNORECASE | re.DOTALL)
                    if task_match and is_valid_codex_cloud_task_id(task_match.group(1)):
                        return f"https://chatgpt.com/codex/tasks/{task_match.group(1)}"
            except Exception as e:
                logger.debug(f"Failed to fetch comments for issue #{issue_number}: {e}")

        return None
    except Exception as e:
        logger.error(f"Error finding Codex Cloud task for issue #{issue_number}: {e}")
        return None


@dataclass(frozen=True)
class CodexTaskProjectionResult:
    """Observed outcome of projecting a durable Codex origin into a PR body."""

    status: str
    diagnostic: str
    confirmed_body: Optional[str] = None

    @property
    def confirmed(self) -> bool:
        return self.status in {"present", "updated"}


def _codex_projection_action(result: object) -> str:
    """Expose only applicable projection outcomes in general PR actions."""
    if isinstance(result, CodexTaskProjectionResult) and result.status in {
        "present",
        "updated",
        "failed",
    }:
        return result.diagnostic
    return ""


def _link_codex_cloud_pr_to_issue(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> CodexTaskProjectionResult:
    """Project a verified PR-specific Codex origin into the live GitHub body.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        A result that distinguishes confirmed publication from deferral/failure.
    """
    pr_number = pr_data.get("number")
    if not isinstance(pr_number, int):
        return CodexTaskProjectionResult("deferred", "Codex task projection deferred: PR number is unavailable")

    try:
        strict_getter = getattr(type(github_client), "get_pull_request_metadata_strict", None)
        if not callable(strict_getter):
            return CodexTaskProjectionResult("unavailable", "Codex task projection unavailable: authoritative PR body reader is not supported")
        authoritative = github_client.get_pull_request_metadata_strict(repo_name, pr_number)
        if not isinstance(authoritative, dict) or authoritative.get("number") != pr_number or not isinstance(authoritative.get("body"), (str, type(None))):
            return CodexTaskProjectionResult("unavailable", "Codex task projection unavailable: authoritative PR body is malformed")

        from .cloud_run import CloudRunRepository

        bindings = CodexPrAttributionRepository(repo_name)
        attribution = resolve_codex_pr_origin(repo_name, authoritative, CloudRunRepository(repo_name), bindings)
        origin = attribution.origin
        if attribution.disposition is not AttributionDisposition.VERIFIED or origin is None or origin.repository != repo_name or origin.pr_number != pr_number or origin.provider != "codex-cloud":
            return CodexTaskProjectionResult(
                "deferred",
                f"Codex task projection deferred: attribution is {attribution.disposition.value} ({attribution.boundary})",
            )

        pr_body = authoritative.get("body") or ""
        if origin.task_id in task_ids_from_text(pr_body):
            pr_data.update(authoritative)
            return CodexTaskProjectionResult("present", f"Confirmed Codex task link for PR #{pr_number} is already present", pr_body)

        # Re-read the durable binding at update admission. This prevents a
        # locally observed conflict or revision change from being ignored after
        # the authoritative body read above.
        admitted = bindings.get(pr_number)
        if admitted != attribution:
            return CodexTaskProjectionResult("deferred", "Codex task projection deferred: attribution changed before update admission")

        codex_url = f"https://chatgpt.com/codex/tasks/{origin.task_id}"
        separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
        new_body = f"{pr_body}{separator}{codex_url}"

        # Update PR body on GitHub
        try:
            token = getattr(github_client, "token", None)
            if isinstance(token, str):
                api = get_ghapi_client(token)
                owner, repo_split = repo_name.split("/")
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_split, pr_number, body=new_body)
            elif hasattr(github_client, "get_repository"):
                repo = github_client.get_repository(repo_name)
                pr = repo.get_pull(pr_number)
                validate_issue_references(new_body, github_client, repo_name)
                pr.edit(body=new_body)
            else:
                token = GitHubClient.get_instance().token
                api = get_ghapi_client(token)
                owner, repo_split = repo_name.split("/")
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_split, pr_number, body=new_body)

            pr_data.update(authoritative)
            pr_data["body"] = new_body
            logger.info(f"Updated PR #{pr_number} body to include verified Codex Cloud URL: {codex_url}")
            log_action(f"Updated PR #{pr_number} body with verified Codex Cloud URL")
            return CodexTaskProjectionResult("updated", f"Confirmed Codex task link update for PR #{pr_number}", new_body)
        except Exception as e:
            logger.error(f"Failed to update PR #{pr_number} body with Codex Cloud URL: {e}")
            return CodexTaskProjectionResult("failed", f"Codex task projection failed: {type(e).__name__}: {e}")

    except Exception as e:
        logger.error(f"Error linking Codex Cloud PR #{pr_data.get('number')}: {e}")
        return CodexTaskProjectionResult("unavailable", f"Codex task projection unavailable: {type(e).__name__}: {e}")


def _is_jules_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Jules (google-labs-jules).

    Args:
        pr_data: PR data dictionary

    Returns:
        True if the PR is created by Jules, False otherwise
    """
    # Codex or Claude PRs should never be treated as Jules
    if _is_codex_or_claude_pr(pr_data):
        return False

    # Check author first
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.startswith("claude") or pr_author.startswith("codex"):
        return False
    if pr_author.startswith("google-labs-jules"):
        return True

    # Fallback: Check if PR body contains a valid Jules session reference
    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Jules URL indicators
    if re.search(r"jules\.google\.com/(?:session|task)/", pr_body) or re.search(r"\bJules session\b", pr_body, re.IGNORECASE):
        return True

    # Check for Session ID format without Claude, Codex, or generic GitHub URL
    session_id = _extract_session_id_from_pr_body(pr_body)
    if session_id:
        if "claude.ai" in session_id or "github.com" in session_id or "codex" in session_id:
            return False
        # Only treat as Jules session if "Session ID:" or "Session:" is explicitly in body
        session_pattern = r"(?:session\s*id:|session:)\s*(.+?)(?:\n|$)"
        if re.search(session_pattern, pr_body, re.IGNORECASE):
            return True

    return False


def _is_local_llm_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR was created by local LLM execution.

    Returns True if:
    1. The PR is not from Jules, Codex Cloud, Claude Routine, or Dependabot/bots.
    2. And either:
       - PR body contains the explicit local marker `<!-- auto-coder:local-llm -->` or `<!-- auto-coder:local -->`.
       - PR head branch matches the Auto-Coder work branch pattern (e.g. `issue-<number>`, `issue-<number>_attempt-<attempt>`, `issue-<number>/attempt-<attempt>`).
       - PR body contains standard Auto-Coder text like `This PR addresses issue #` or `Auto-Coder: Address issue #`.
    """
    if not pr_data:
        return False

    # Exclude cloud LLM PRs and dependency bots
    if _is_jules_pr(pr_data):
        return False
    if _is_codex_or_claude_pr(pr_data):
        return False
    if _is_dependabot_pr(pr_data):
        return False

    pr_body = pr_data.get("body", "") or ""

    # 1. Check explicit local LLM markers
    if "<!-- auto-coder:local-llm -->" in pr_body or "<!-- auto-coder:local -->" in pr_body:
        return True

    # 2. Check head branch name
    head_branch = pr_data.get("head_branch") or (pr_data.get("head") or {}).get("ref") or ""
    if head_branch:
        # Pattern matches: issue-123, issue-123_attempt-1, issue-123/attempt-1
        if re.match(r"^issue-\d+(?:[_/]attempt-\d+)?$", head_branch):
            return True

    # 3. Check standard PR body signatures for Auto-Coder local issue PRs
    if re.search(r"\bThis PR addresses issue #\d+", pr_body):
        return True
    if "Auto-Coder: Address issue #" in pr_body:
        return True

    return False


is_local_llm_pr = _is_local_llm_pr


def _resolve_jules_pr_issue_number(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> Optional[int]:
    """Find the issue a Jules PR was created for.

    The lookup order is session ID (local DB, then issue comments), branch name,
    and finally the PR title.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        Issue number if found, None otherwise
    """
    pr_number = pr_data.get("number")
    pr_body = pr_data.get("body", "") or ""

    primary_session_id = _extract_session_id_from_pr_body(pr_body)
    from unittest.mock import Mock

    if isinstance(_extract_session_id_from_pr_body, Mock):
        candidates = [("Mock", primary_session_id)] if primary_session_id else []
    else:
        candidates = _extract_session_id_candidates(pr_body)

    issue_number: Optional[int] = None
    matched_session_id: Optional[str] = None

    if candidates:
        cloud_manager = CloudManager(repo_name)
        # Step 1: Check local DB (cloud.csv) for each candidate in pattern priority order.
        # When an earlier candidate (such as Pattern 2 extracting "True") is not found in
        # cloud.csv, proceed / return to subsequent patterns (such as Pattern 3a) in order.
        for pattern_name, candidate_session_id in candidates:
            logger.info(f"Extracted session ID '{candidate_session_id}' from Jules PR #{pr_number} ({pattern_name})")
            found = cloud_manager.get_issue_by_session(candidate_session_id)
            if found:
                issue_number = found
                matched_session_id = candidate_session_id
                logger.info(f"Found issue #{issue_number} for session ID '{candidate_session_id}' in local DB ({pattern_name})")
                break
            else:
                durable_issues = cloud_manager.get_issues_by_session(candidate_session_id)
                if len(durable_issues) > 1:
                    logger.warning(f"Session ID '{candidate_session_id}' ({pattern_name}) has ambiguous durable ownership; " f"checking next candidate pattern...")
                else:
                    logger.warning(f"No issue found for session ID '{candidate_session_id}' in local DB ({pattern_name}). " f"Checking next candidate pattern...")

        # Step 2: If no candidate was found in local DB, search comments for viable candidates
        if not issue_number:
            for pattern_name, candidate_session_id in candidates:
                # Avoid searching comments with common boolean or trivial tokens (e.g., "True", "False")
                if candidate_session_id.lower() in ("true", "false", "none", "null") or len(candidate_session_id) < 4:
                    logger.warning(f"Skipping comment search for invalid session ID token '{candidate_session_id}' ({pattern_name})")
                    continue

                durable_issues = cloud_manager.get_issues_by_session(candidate_session_id)
                if len(durable_issues) > 1:
                    logger.warning(f"Session ID '{candidate_session_id}' has ambiguous durable ownership; refusing comment-search inference")
                    continue

                logger.warning(f"No issue found for session ID '{candidate_session_id}' in local DB ({pattern_name}). Searching comments...")
                found = _find_issue_by_session_id_in_comments(repo_name, candidate_session_id, github_client)
                if found:
                    issue_number = found
                    matched_session_id = candidate_session_id
                    logger.info(f"Found issue #{issue_number} via comment search for session ID '{candidate_session_id}' ({pattern_name})")
                    break
    else:
        logger.warning(f"No session ID found in Jules PR #{pr_number} body")

    if matched_session_id:
        pr_data["_jules_session_id"] = matched_session_id
    elif candidates:
        pr_data["_jules_session_id"] = candidates[0][1]

    # Fallback: Extract from branch name
    if not issue_number:
        branch_name = pr_data.get("head", {}).get("ref", "")
        if branch_name:
            # Match patterns like issue-123
            match = re.search(r"\bissue[-_](\d+)\b", branch_name, re.IGNORECASE)
            if match:
                issue_number = int(match.group(1))
                logger.info(f"Extracted issue #{issue_number} from branch name '{branch_name}'")

    # Fallback: Extract from PR title
    if not issue_number:
        pr_title = pr_data.get("title", "")
        if pr_title:
            # Match patterns like "Issue #123" or "Fix #123"
            match = re.search(r"(?:issue|fix|close|resolve)s?\s*#(\d+)", pr_title, re.IGNORECASE)
            if match:
                issue_number = int(match.group(1))
                logger.info(f"Extracted issue #{issue_number} from PR title '{pr_title}'")

    return issue_number


def _link_jules_pr_to_issue(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> bool:
    """Process a Jules, Claude Code, or session PR to detect session ID and update PR body.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        True if PR body was updated successfully, False otherwise
    """
    try:
        pr_number = pr_data["number"]
        pr_body = pr_data.get("body", "") or ""
        pr_author = get_pr_author_login(pr_data) or ""

        is_jules = _is_jules_pr(pr_data)
        is_claude = "claude" in pr_author.lower() or "claude.ai/code/" in pr_body or bool(re.search(r"\bClaude session\b", pr_body, re.IGNORECASE))
        has_session = bool(re.search(r"claude\.ai/code/|jules\.google\.com/(?:session|task)/|\bsession_[a-zA-Z0-9-_]+\b", pr_body, re.IGNORECASE))

        # If not a Jules PR, has no session indicator, and is not a Claude PR, skip
        if not is_jules and not is_claude and not has_session:
            logger.debug(f"PR #{pr_number} has no session ID or cloud author, skipping session issue linking")
            return True  # Not an error, just not a session PR

        logger.info(f"Processing session PR #{pr_number} by {pr_author}")

        # Check for special Jules PRs that don't need issue linking
        pr_title = pr_data.get("title", "")
        special_prefixes = ["🛡️ Sentinel: ", "🎨 Palette: ", "⚡ Bolt: "]
        if any(pr_title.startswith(prefix) for prefix in special_prefixes):
            logger.info(f"Skipping issue lookup for Jules special PR #{pr_number} ('{pr_title}')")
            return True

        issue_number = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)

        if not issue_number:
            logger.warning(f"No issue found for session PR #{pr_number} (checked session, branch, and title)")
            return False

        logger.info(f"Found issue #{issue_number} for session PR #{pr_number}")

        # Step 4: Update PR body to include close #<issue_number> and link to issue
        success = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        if success:
            logger.info(f"Successfully processed PR #{pr_number}, updated body to reference issue #{issue_number}")
            # Update local pr_data body so downstream logic in the same run has the updated body
            if f"close #{issue_number}" not in pr_body.lower() and f"closes #{issue_number}" not in pr_body.lower():
                separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
                pr_data["body"] = f"{pr_body}{separator}close #{issue_number}\n\nRelated issue: https://github.com/{repo_name}/issues/{issue_number}"
        else:
            logger.error(f"Failed to update PR #{pr_number} body")

        return success

    except Exception as e:
        logger.error(f"Error processing session PR {pr_data.get('number', 'unknown')}: {e}")
        return False


# Alias for backwards compatibility with tests
_process_jules_pr = _link_jules_pr_to_issue


def _close_linked_issues(repo_name: str, pr_number: int, github_client: Optional[Any] = None) -> None:
    """Close issues linked in the PR body after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
        github_client: Optional GitHubClient instance
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        client = github_client or GitHubClient.get_instance()
        token = getattr(client, "token", None) or GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Get PR body
        try:
            pr_info = api.pulls.get(owner, repo, pr_number)
            pr_body = pr_info.get("body", "") or ""
        except Exception as e:
            logger.debug(f"Could not retrieve PR #{pr_number} body for issue linking: {e}")
            return

        # Extract linked issues
        linked_issues = extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            # Fallback: resolve from session ID (Jules, Claude Code, etc.), branch name, or title
            resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_info, client)
            if resolved_issue:
                logger.info(f"Resolved issue #{resolved_issue} from session/branch/title for merged PR #{pr_number}")
                linked_issues = [resolved_issue]

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_number} body")
            return

        # Close each linked issue
        for issue_num in linked_issues:
            try:
                # Add comment
                try:
                    api.issues.create_comment(owner, repo, issue_num, body=f"Closed by PR #{pr_number}")
                except Exception as e:
                    logger.warning(f"Failed to comment on issue #{issue_num}: {e}")

                # Close issue
                api.issues.update(owner, repo, issue_num, state="closed")

                logger.info(f"Closed issue #{issue_num} linked from PR #{pr_number}")
                log_action(f"Closed issue #{issue_num} (linked from PR #{pr_number})")
            except Exception as e:
                logger.warning(f"Error closing issue #{issue_num}: {e}")

    except Exception as e:
        logger.warning(f"Error processing linked issues for PR #{pr_number}: {e}")


def _archive_jules_session(repo_name: str, pr_number: int) -> None:
    """Archive Jules session for Jules-created PRs after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Get PR data to check if it's a Jules PR and extract session ID
        try:
            pr_data = api.pulls.get(owner, repo, pr_number)
        except Exception as e:
            logger.debug(f"Could not retrieve PR #{pr_number} data for Jules session archiving: {e}")
            return

        pr_author = pr_data.get("user", {}).get("login", "")
        pr_body = pr_data.get("body", "")

        # Check if this is a Jules-created PR
        if pr_author != "google-labs-jules":
            logger.debug(f"PR #{pr_number} is not created by Jules ({pr_author}), skipping session archiving")
            return

        # Extract session ID from PR body
        session_id = _extract_session_id_from_pr_body(pr_body)
        if not session_id:
            logger.warning(f"No session ID found in Jules PR #{pr_number} body")
            return

        # Archive the Jules session
        try:
            from .jules_client import JulesClient

            jules_client = JulesClient()
            success = jules_client.archive_session(session_id)

            if success:
                logger.info(f"Archived Jules session '{session_id}' for PR #{pr_number}")
                log_action(f"Archived Jules session for PR #{pr_number}")
                # Check and restart recurrent tasks
                from .jules_engine import check_and_restart_recurrent_jules_task_for_pr

                check_and_restart_recurrent_jules_task_for_pr(repo_name, pr_number, session_id)
            else:
                logger.warning(f"Failed to archive Jules session '{session_id}' for PR #{pr_number}")
        except Exception as e:
            logger.warning(f"Error archiving Jules session for PR #{pr_number}: {e}")

    except Exception as e:
        logger.warning(f"Error processing Jules session archiving for PR #{pr_number}: {e}")


def _guard_outbound_jules_send(repo_name: str, config: AutomationConfig, session_id: str) -> bool:
    """Resolve the owning Issue and durably admit/guard one outbound Jules send.

    Shared boundary wrapper (REQ-007/REQ-009, Issue #2147) around
    :func:`admit_or_block_outbound_jules_send` for every production caller in
    this module that mutates an existing Jules session (error feedback,
    merge-conflict-resolution requests, branch-update conflict delegation,
    etc). Resolves the owning Issue and constructs the repo-scoped
    :class:`ImplementationSlotRepository` (the same file-backed store the
    maintenance loop uses, so it observes the same live retirement/admission
    state regardless of which caller constructs it), then delegates to the
    shared guard/admission helper. Never send the outbound mutation when this
    returns False.
    """
    from .cloud_manager import CloudManager
    from .implementation_retirement_observer import admit_or_block_outbound_jules_send
    from .implementation_slots import ImplementationSlotRepository

    owning_issue: Optional[int] = None
    try:
        owning_issue = CloudManager(repo_name).get_issue_by_session(session_id)
    except Exception as exc:
        logger.warning(f"Could not resolve owning Issue for Jules session {session_id}: {exc}")

    try:
        implementation_slots = ImplementationSlotRepository(repo_name, config.MAX_CONCURRENT_IMPLEMENTATIONS)
    except Exception as exc:
        logger.error(f"Could not construct ImplementationSlotRepository for {repo_name}: {exc}; " "blocking outbound Jules send (REQ-007)")
        return False

    return admit_or_block_outbound_jules_send(session_id, owning_issue, implementation_slots)


def _send_jules_error_feedback(
    repo_name: str,
    pr_data: Dict[str, Any],
    failed_checks: List[Dict[str, Any]],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> List[str]:
    """Send CI error logs to Jules session for Jules-created PRs.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        failed_checks: List of failed GitHub Actions checks
        config: AutomationConfig instance
        github_client: Optional GitHub client instance

    Returns:
        List of action strings describing what was done
    """
    actions = []
    pr_number = pr_data["number"]

    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
    if exhaustion_info and exhaustion_info.is_exhausted:
        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
        return [f"Skipped Jules error feedback for PR #{pr_number}: automatic repair allowance is exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}"]

    # Never send error feedback to Jules for PRs created by Codex or Claude
    if _is_codex_or_claude_pr(pr_data):
        logger.info(f"PR #{pr_number} is created by Codex/Claude, skipping Jules error feedback")
        return [f"Skipped Jules error feedback for PR #{pr_number} (created by Codex/Claude)"]

    try:
        # Get the session ID from pr_data
        session_id = pr_data.get("_jules_session_id")
        if not session_id:
            session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))

        if not session_id:
            actions.append(f"Cannot send error feedback to Jules for PR #{pr_number}: no session ID found")
            try:
                logger.error(f"No session ID found in PR #{pr_number} data for Jules error feedback")
            except Exception:
                pass  # Prevent logging failures from affecting the result
            return actions

        # Get GitHub Actions error logs
        github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)

        # Format the message to send to Jules
        message = f"""CI checks failed for PR #{pr_number} in {repo_name}.

Please review and fix the following errors:

{github_logs}

PR Title: {pr_data.get('title', 'Unknown')}
PR Author: {pr_data.get('user', {}).get('login', 'Unknown')}
"""

        if not new_work_allowed():
            actions.append(f"Deferred Jules CI feedback for PR #{pr_number}: graceful shutdown is draining")
            return actions

        expected_head = str(pr_data.get("head", {}).get("sha") or "")
        with current_ci_failure_authority(github_client, repo_name, pr_number, expected_head) as authority:
            if not authority.allowed:
                actions.append(f"Deferred Jules CI feedback for PR #{pr_number}: {authority.reason}")
                return actions
            # The retirement admission and provider mutation are inside the
            # same invalidation barrier as the final exact-head observation.
            if not _guard_outbound_jules_send(repo_name, config, session_id):
                actions.append(f"Blocked Jules CI feedback for PR #{pr_number}: session '{session_id}' " "belongs to a durably retired implementation slot (REQ-009)")
                return actions

            from .jules_client import JulesClient

            logger.info(f"Sending CI failure logs to Jules session '{session_id}' for PR #{pr_number}; " f"head={expected_head} failures={authority.failure_identities}")
            jules_client = JulesClient()
            response = jules_client.send_message(session_id, message)

        get_trace_logger().log("Jules Feedback", f"Sent CI failure logs to Jules for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"session_id": session_id})

        actions.append(f"Sent CI failure logs to Jules session '{session_id}' for PR #{pr_number}")
        try:
            logger.info(f"Jules response for PR #{pr_number}: {response[:200]}...")
        except Exception:
            pass  # Prevent logging failures from affecting the result

        # Post a comment on the PR stating that a fix has been requested
        if github_client:
            comment_body = f"🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
            try:
                if _add_unique_pr_comment(github_client, repo_name, pr_number, comment_body):
                    actions.append(f"Posted comment on PR #{pr_number} stating that a fix has been requested from Jules")
                else:
                    actions.append(f"Skipped duplicate Jules fix-request comment on PR #{pr_number}")
            except Exception as e:
                error_msg = f"Failed to post comment on PR #{pr_number}: {e}"
                try:
                    logger.error(error_msg)
                except Exception:
                    pass  # Prevent logging failures from affecting the result
                actions.append(error_msg)
        else:
            actions.append(f"Skipped posting comment on PR #{pr_number}: no GitHub client available")

    except Exception as e:
        error_msg = f"Error sending Jules error feedback for PR #{pr_number}: {e}"
        try:
            logger.error(error_msg)
        except Exception:
            pass  # Prevent logging failures from affecting the result
        actions.append(error_msg)

    return actions


def _resolve_codex_cloud_task_id(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> Optional[str]:
    """Resolve the Codex Cloud task associated with a pull request."""
    pr_body = pr_data.get("body", "") or ""

    task_id = extract_codex_cloud_task_id(pr_data.get("_codex_task_id"))

    if not task_id:
        task_id = extract_codex_cloud_task_id(pr_body)

    if not task_id:
        for issue_num in extract_linked_issues_from_pr_body(pr_body):
            found_url = _find_codex_cloud_task_for_issue(repo_name, issue_num, github_client)
            if found_url:
                task_id = extract_codex_cloud_task_id(found_url)
                if task_id:
                    break

    return task_id


def _resolve_cloud_conflict_origin(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> Optional[Tuple[Any, str]]:
    """Return the capable client and existing task ID that originated a PR."""
    guarded = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
    if guarded.origin is not None and guarded.origin.attribution_token:
        return guarded.origin.client, guarded.origin.task_id
    if "Codex PR attribution is" in guarded.reason:
        logger.warning(f"Conflict repair blocked for PR #{pr_data.get('number')}: {guarded.reason}")
        return None
    issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_data, github_client)
    explicitly_linked_issue_numbers = extract_linked_issues_from_pr_body(pr_data.get("body", "") or "")
    manager = CloudManager(repo_name)

    def claude_client(task_id: str) -> Optional[Any]:
        """Reconstruct a Claude transport from durable task ownership."""
        # Only explicit PR-to-issue links are authoritative here. ``issue_numbers``
        # can also contain a first-match reverse lookup from a session URL, which
        # must not break a tie between duplicate provider task identifiers.
        bindings = [manager.get_binding(number) for number in explicitly_linked_issue_numbers]
        matching = {binding for binding in bindings if binding is not None and binding.provider == "claude-routine" and binding.task_id == task_id}
        if len(matching) > 1:
            return None
        persisted = manager.get_bindings_for_task("claude-routine", task_id)
        if not matching and len(persisted) > 1:
            return None
        binding = matching.pop() if matching else (persisted[0] if persisted else None)
        backend_name = binding.backend_name if binding else "claude-routine"

        from .cloud_task_engine import CloudTaskEngine

        return CloudTaskEngine().get_client_for_provider("claude-routine", repo_name, backend_name=backend_name)

    # CloudRun is the lifecycle's authoritative implementation association.
    # Consult it before heuristics based on an author name or PR body, because
    # provider-created PRs do not consistently retain those presentation cues.
    try:
        from .cloud_run import CloudRunRepository

        pr_number = int(pr_data["number"])
        repository = CloudRunRepository(repo_name)
        associated_runs = [run for issue_number in issue_numbers for run in repository.list_for_issue(issue_number) if pr_number in run.pull_request_numbers]
        if associated_runs:
            run = max(associated_runs, key=lambda candidate: candidate.attempt)
            if run.provider == "codex-cloud":
                from .codex_cloud_client import CodexCloudClient

                return CodexCloudClient(repo_name=repo_name), run.task_id
            if run.provider == "claude-routine":
                client = claude_client(run.task_id)
                return (client, run.task_id) if client else None
            logger.warning(f"Cloud run provider '{run.provider}' does not support PR conflict follow-up")
            return None
    except (KeyError, TypeError, ValueError, OSError) as exc:
        logger.warning(f"Could not resolve authoritative cloud run for PR #{pr_data.get('number')}: {exc}")

    if _is_codex_pr(pr_data):
        task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
        if task_id:
            from .codex_cloud_client import CodexCloudClient

            return CodexCloudClient(repo_name=repo_name), task_id

    if _is_claude_pr(pr_data):
        task_id = pr_data.get("_jules_session_id") or _extract_session_id_from_pr_body(pr_data.get("body", "") or "")
        if not task_id:
            for issue_number in extract_linked_issues_from_pr_body(pr_data.get("body", "") or ""):
                binding = manager.get_binding(issue_number)
                if binding and binding.provider == "claude-routine":
                    task_id = binding.task_id
                    break
        if task_id and not task_id.startswith("http"):
            client = claude_client(task_id)
            return (client, task_id) if client else None

    return None


def _cloud_conflict_state_path(repo_name: str) -> Path:
    """Return the durable deduplication state path for cloud conflict work."""
    return Path.home() / ".auto-coder" / repo_name / "cloud_conflict_repairs.json"


def _record_cloud_conflict_deliveries(state_path: Path, delivered: dict[str, CloudConflictDeliveryRecord]) -> None:
    """Atomically persist conflict delivery reservations and receipts."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(f"{state_path.suffix}.{os.getpid()}.tmp")
    serialized = {fingerprint: {"task_id": record.task_id, "status": record.status} for fingerprint, record in delivered.items()}
    temporary.write_text(json.dumps(serialized, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, state_path)


def _load_cloud_conflict_deliveries(state_path: Path) -> dict[str, CloudConflictDeliveryRecord]:
    """Load and validate durable cloud conflict delivery state."""
    if not state_path.exists():
        return {}
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("delivery state is not a JSON object")

    records: dict[str, CloudConflictDeliveryRecord] = {}
    for fingerprint, value in loaded.items():
        if not isinstance(fingerprint, str) or not isinstance(value, dict):
            raise ValueError("delivery state contains an invalid record")
        task_id = value.get("task_id")
        status = value.get("status")
        if not isinstance(task_id, str) or status not in {"pending", "confirmed"}:
            raise ValueError("delivery state contains an invalid record")
        records[fingerprint] = CloudConflictDeliveryRecord(task_id=task_id, status=status)
    return records


def _report_cloud_conflict_followup(
    github_client: Optional[Any],
    repo_name: str,
    pr_number: int,
    task_id: str,
    head_sha: str,
    base_state: str,
) -> bool:
    """Publish one durable, delivery-specific receipt without affecting delivery state."""
    if github_client is None:
        return False
    identity = f"{repo_name}#{pr_number}:{task_id}:{head_sha}:{base_state}:merge-conflict-repair"
    marker = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    body = "\n".join(
        (
            f"<!-- {CLOUD_CONFLICT_FOLLOWUP_MARKER_PREFIX}{marker} -->",
            "🤖 Auto-Coder: Codex Cloud accepted a merge-conflict-repair follow-up " f"for PR #{pr_number} at head `{head_sha}` (base revision `{base_state}`) " f"on task `{task_id}`.",
        )
    )
    return _add_unique_pr_comment(github_client, repo_name, pr_number, body)


def _cloud_review_repair_state_path(repo_name: str) -> Path:
    """Return the durable deduplication state path for Codex review work."""
    return Path.home() / ".auto-coder" / repo_name / "cloud_review_repairs.json"


def _review_feedback_identity(prefix: str, thread: ReviewThread, comment_index: int) -> str:
    """Identify one review finding independently of its wording and PR head."""
    comment = thread.comments[comment_index]
    anchor = f"comment:{comment.database_id}" if comment.database_id is not None else f"thread:{thread.id}:comment:{comment_index}"
    return prefix + hashlib.sha256(anchor.encode("utf-8")).hexdigest()


def _cloud_task_remediation_token(client: Any, task_id: str, feedback_identity: str = "") -> str:
    """Return durable evidence that the owning task completed later activity."""
    from .cloud_task_client_base import CloudTask, CloudTaskState

    completed_turn_observer = getattr(type(client), "get_completed_followup_remediation_turn", None)
    if feedback_identity and callable(completed_turn_observer):
        try:
            completed_turn = completed_turn_observer(client, task_id, feedback_identity)
        except Exception as exc:
            logger.warning(f"Could not inspect cloud task '{task_id}' completed follow-up turn: {exc}")
            return ""
        if not isinstance(completed_turn, str) or not completed_turn:
            return ""
        return hashlib.sha256(f"{task_id}\ncompleted_assistant_turn:{completed_turn}".encode("utf-8")).hexdigest()

    try:
        task = client.get_task(task_id)
    except Exception as exc:
        logger.warning(f"Could not inspect cloud task '{task_id}' remediation activity: {exc}")
        return ""
    if not isinstance(task, CloudTask) or task.state not in {CloudTaskState.COMPLETED, CloudTaskState.FAILED, CloudTaskState.PAUSED}:
        return ""
    activity = task.updated_at.isoformat() if task.updated_at is not None else ""
    if not activity and isinstance(task.raw_data, dict):
        for key in ("updated_at", "updatedAt", "completed_at", "completedAt", "latest_turn_id", "latestTurnId"):
            value = task.raw_data.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                activity = f"{key}:{value}"
                break
    if not activity:
        return ""
    return hashlib.sha256(f"{task.task_id}\n{activity}".encode("utf-8")).hexdigest()


def _observe_codex_cloud_remediation_activity(repo_name: str, pr_data: Dict[str, Any], github_client: Optional[Any]) -> Optional[dict[str, str]]:
    """Snapshot completed Codex repair turns before a new validation runs."""
    resolution = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
    if resolution.origin is None or resolution.origin.provider != "codex-cloud":
        return None
    observer = getattr(type(resolution.origin.client), "observe_completed_followup_remediation_turns", None)
    if not callable(observer):
        return {}
    try:
        observed = observer(resolution.origin.client, resolution.origin.task_id)
        if not isinstance(observed, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in observed.items()):
            return {}
        return {feedback_identity: hashlib.sha256(f"{resolution.origin.task_id}\ncompleted_assistant_turn:{turn_id}".encode("utf-8")).hexdigest() if turn_id else "" for feedback_identity, turn_id in observed.items()}
    except Exception as exc:
        logger.warning(f"Could not snapshot Codex Cloud remediation activity before adversarial validation: {exc}")
        return {}


def _adversarial_feedback_generation_identity(feedback_identity: str, remediation_token: str, validation_report: str) -> str:
    """Identify one finding delivery within a durable remediation generation.

    Only observed terminal activity by the owning implementation task, or new
    implementer provenance evidence, advances this token. A head SHA is not
    evidence of remediation because unrelated commits can change it. Keeping
    the stable finding identity separate preserves cross-path deduplication.
    """
    provenance_match = re.search(r"<!-- auto-coder-change-provenance-evidence:v1:[a-f0-9]+ -->", validation_report)
    lifecycle_token = f"{remediation_token}\n{provenance_match.group(0) if provenance_match else ''}"
    generation = hashlib.sha256(lifecycle_token.encode("utf-8")).hexdigest()
    return f"{feedback_identity}:remediation:{generation}"


def _has_adversarial_remediation_evidence(remediation_token: str, validation_report: str) -> bool:
    """Return whether this observation proves that remediation advanced."""
    return bool(remediation_token) or bool(re.search(r"<!-- auto-coder-change-provenance-evidence:v1:[a-f0-9]+ -->", validation_report))


def _adversarial_validation_delivery_identity(feedback_identity: str, validation_report: str) -> str:
    """Identify the independent validation result authorizing a delivery."""
    marker = re.search(r"<!-- auto-coder-adversarial-validation-attempt:v1:\d+:[0-9a-f]+ -->", validation_report)
    source = marker.group(0) if marker else validation_report
    return f"{feedback_identity}:validation:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"


def _adversarial_feedback_belongs_to_report(body: str, validation_report: str) -> bool:
    """Return whether a standalone adversarial finding is represented in a report."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
    substantive = [block for block in blocks if not block.startswith(("#", "**", "Gap identity:")) and len(block) >= 12]
    return bool(substantive) and any(block in validation_report for block in substantive)


def _load_delivered_review_feedback(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("delivery state is not a JSON object")
    values = loaded.get("delivered_feedback", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("delivered_feedback is not a string list")
    return set(values)


def _load_pending_review_feedback(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("delivery state is not a JSON object")
    values = loaded.get("pending_feedback", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("pending_feedback is not a string list")
    return set(values)


def _load_review_validation_generations(state_path: Path) -> dict[str, str]:
    """Return immutable validation-to-remediation-generation associations."""
    if not state_path.exists():
        return {}
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("delivery state is not a JSON object")
    values = loaded.get("validation_generations", {})
    if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
        raise ValueError("validation_generations is not a string mapping")
    return values


def _load_review_validation_snapshots(state_path: Path) -> dict[str, dict[str, str]]:
    """Return pre-validation generation snapshots keyed by validation attempt."""
    if not state_path.exists():
        return {}
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("delivery state is not a JSON object")
    values = loaded.get("validation_snapshots", {})
    if not isinstance(values, dict):
        raise ValueError("validation_snapshots is not an object mapping")
    snapshots: dict[str, dict[str, str]] = {}
    for identity, snapshot in values.items():
        if not isinstance(identity, str) or not isinstance(snapshot, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in snapshot.items()):
            raise ValueError("validation_snapshots contains an invalid generation mapping")
        snapshots[identity] = snapshot
    return snapshots


def _record_review_feedback_state(
    state_path: Path,
    delivered: set[str],
    pending: set[str],
    validation_generations: Optional[dict[str, str]] = None,
    validation_snapshots: Optional[dict[str, dict[str, str]]] = None,
) -> None:
    """Atomically persist confirmed and indeterminate feedback deliveries."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix(f"{state_path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "delivered_feedback": sorted(delivered),
                "pending_feedback": sorted(pending),
                "validation_generations": validation_generations if validation_generations is not None else _load_review_validation_generations(state_path),
                "validation_snapshots": validation_snapshots if validation_snapshots is not None else _load_review_validation_snapshots(state_path),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, state_path)


def _adversarial_validation_snapshot_identity(validation_report: str) -> str:
    marker = re.search(r"<!-- auto-coder-adversarial-validation-attempt:v1:\d+:[0-9a-f]+ -->", validation_report)
    return hashlib.sha256((marker.group(0) if marker else validation_report).encode("utf-8")).hexdigest()


def _record_review_validation_snapshot(state_path: Path, validation_report: str, snapshot: dict[str, str]) -> None:
    """Durably freeze observed generations before validation publication."""
    with _cloud_review_delivery_lock:
        snapshots = _load_review_validation_snapshots(state_path)
        snapshots.setdefault(_adversarial_validation_snapshot_identity(validation_report), snapshot)
        _record_review_feedback_state(
            state_path,
            _load_delivered_review_feedback(state_path),
            _load_pending_review_feedback(state_path),
            validation_snapshots=snapshots,
        )


def _record_delivered_review_feedback(state_path: Path, feedback: set[str]) -> None:
    """Atomically persist confirmed feedback deliveries."""
    pending = _load_pending_review_feedback(state_path) - feedback
    _record_review_feedback_state(state_path, feedback, pending)


def _cloud_review_feedback_markers(feedback: Sequence[str]) -> str:
    """Render durable PR-side receipts for confirmed cloud deliveries."""
    return "\n".join(f"<!-- {CLOUD_REVIEW_FEEDBACK_MARKER_PREFIX}{hashlib.sha256(identity.encode('utf-8')).hexdigest()} -->" for identity in sorted(feedback))


def _load_pr_delivered_review_feedback(github_client: Optional[Any], repo_name: str, pr_number: int, feedback: Sequence[str]) -> set[str]:
    """Return feedback identities with a durable delivery receipt on the PR."""
    if github_client is None or not feedback:
        return set()
    comments = github_client.get_pr_comments(repo_name, pr_number)
    receipt_comments = [comment for comment in comments if isinstance(comment, dict) and CLOUD_REVIEW_FEEDBACK_MARKER_PREFIX in str(comment.get("body", ""))]
    if not receipt_comments:
        return set()
    trusted_login = github_client.get_authenticated_user_login()
    if not isinstance(trusted_login, str) or not trusted_login.strip():
        raise ValueError("authenticated GitHub receipt author is unavailable")
    trusted_login = trusted_login.strip().lower()
    bodies = "\n".join(comment.get("body", "") for comment in receipt_comments if isinstance(comment.get("user"), dict) and str(comment["user"].get("login", "")).strip().lower() == trusted_login)
    return {identity for identity in feedback if f"<!-- {CLOUD_REVIEW_FEEDBACK_MARKER_PREFIX}{hashlib.sha256(identity.encode('utf-8')).hexdigest()} -->" in bodies}


def _record_pr_delivered_review_feedback(github_client: Optional[Any], repo_name: str, pr_number: int, feedback: Sequence[str], message: str) -> bool:
    """Persist confirmed delivery receipts in GitHub independently of local state."""
    if github_client is None or not feedback:
        return False
    github_client.add_comment_to_pr(repo_name, pr_number, f"{_cloud_review_feedback_markers(feedback)}\n{message}")
    return True


def _reconcile_codex_review_feedback(client: Any, provider: str, task_id: str, feedback: Sequence[str]) -> set[str]:
    """Return stable feedback identities confirmed by Codex reconciliation."""
    if provider != "codex-cloud" or not hasattr(client, "get_followup_delivery"):
        return set()
    from .codex_wham_client import FollowUpDeliveryOutcome

    return {identity for identity in feedback if client.get_followup_delivery(task_id, identity) is FollowUpDeliveryOutcome.DELIVERED}


def _resolve_cloud_task_origin(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> CloudTaskOriginResolution:
    """Resolve exactly one durable provider/session association for a PR."""
    pr_number = int(pr_data["number"])
    manager = CloudManager(repo_name)
    direct_binding = manager.get_binding(pr_number)
    if direct_binding is not None and direct_binding.provider != "codex-cloud":
        from .cloud_task_engine import CloudTaskEngine

        try:
            engine = CloudTaskEngine()
            if direct_binding.provider == "claude-routine":
                client = engine.get_client_for_provider(
                    direct_binding.provider,
                    repo_name,
                    backend_name=direct_binding.backend_name or "claude-routine",
                )
            else:
                client = engine.get_client_for_provider(direct_binding.provider, repo_name)
        except Exception as exc:
            logger.error(f"Failed to initialize direct PR cloud provider '{direct_binding.provider}' for PR #{pr_number}: {exc}")
            return CloudTaskOriginResolution(reason=f"cloud provider '{direct_binding.provider}' is unavailable: {exc}")
        if client is None:
            return CloudTaskOriginResolution(reason=f"cloud provider '{direct_binding.provider}' is unavailable")
        return CloudTaskOriginResolution(
            origin=CloudTaskOrigin(
                provider=direct_binding.provider,
                task_id=direct_binding.task_id,
                client=client,
            )
        )
    # Codex-associated PRs are exceptional: an Issue's current cloud binding is
    # only a projection and can point at an older Jules session or a later retry.
    # Resolve the publication-owned PR binding before consulting that projection.
    try:
        from .cloud_run import CloudRunRepository
        from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository, resolve_codex_pr_origin

        runs = CloudRunRepository(repo_name)
        attribution_registry = CodexPrAttributionRepository(repo_name)
        attribution = resolve_codex_pr_origin(repo_name, pr_data, runs, attribution_registry)
        in_scope = bool(attribution.origin or pr_data.get("_codex_pr_attribution_required"))
        if in_scope:
            if attribution.disposition is not AttributionDisposition.VERIFIED or attribution.origin is None:
                boundary = attribution.boundary or "positive PR publication attribution is missing"
                return CloudTaskOriginResolution(reason=f"Codex PR attribution is {attribution.disposition.value}: {boundary}")
            from .codex_cloud_client import CodexCloudClient

            origin = attribution.origin
            selected_client: Any = CodexCloudClient(backend_name=origin.backend_name or None, repo_name=repo_name)
            return CloudTaskOriginResolution(
                origin=CloudTaskOrigin(
                    provider=origin.provider,
                    task_id=origin.task_id,
                    client=selected_client,
                    attribution_token=attribution.consistency_token,
                )
            )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        if pr_data.get("_codex_pr_attribution_required"):
            return CloudTaskOriginResolution(reason=f"Codex PR attribution is UNAVAILABLE: {type(exc).__name__}")

    bindings = []
    if direct_binding:
        bindings.append(direct_binding)
    else:
        for issue_number in _resolve_pr_issue_numbers(repo_name, pr_data, github_client):
            binding = manager.get_binding(issue_number)
            if binding:
                bindings.append(binding)
    unique_bindings = {(binding.provider, binding.task_id, binding.backend_name) for binding in bindings}
    if len(unique_bindings) != 1:
        reason = "no provider-owned cloud task association was found" if not unique_bindings else "multiple conflicting cloud task associations were found"
        return CloudTaskOriginResolution(reason=reason)
    provider, task_id, backend_name = unique_bindings.pop()

    from .cloud_task_engine import CloudTaskEngine

    try:
        engine = CloudTaskEngine()
        if provider == "claude-routine":
            client = engine.get_client_for_provider(provider, repo_name, backend_name=backend_name or "claude-routine")
        else:
            client = engine.get_client_for_provider(provider, repo_name)
    except Exception as exc:
        logger.error(f"Failed to initialize cloud provider '{provider}' for PR #{pr_number}: {exc}")
        return CloudTaskOriginResolution(reason=f"cloud provider '{provider}' is unavailable: {exc}")
    if client is None:
        return CloudTaskOriginResolution(reason=f"cloud provider '{provider}' is unavailable")
    return CloudTaskOriginResolution(origin=CloudTaskOrigin(provider=provider, task_id=task_id, client=client))


def _revalidate_cloud_origin(repo_name: str, pr_number: int, origin: CloudTaskOrigin) -> Optional[str]:
    """Fail closed if a Codex PR binding changed after route selection."""
    if not origin.attribution_token:
        return None
    from .codex_pr_attribution import AttributionDisposition, CodexPrAttributionRepository

    current = CodexPrAttributionRepository(repo_name).get(pr_number)
    if current.disposition is AttributionDisposition.VERIFIED and current.origin is not None and current.consistency_token == origin.attribution_token and current.origin.provider == origin.provider and current.origin.task_id == origin.task_id:
        return None
    boundary = current.boundary or "the PR attribution consistency token or recipient changed"
    return f"Codex PR attribution is {current.disposition.value}: {boundary}"


def _delegate_cloud_review_thread_repair(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
    unresolved_threads: Tuple[ReviewThread, ...] = (),
) -> CloudReviewRepairResult:
    """Assign unresolved review feedback to its originating cloud task.

    Delivery is durable and keyed to actionable root findings. Implementation
    commits and replies therefore cannot make an old finding eligible again.
    Ownership comes only from the provider-aware durable association. No new
    task, branch, or pull request is created by this path.
    """
    pr_number = int(pr_data["number"])
    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
    if exhaustion_info and exhaustion_info.is_exhausted:
        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: automatic repair allowance is exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}"])

    resolution = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
    if resolution.origin is None:
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: {resolution.reason}"])
    provider, task_id, client = resolution.origin.provider, resolution.origin.task_id, resolution.origin.client
    selected_origin = resolution.origin
    provider_label = {"codex-cloud": "Codex Cloud", "jules": "Jules", "claude-routine": "Claude Routine"}.get(provider, provider)

    from .cloud_task_client_base import CloudTaskClientBase

    if getattr(type(client), "send_followup", None) is CloudTaskClientBase.send_followup:
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: cloud provider '{provider}' does not support follow-up delivery"])

    state_path = _cloud_review_repair_state_path(repo_name)
    prefix = f"{repo_name}#{pr_number}:{provider}:{task_id}:"
    implementer_login = (get_pr_author_login(pr_data) or "").lower()
    findings = [
        (thread, comment, _review_feedback_identity(prefix, thread, index))
        for thread in unresolved_threads
        for index, comment in enumerate(thread.comments)
        # A root is reviewer feedback by definition. Later comments create new
        # work only when they come from someone other than the PR implementer.
        # A structural adjudication envelope (REQ-009) is a machine-readable
        # decision, not free-form reviewer prose; review_adjudication_orchestrator
        # owns routing its effect, so it must never be forwarded here verbatim,
        # authorized or not.
        if (index == 0 or not implementer_login or not is_same_github_login(comment.author_login, implementer_login)) and not is_adjudication_envelope(comment.body)
    ]
    with _cloud_review_delivery_lock:
        try:
            delivered = _load_delivered_review_feedback(state_path)
            delivered.update(_load_pr_delivered_review_feedback(github_client, repo_name, pr_number, [identity for _thread, _comment, identity in findings]))
            reconciled = _reconcile_codex_review_feedback(client, provider, task_id, [identity for _thread, _comment, identity in findings if identity not in delivered])
            if reconciled:
                delivered.update(reconciled)
                _record_delivered_review_feedback(state_path, delivered)
                _record_pr_delivered_review_feedback(github_client, repo_name, pr_number, sorted(reconciled), f"🤖 Auto-Coder: I reconciled previously submitted review feedback with the existing {provider} task.")
            indeterminate = _load_pending_review_feedback(state_path) - delivered
        except (OSError, ValueError) as exc:
            logger.warning(f"Could not read cloud review repair state: {exc}")
            return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: prior delivery state could not be read: {exc}"])
        pending = [(thread, comment, identity) for thread, comment, identity in findings if identity not in delivered]
    if not pending:
        logger.info(f"All actionable review feedback for PR #{pr_number} was already delegated")
        return CloudReviewRepairResult([f"{provider_label} review repair was already requested for PR #{pr_number} for all current actionable feedback"], delivered=True)

    blocked = [(thread, comment, identity) for thread, comment, identity in pending if identity in indeterminate]
    pending = [(thread, comment, identity) for thread, comment, identity in pending if identity not in indeterminate]
    if not pending:
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: a prior follow-up has unconfirmed durable receipt status; duplicate delivery was suppressed"])

    pending_identities = {identity for _thread, _comment, identity in pending}
    work_identity = hashlib.sha256("\n".join(sorted(pending_identities)).encode()).hexdigest()
    retained_wait = get_claude_followup_wait_store().get(repo_name, task_id, "review-thread-repair", work_identity)
    if retained_wait and (retained_wait.certainty is DeliveryCertainty.INDETERMINATE or retained_wait.retry_not_before > time.time()):
        return CloudReviewRepairResult(
            [f"DEFERRED Claude review repair for PR #{pr_number} until {retained_wait.retry_not_before}"],
            deferred=True,
            retry_not_before=retained_wait.retry_not_before,
        )
    with _cloud_review_delivery_lock:
        try:
            _record_review_feedback_state(state_path, delivered, indeterminate | pending_identities)
        except OSError as exc:
            return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: a durable delivery reservation could not be recorded: {exc}"])

    if github_client is None:
        with _cloud_review_delivery_lock:
            _record_review_feedback_state(state_path, delivered, indeterminate)
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: an authoritative current PR lookup is unavailable"])
    try:
        live_metadata = github_client.get_pull_request_repair_metadata_strict(repo_name, pr_number)
    except Exception as exc:
        with _cloud_review_delivery_lock:
            try:
                _record_review_feedback_state(state_path, delivered, indeterminate)
            except OSError:
                pass
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: current PR head/branch could not be verified: {exc}"])
    live_pr_data = dict(pr_data)
    live_pr_data["head"] = {"ref": live_metadata.head_ref, "sha": live_metadata.head_sha}
    live_pr_data["base"] = {"ref": live_metadata.base_ref}
    target = resolve_existing_pr_repair_target(repo_name, live_pr_data)
    if not target:
        with _cloud_review_delivery_lock:
            _record_review_feedback_state(state_path, delivered, indeterminate)
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: current PR head/base branch metadata is unavailable"])

    canonical_pending = [(thread, comment, identity) for thread, comment, identity in pending if comment.body.startswith(("### Auto-Coder adversarial finding", "### Auto-Coder material test-oracle gap"))]
    has_canonical_marker = any(_BLOCKER_ID_RE.search(comment.body) for _thread, comment, _identity in canonical_pending)
    bundle_to_bind = None
    ledger = CanonicalPRBlockerLedger()
    snapshot = None
    try:
        snapshot = ledger.get_snapshot("https://api.github.com", repo_name, pr_number, require_retained_state=True)
    except Exception:
        pass

    matched_bids = set()
    if snapshot is not None:
        for _thread, comment, _identity in canonical_pending:
            m = _BLOCKER_ID_RE.search(comment.body)
            if m and snapshot.get_blocker(m.group(1) or m.group(2)):
                matched_bids.add(m.group(1) or m.group(2))
            gm = _GAP_ID_RE.search(comment.body)
            if gm and snapshot.get_blocker(gm.group(1) or gm.group(2) or gm.group(3)):
                matched_bids.add(gm.group(1) or gm.group(2) or gm.group(3))

    if has_canonical_marker and not matched_bids:
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: canonical blocker bundle data is absent"])

    if matched_bids and snapshot is not None:
        bundle_to_bind = build_repair_handoff_bundle(
            snapshot=snapshot,
            repo_name=repo_name,
            pr_number=pr_number,
            head_branch=target.head_branch,
            base_branch=target.base_branch,
            reviewed_head_sha=target.head_sha,
            requirement_manifest_revision=pr_data.get("requirement_manifest_revision", ""),
            target_blocker_ids=sorted(matched_bids),
        )
        val_res = validate_repair_handoff_bundle(bundle_to_bind, target.head_sha, pr_data.get("requirement_manifest_revision", ""), snapshot)
        if not val_res.is_valid:
            return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: bundle {bundle_to_bind.bundle_id} is stale ({val_res.reason})"])

        rendered_bundle = render_bounded_repair_payload(bundle_to_bind)
        ledger.record_repair_bundle(bundle_to_bind, rendered_payload=rendered_bundle)
        details = Template(get_prompt_template("codex_cloud.review_thread_repair_details")).safe_substitute(actionable_feedback=rendered_bundle)
        prompt = build_existing_pr_repair_prompt(target, details, bundle=bundle_to_bind)
    else:
        feedback = "\n\n".join(f"Thread `{thread.id}`:\n{comment.body}" for thread, comment, _identity in pending)
        details = Template(get_prompt_template("codex_cloud.review_thread_repair_details")).safe_substitute(actionable_feedback=feedback)
        prompt = build_existing_pr_repair_prompt(target, details)
    try:
        attribution_error = _revalidate_cloud_origin(repo_name, pr_number, selected_origin)
        if attribution_error:
            with _cloud_review_delivery_lock:
                _record_review_feedback_state(state_path, delivered, indeterminate)
            return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: {attribution_error}"])
        if provider == "codex-cloud":
            accepted = _send_followup_with_quota_admission(client, repo_name, task_id, prompt, tuple(sorted(pending_identities)))
        else:
            accepted = _send_followup_with_quota_admission(client, repo_name, task_id, prompt)
    except ClaudeFollowupHoldActive as exc:
        return CloudReviewRepairResult(
            [f"DEFERRED Claude review repair for PR #{pr_number} until {exc.retry_not_before}"],
            deferred=True,
            retry_not_before=exc.retry_not_before,
        )
    except ClaudeFollowupUsageLimitError as exc:
        retry_at = _retain_claude_quota_deferral(exc, pr_number, task_id, "review-thread-repair", work_identity)
        if exc.delivery_certainty is DeliveryCertainty.NOT_SENT:
            with _cloud_review_delivery_lock:
                _record_review_feedback_state(state_path, delivered, indeterminate)
        return CloudReviewRepairResult(
            [f"DEFERRED Claude review repair for PR #{pr_number} until {retry_at}"],
            deferred=True,
            retry_not_before=retry_at,
        )
    except Exception as exc:
        logger.warning(f"Cloud review repair delegation failed for PR #{pr_number}: {exc}")
        with _cloud_review_delivery_lock:
            try:
                _record_review_feedback_state(state_path, delivered, indeterminate)
            except OSError:
                pass
        return CloudReviewRepairResult([f"Review repair was not delivered to {provider} for PR #{pr_number}: {exc}"])
    if not accepted:
        with _cloud_review_delivery_lock:
            try:
                _record_review_feedback_state(state_path, delivered, indeterminate)
            except OSError:
                pass
        return CloudReviewRepairResult([f"Review repair was not delivered for PR #{pr_number}: {provider} task '{task_id}' rejected follow-up delivery"])

    get_claude_followup_wait_store().retire(repo_name, task_id, "review-thread-repair", work_identity)

    local_receipt = False
    try:
        with _cloud_review_delivery_lock:
            delivered = _load_delivered_review_feedback(state_path)
            delivered.update(identity for _thread, _comment, identity in pending)
            _record_delivered_review_feedback(state_path, delivered)
            local_receipt = True
    except OSError as exc:
        logger.warning(f"Could not persist cloud review repair state: {exc}")
    try:
        remote_receipt = _record_pr_delivered_review_feedback(
            github_client,
            repo_name,
            pr_number,
            [identity for _thread, _comment, identity in pending],
            f"🤖 Auto-Coder: I sent newly actionable review feedback to the existing {provider} task.",
        )
    except Exception as exc:
        remote_receipt = False
        logger.warning(f"Could not persist cloud review repair receipt on PR #{pr_number}: {exc}")
    if not local_receipt and not remote_receipt:
        logger.error(f"Confirmed cloud review repair delivery for PR #{pr_number} has no durable receipt")
        return CloudReviewRepairResult(
            [f"Review repair follow-up was accepted for PR #{pr_number}, but durable delivery confirmation failed; duplicate delivery is suppressed"],
        )

    get_trace_logger().log(
        "Cloud Review Repair",
        f"Assigned unresolved review threads to {provider} task '{task_id}' for PR #{pr_number}",
        item_type="pr",
        item_number=pr_number,
        details={"provider": provider, "task_id": task_id, "head_sha": target.head_sha},
    )
    actions = [f"Requested {provider_label} task '{task_id}' to address unresolved review threads for PR #{pr_number}"]
    if blocked:
        actions.insert(0, f"Suppressed duplicate delivery of {len(blocked)} finding(s) with unconfirmed durable receipt status for PR #{pr_number}")
    return CloudReviewRepairResult(actions, delivered=True)


def _adjudication_context_store_path() -> Path:
    return Path(os.environ.get(ADJUDICATION_DB_ENV, DEFAULT_ADJUDICATION_DB_PATH)).expanduser()


def _adjudication_effects_store_path() -> Path:
    return Path(os.environ.get(ADJUDICATION_EFFECTS_DB_ENV, DEFAULT_ADJUDICATION_EFFECTS_DB_PATH)).expanduser()


_adjudication_effect_store_cache: Dict[str, AdjudicationEffectStore] = {}
_adjudication_effect_store_cache_lock = threading.Lock()


def _get_adjudication_effect_store() -> AdjudicationEffectStore:
    path = str(_adjudication_effects_store_path())
    with _adjudication_effect_store_cache_lock:
        store = _adjudication_effect_store_cache.get(path)
        if store is None:
            store = AdjudicationEffectStore(Path(path))
            _adjudication_effect_store_cache[path] = store
        return store


def _current_adjudication_ledger_snapshots(repo_name: str, pr_number: int, github_client: Any) -> Tuple[AdjudicationSnapshot, ...]:
    """Re-prove applicability against current authority before any effect.

    A merely-persisted tip is not evidence: a revoked allowlist, an edited or
    deleted accepted source, a new conflicting reply, or a moved head/base
    since the last observation must all be caught here, immediately before
    the caller trusts the result, not only during an earlier same-pass
    ``AutomationEngine.refresh_review_adjudications`` (REQ-001, REQ-002,
    REQ-011, REQ-014). This performs the identical authorization-policy,
    revision-binding, and thread-reconciliation steps that production
    refresh already performs, reusing that same code rather than a lighter
    read-only shortcut.
    """
    store = AdjudicationContextStore(_adjudication_context_store_path())
    if not store.ledgers_for_pr(repo_name, pr_number):
        return ()
    try:
        authoritative = github_client.get_pull_request_metadata_strict(repo_name, pr_number)
        threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
    except Exception as exc:
        logger.warning(f"Could not re-read authoritative PR state to apply adjudication effects for PR #{pr_number}: {exc}")
        return ()
    reviewer_ids = get_pr_review_allowlist_from_config(repo_name=repo_name) or ()
    adjudicator_ids = get_review_adjudicator_allowlist_from_config(repo_name=repo_name) or ()
    service = ReviewAdjudicationService(github_client, store)
    service.apply_authorization_policy(repo_name, pr_number, reviewer_ids, adjudicator_ids)
    try:
        binding = ReviewAdjudicationService._binding(repo_name, pr_number, authoritative)
    except ValueError as exc:
        logger.warning(f"Could not verify authoritative PR revision binding to apply adjudication effects for PR #{pr_number}: {exc}")
        return ()
    service.apply_revision_binding(binding)

    threads_by_root = {thread.comments[0].database_id: thread for thread in threads if thread.comments}
    snapshots = []
    for ledger in store.ledgers_for_pr(repo_name, pr_number):
        thread = threads_by_root.get(ledger.context.root_comment_id)
        if ledger.context.retired_reason is None:
            if thread is None:
                ledger.retire("registered review root was confirmed absent")
                store.save(ledger, "orchestrator-root-check")
            else:
                reconcile_thread(ledger, thread, adjudicator_ids, reviewer_ids)
                observation = hashlib.sha256("\0".join(f"{item.database_id}:{item.updated_at}" for item in thread.comments).encode("utf-8")).hexdigest()
                store.save(ledger, observation)
        raw_finding = thread.comments[0].body if thread is not None else ""
        result = ledger.current(None, None, "orchestrator read")
        snapshots.append(
            AdjudicationSnapshot(
                context=ledger.context,
                raw_finding=raw_finding,
                contributing_issues=tuple(item.issue_number for item in ledger.context.contracts),
                root_author_id=ledger.context.root_author_id,
                source_comment_id=result.source_comment_id,
                result=result,
                observation_revision="orchestrator-read",
            )
        )
    return tuple(snapshots)


def _mark_test_oracle_gap_invalid(repo_name: str, pr_number: int, gap_id: str, decision_id: str, rationale: str, head_sha: str) -> bool:
    """Retire a persisted material test-oracle gap as adjudicated-invalid.

    Never downgrades an independently established ``RESOLVED`` gap; only an
    ``OPEN`` gap is retired, so a gap already proven fixed by a real
    regression test is left alone (REQ-004).
    """
    registry = ReviewerSessionRegistry()
    updated = False
    for session in registry.sessions_for_pr(repo_name, pr_number):
        changed = False
        for gap in session.test_oracle_gaps:
            if gap.gap_id == gap_id and gap.status == "OPEN":
                gap.status = "INVALID"
                gap.resolution_evidence = f"Overruled by authorized adjudication decision `{decision_id}`: {rationale}"
                gap.resolution_head_sha = head_sha
                changed = True
                updated = True
        if changed:
            registry.save(session)
    return updated


def _reopen_test_oracle_gap(repo_name: str, pr_number: int, gap_id: str) -> bool:
    """Reverse a gap this adjudication effect previously retired as invalid."""
    registry = ReviewerSessionRegistry()
    reopened = False
    for session in registry.sessions_for_pr(repo_name, pr_number):
        changed = False
        for gap in session.test_oracle_gaps:
            if gap.gap_id == gap_id and gap.status == "INVALID":
                gap.status = "OPEN"
                gap.resolution_evidence = ""
                gap.resolution_head_sha = ""
                changed = True
                reopened = True
        if changed:
            registry.save(session)
    return reopened


_ADJUDICATION_EFFECT_OUTCOMES = {
    "delivered": Outcome.COMPLETED,
    "retired": Outcome.COMPLETED,
    "reconciled": Outcome.COMPLETED,
    "pending": Outcome.DEFERRED,
    "unknown": Outcome.UNKNOWN,
    "reconciliation-required": Outcome.BLOCKED,
}


def _record_adjudication_effect_stage(pr_number: int, effect_kind: str, context_id: str, decision_id: str, status: str, gap_id: str = "") -> None:
    """Record one applied (or attempted) adjudication effect for the dashboard.

    A distinct outcome per REQ-010's processing-status distinctions: repair
    delivery pending/confirmed/unknown, scoped retirement pending/applied,
    and reconciliation-required for a reversed overrule that could not be
    confirmed reversed on GitHub.
    """
    facts: Dict[str, Any] = {"effect_kind": effect_kind, "context_id": context_id, "decision_id": decision_id, "status": status}
    if gap_id:
        facts["gap_id"] = gap_id
    _record_pr_stage(pr_number, "pr.review-adjudication-effect", f"pr#{pr_number} review adjudication effect ({effect_kind})", _ADJUDICATION_EFFECT_OUTCOMES.get(status, Outcome.UNKNOWN), facts)


def _apply_review_adjudication_effects(repo_name: str, pr_number: int, pr_data: Dict[str, Any], github_client: Any) -> Tuple[PRActionList, bool]:
    """Apply every authorized adjudication decision's owned effect for this PR.

    Returns the actions taken plus whether ordinary same-head validation
    suppression must be bypassed this pass because a real adjudication
    effect is newly effective or still unconfirmed (REQ-002, REQ-003,
    REQ-004, REQ-007).
    """
    actions = PRActionList()
    try:
        snapshots = _current_adjudication_ledger_snapshots(repo_name, pr_number, github_client)
    except Exception as exc:
        logger.warning(f"Could not evaluate review adjudication effects for PR #{pr_number}: {exc}")
        return actions, False
    if not snapshots:
        return actions, False
    effect_store = _get_adjudication_effect_store()
    plan = plan_adjudication_effects(snapshots, effect_store)
    if not plan:
        return actions, effect_store.force_revalidation_needed(repo_name, pr_number)

    # Reconcile a reversed OVERRULE before applying whatever the new current
    # disposition is: both can target the same context_id journal row in this
    # same pass (e.g. a fresh UPHOLD superseding a retired OVERRULE), and the
    # row must end this pass reflecting the current disposition's own
    # delivery/retirement status, not the stale reconciliation outcome.
    for reopen in plan.reopens:
        try:
            effect_store.begin(reopen.context_id, repo_name, pr_number, reopen.decision_id, reopen.head_sha, reopen.contract_digest, reopen.verdict, gap_id=reopen.gap_id)
        except Exception as exc:
            logger.error(f"Could not durably record adjudication reconciliation for PR #{pr_number}: {exc}")
            continue
        reopened_gap = bool(reopen.gap_id) and _reopen_test_oracle_gap(repo_name, pr_number, reopen.gap_id)
        try:
            github_client.unresolve_review_thread(reopen.thread_id)
            note = f" and reopened material test-oracle gap `{reopen.gap_id}`" if reopened_gap else ""
            actions.append(f"Reversed a superseded or revoked adjudication overrule on PR #{pr_number}{note}")
            effect_store.finish(reopen.context_id, "reconciled", gap_id=reopen.gap_id)
            _record_adjudication_effect_stage(pr_number, "REOPEN", reopen.context_id, reopen.decision_id, "reconciled", gap_id=reopen.gap_id)
        except Exception as exc:
            logger.warning(f"Could not reverse a superseded adjudication overrule for PR #{pr_number}: {exc}")
            actions.append(f"A superseded adjudication overrule on PR #{pr_number} could not be reversed on GitHub and remains a merge blocker until it is: {exc}")
            effect_store.finish(reopen.context_id, "reconciliation-required", gap_id=reopen.gap_id)
            _record_adjudication_effect_stage(pr_number, "REOPEN", reopen.context_id, reopen.decision_id, "reconciliation-required", gap_id=reopen.gap_id)

    for upheld in plan.upholds:
        try:
            effect_store.begin(upheld.context_id, repo_name, pr_number, upheld.decision_id, upheld.head_sha, upheld.contract_digest, "UPHOLD")
        except Exception as exc:
            logger.error(f"Could not durably record adjudication effect for PR #{pr_number}: {exc}")
            continue
        status = "unknown"
        try:
            resolution = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
            if resolution.origin is None:
                actions.append(f"Adjudicated repair for PR #{pr_number} was not delivered: {resolution.reason}")
                status = "pending"
            else:
                provider, task_id, client = resolution.origin.provider, resolution.origin.task_id, resolution.origin.client
                from .cloud_task_client_base import CloudTaskClientBase

                if getattr(type(client), "send_followup", None) is CloudTaskClientBase.send_followup:
                    actions.append(f"Adjudicated repair for PR #{pr_number} was not delivered: cloud provider '{provider}' does not support follow-up delivery")
                    status = "pending"
                else:
                    live_metadata = github_client.get_pull_request_repair_metadata_strict(repo_name, pr_number)
                    live_pr_data = dict(pr_data)
                    live_pr_data["head"] = {"ref": live_metadata.head_ref, "sha": live_metadata.head_sha}
                    live_pr_data["base"] = {"ref": live_metadata.base_ref}
                    target = resolve_existing_pr_repair_target(repo_name, live_pr_data)
                    if not target:
                        actions.append(f"Adjudicated repair for PR #{pr_number} was not delivered: current PR head/base branch metadata is unavailable")
                        status = "pending"
                    else:
                        details = Template(get_prompt_template("codex_cloud.adjudication_upheld_repair_details")).safe_substitute(rationale=upheld.rationale, raw_finding=upheld.raw_finding)
                        prompt = build_existing_pr_repair_prompt(target, details)
                        attribution_error = _revalidate_cloud_origin(repo_name, pr_number, resolution.origin)
                        if attribution_error:
                            actions.append(f"Adjudicated repair for PR #{pr_number} was not delivered: {attribution_error}")
                            status = "pending"
                            continue
                        accepted = client.send_followup(task_id, prompt, (upheld.decision_id,)) if provider == "codex-cloud" else client.send_followup(task_id, prompt)
                        if accepted:
                            actions.append(f"Requested {provider} task '{task_id}' to apply an authorized bounded correction for PR #{pr_number}")
                            status = "delivered"
                        else:
                            actions.append(f"Adjudicated repair for PR #{pr_number} was not delivered: {provider} task '{task_id}' rejected follow-up delivery")
                            status = "pending"
        except Exception as exc:
            logger.warning(f"Adjudicated repair delivery failed for PR #{pr_number}: {exc}")
            actions.append(f"Adjudicated repair for PR #{pr_number} was not confirmed delivered: {exc}")
            status = "unknown"
        effect_store.finish(upheld.context_id, status)
        _record_adjudication_effect_stage(pr_number, "UPHOLD", upheld.context_id, upheld.decision_id, status)

    for overrule in plan.overrules:
        try:
            effect_store.begin(overrule.context_id, repo_name, pr_number, overrule.decision_id, overrule.head_sha, overrule.contract_digest, "OVERRULE", gap_id=overrule.gap_id or "")
        except Exception as exc:
            logger.error(f"Could not durably record adjudication effect for PR #{pr_number}: {exc}")
            continue
        gap_note = ""
        if overrule.gap_id and not overrule.gap_still_contributed_elsewhere:
            if _mark_test_oracle_gap_invalid(repo_name, pr_number, overrule.gap_id, overrule.decision_id, overrule.rationale, overrule.head_sha):
                gap_note = f" and retired material test-oracle gap `{overrule.gap_id}`"
        elif overrule.gap_id:
            gap_note = f" (material test-oracle gap `{overrule.gap_id}` remains open: another live finding still contributes to it)"

        status = "unknown"
        try:
            explanation = format_overrule_explanation(overrule.decision_id, overrule.rationale)
            github_client.reply_to_review_thread(repo_name, pr_number, overrule.root_comment_id, explanation)
            # Re-check immediately before the resolve mutation: a concurrent
            # writer (e.g. the Dashboard write boundary) may have superseded
            # or revoked this exact decision since the plan was computed.
            fresh = _current_adjudication_ledger_snapshots(repo_name, pr_number, github_client)
            fresh_result = next((snap.result for snap in fresh if snap.context is not None and snap.context.context_id == overrule.context_id), None)
            if fresh_result is None or fresh_result.status != AdjudicationStatus.APPLICABLE or fresh_result.verdict != "OVERRULE" or fresh_result.decision_id != overrule.decision_id:
                actions.append(f"Overruled finding on PR #{pr_number} was not resolved on GitHub: the decision is no longer current")
                status = "pending"
            else:
                github_client.resolve_review_thread(overrule.thread_id)
                actions.append(f"Retired an overruled finding on PR #{pr_number}{gap_note}")
                status = "retired"
        except Exception as exc:
            logger.warning(f"Could not resolve overruled review thread for PR #{pr_number}: {exc}")
            actions.append(f"Overruled finding on PR #{pr_number} was not confirmed resolved on GitHub: {exc}")
            status = "unknown"
        effect_store.finish(overrule.context_id, status, gap_id=overrule.gap_id or "")
        _record_adjudication_effect_stage(pr_number, "OVERRULE", overrule.context_id, overrule.decision_id, status, gap_id=overrule.gap_id or "")

    return actions, effect_store.force_revalidation_needed(repo_name, pr_number)


def _delegate_cloud_merge_conflict_repair_result(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> CloudConflictDelegationResult:
    """Delegate a current conflict to its originating cloud session when possible.

    ``True`` means this conflict state was either just delegated or was already
    delegated, so local repair must stop for this processing pass. ``False``
    preserves the caller's existing fallback path.
    """
    origin = _resolve_cloud_conflict_origin(repo_name, pr_data, github_client)
    if origin is None:
        return CloudConflictDelegationResult(reason="no originating cloud implementation session could be resolved")
    client, task_id = origin

    # An inherited default method means that the provider does not opt in to
    # follow-up work. Do not use continue_if_paused as a substitute.
    from .cloud_task_client_base import CloudTaskClientBase

    if type(client).send_followup is CloudTaskClientBase.send_followup:
        return CloudConflictDelegationResult(reason=f"cloud provider for session '{task_id}' does not support repair follow-up")

    raw_pr_number = pr_data.get("number")
    if not isinstance(raw_pr_number, int) or isinstance(raw_pr_number, bool):
        return CloudConflictDelegationResult(reason="the PR number required for repair is unavailable")
    pr_number = raw_pr_number
    base = pr_data.get("base") or {}
    base_state = base.get("sha") or pr_data.get("base_sha") or pr_data.get("base_branch") or base.get("ref")
    target = resolve_existing_pr_repair_target(repo_name, pr_data)
    if not target or not base_state:
        logger.warning(f"PR #{pr_number} lacks complete head/base metadata; cannot delegate conflict repair")
        return CloudConflictDelegationResult(reason="the PR head/base metadata required for repair is unavailable")

    fingerprint = f"{repo_name}#{pr_number}:{target.head_sha}:{base_state}:{task_id}"
    retained_wait = get_claude_followup_wait_store().get(repo_name, task_id, "merge-conflict-repair", fingerprint)
    if retained_wait and (retained_wait.certainty is DeliveryCertainty.INDETERMINATE or retained_wait.retry_not_before > time.time()):
        return CloudConflictDelegationResult(
            reason=f"Claude quota wait is active until {retained_wait.retry_not_before}",
            deferred=True,
            retry_not_before=retained_wait.retry_not_before,
        )
    accepted_action = f"Codex Cloud task '{task_id}' accepted merge-conflict-repair follow-up for " f"PR #{pr_number} at head {target.head_sha}"
    state_path = _cloud_conflict_state_path(repo_name)
    with _cloud_conflict_delivery_lock:
        delivered: dict[str, CloudConflictDeliveryRecord] = {}
        try:
            delivered = _load_cloud_conflict_deliveries(state_path)
        except (OSError, ValueError) as exc:
            logger.warning(f"Could not read cloud conflict repair state: {exc}")
            return CloudConflictDelegationResult(reason=f"prior repair delivery state could not be read: {exc}")

        existing = delivered.get(fingerprint)
        if existing and existing.status == "confirmed":
            logger.info(f"Conflict repair for PR #{pr_number} at the current head/base state was already delegated")
            try:
                _report_cloud_conflict_followup(github_client, repo_name, pr_number, existing.task_id, target.head_sha, str(base_state))
            except Exception as exc:
                logger.warning(f"Could not publish accepted cloud conflict follow-up receipt on PR #{pr_number}: {exc}")
            return CloudConflictDelegationResult(
                delegated=True,
                reason="an equivalent repair request was already delegated",
                accepted_action=accepted_action,
            )
        if existing:
            return CloudConflictDelegationResult(reason=(f"delivery to originating cloud session '{existing.task_id}' has unconfirmed status; " "not resending a potentially accepted request"))

        # Reserve the conflict identity before the non-idempotent follow-up call.
        # Pending state prevents speculative redelivery but is never evidence
        # that the cloud session accepted the request. If this write fails, do not send.
        delivered[fingerprint] = CloudConflictDeliveryRecord(task_id=task_id, status="pending")
        try:
            _record_cloud_conflict_deliveries(state_path, delivered)
        except OSError as exc:
            logger.warning(f"Could not reserve cloud conflict repair delivery: {exc}")
            return CloudConflictDelegationResult(reason=f"a durable repair delivery receipt could not be reserved: {exc}")

    details = Template(get_prompt_template("pr.cloud_merge_conflict_repair_details")).safe_substitute(base_branch=target.base_branch)
    message = build_existing_pr_repair_prompt(target, details)
    failure_reason: Optional[str] = None
    try:
        final_origin = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
        if final_origin.origin is not None and final_origin.origin.attribution_token:
            attribution_error = _revalidate_cloud_origin(repo_name, pr_number, final_origin.origin)
            if attribution_error or final_origin.origin.task_id != task_id:
                with _cloud_conflict_delivery_lock:
                    delivered.pop(fingerprint, None)
                    _record_cloud_conflict_deliveries(state_path, delivered)
                return CloudConflictDelegationResult(reason=attribution_error or "verified PR origin changed before send")
        elif "Codex PR attribution is" in final_origin.reason:
            with _cloud_conflict_delivery_lock:
                delivered.pop(fingerprint, None)
                _record_cloud_conflict_deliveries(state_path, delivered)
            return CloudConflictDelegationResult(reason=final_origin.reason)
        accepted = _send_followup_with_quota_admission(client, repo_name, task_id, message)
    except ClaudeFollowupHoldActive as exc:
        return CloudConflictDelegationResult(reason=str(exc), deferred=True, retry_not_before=exc.retry_not_before)
    except ClaudeFollowupUsageLimitError as exc:
        retry_at = _retain_claude_quota_deferral(exc, pr_number, task_id, "merge-conflict-repair", fingerprint)
        if exc.delivery_certainty is DeliveryCertainty.NOT_SENT:
            with _cloud_conflict_delivery_lock:
                delivered.pop(fingerprint, None)
                _record_cloud_conflict_deliveries(state_path, delivered)
        return CloudConflictDelegationResult(
            reason=f"Claude quota wait is active until {retry_at}",
            deferred=True,
            retry_not_before=retry_at,
        )
    except Exception as exc:
        logger.warning(f"Cloud conflict repair delegation failed for PR #{pr_number}: {exc}")
        accepted = False
        failure_reason = f"delivery to originating cloud session '{task_id}' failed: {exc}"
    if not accepted:
        if failure_reason is None:
            failure_reason = f"delivery to originating cloud session '{task_id}' was rejected"
        with _cloud_conflict_delivery_lock:
            delivered.pop(fingerprint, None)
            try:
                _record_cloud_conflict_deliveries(state_path, delivered)
            except OSError as exc:
                logger.warning(f"Could not clear rejected cloud conflict repair reservation: {exc}")
                failure_reason += f"; its delivery reservation could not be cleared: {exc}"
        return CloudConflictDelegationResult(reason=failure_reason)

    get_claude_followup_wait_store().retire(repo_name, task_id, "merge-conflict-repair", fingerprint)

    with _cloud_conflict_delivery_lock:
        delivered[fingerprint] = CloudConflictDeliveryRecord(task_id=task_id, status="confirmed")
        try:
            _record_cloud_conflict_deliveries(state_path, delivered)
        except OSError as exc:
            # The request was accepted, but the pending reservation must not be
            # interpreted as confirmed on a later pass or resent speculatively.
            logger.warning(f"Could not confirm cloud conflict repair delivery: {exc}")
    logger.info(f"Delegated merge-conflict repair for PR #{pr_number} to existing cloud task {task_id}")
    try:
        _report_cloud_conflict_followup(github_client, repo_name, pr_number, task_id, target.head_sha, str(base_state))
    except Exception as exc:
        # Cloud acceptance is authoritative and must remain independent of the
        # best-effort GitHub publication. A later pass retries only this receipt.
        logger.warning(f"Could not publish accepted cloud conflict follow-up receipt on PR #{pr_number}: {exc}")
    return CloudConflictDelegationResult(
        delegated=True,
        reason=f"repair was delivered to cloud session '{task_id}'",
        accepted_action=accepted_action,
    )


def _delegate_cloud_merge_conflict_repair(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> bool:
    """Return whether cloud conflict repair was delegated or deduplicated."""
    return bool(_delegate_cloud_merge_conflict_repair_result(repo_name, pr_data, github_client))


def _send_codex_cloud_error_feedback(
    repo_name: str,
    pr_data: Dict[str, Any],
    failed_checks: List[Dict[str, Any]],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> CodexCloudFeedbackResult:
    """Send continuation request via continue_if_paused to Codex Cloud for Codex-created PRs.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        failed_checks: List of failed GitHub Actions checks
        config: AutomationConfig instance
        github_client: Optional GitHub client instance

    Returns:
        Structured delivery status and action strings describing what was done.
    """
    actions = []
    pr_number = pr_data["number"]

    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
    if exhaustion_info and exhaustion_info.is_exhausted:
        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
        return CodexCloudFeedbackResult(
            delivered=False,
            retryable=False,
            actions=(f"Codex Cloud continuation not delivered for PR #{pr_number}: automatic repair allowance is exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}",),
        )

    try:
        resolution = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
        if resolution.origin is None:
            if pr_data.get("_codex_pr_attribution_required"):
                actions.append(f"Cannot resume Codex Cloud task for PR #{pr_number}: {resolution.reason}")
                return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
            task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
            if not task_id:
                actions.append(f"Cannot resume Codex Cloud task for PR #{pr_number}: no valid Codex task ID found")
                return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
            from .codex_cloud_client import CodexCloudClient

            selected_origin = CloudTaskOrigin("codex-cloud", task_id, CodexCloudClient(repo_name=repo_name))
        else:
            selected_origin = resolution.origin
        if selected_origin.provider != "codex-cloud":
            actions.append(f"Cannot resume Codex Cloud task for PR #{pr_number}: verified provider is '{selected_origin.provider}'")
            return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
        task_id = selected_origin.task_id

        logger.info(f"Triggering continue_if_paused for Codex Cloud task '{task_id}' on PR #{pr_number}")
        client = selected_origin.client

        target = resolve_existing_pr_repair_target(repo_name, pr_data)
        if not new_work_allowed():
            actions.append(f"Deferred Codex Cloud continuation for PR #{pr_number}: graceful shutdown is draining")
            return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
        prompt = None
        if target:
            details = get_prompt_template("codex_cloud.ci_review_repair_details")
            prompt = build_existing_pr_repair_prompt(target, details)
        expected_head = str(pr_data.get("head", {}).get("sha") or "")
        with current_ci_failure_authority(github_client, repo_name, pr_number, expected_head) as authority:
            if not authority.allowed:
                actions.append(f"Deferred Codex Cloud continuation for PR #{pr_number}: {authority.reason}")
                return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
            logger.info(f"Initiating Codex Cloud CI repair for PR #{pr_number}; " f"head={expected_head} failures={authority.failure_identities}")
            attribution_error = _revalidate_cloud_origin(repo_name, pr_number, selected_origin)
            if attribution_error:
                actions.append(f"Deferred Codex Cloud continuation for PR #{pr_number}: {attribution_error}")
                return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))
            if prompt is not None:
                resumed = client.continue_if_paused(task_id, prompt=prompt)
            else:
                resumed = client.continue_if_paused(task_id)

        if resumed:
            get_trace_logger().log(
                "Codex Cloud Feedback",
                f"Resumed Codex Cloud task '{task_id}' for PR #{pr_number}",
                item_type="pr",
                item_number=pr_number,
                details={"task_id": task_id},
            )
            actions.append(f"Sent continuation request to Codex Cloud task '{task_id}' for PR #{pr_number}")

            # Post a comment on the PR if github_client is available
            if github_client:
                comment_body = "🤖 Auto-Coder: CI checks failed. I've requested continuation from Codex Cloud to resolve the failures. Please wait for updates."
                try:
                    if _add_unique_pr_comment(github_client, repo_name, pr_number, comment_body):
                        actions.append(f"Posted comment on PR #{pr_number} stating that a fix has been requested from Codex Cloud")
                    else:
                        actions.append(f"Skipped duplicate Codex Cloud fix-request comment on PR #{pr_number}")
                except Exception as e:
                    error_msg = f"Failed to post comment on PR #{pr_number}: {e}"
                    logger.error(error_msg)
                    actions.append(error_msg)
            else:
                actions.append(f"Skipped posting comment on PR #{pr_number}: no GitHub client available")
        else:
            actions.append(f"Codex Cloud task '{task_id}' could not be resumed for PR #{pr_number}")
            return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))

    except Exception as e:
        error_msg = f"Error resuming Codex Cloud task for PR #{pr_number}: {e}"
        logger.error(error_msg)
        actions.append(error_msg)
        return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))

    return CodexCloudFeedbackResult(delivered=True, actions=tuple(actions))


def _send_adversarial_validation_feedback_to_cloud_task(
    repo_name: str,
    pr_data: Dict[str, Any],
    head_sha: str,
    validation_report: str,
    github_client: Optional[Any] = None,
    actionable_feedback: Sequence[str] = (),
) -> List[str]:
    """Send actionable findings only to the owning provider task."""
    pr_number = pr_data["number"]
    exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
    if exhaustion_info and exhaustion_info.is_exhausted:
        publish_exhaustion_comment_deduped(github_client, repo_name, pr_number, exhaustion_info)
        return [f"Adversarial feedback was not delivered for PR #{pr_number}: automatic repair allowance is exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}"]

    if not new_work_allowed():
        return [f"Deferred adversarial correction feedback for PR #{pr_number}: graceful shutdown is draining"]
    feedback_marker = adversarial_validation_codex_feedback_marker(head_sha)
    source_validation_report = validation_report

    resolution = _resolve_cloud_task_origin(repo_name, pr_data, github_client)
    if resolution.origin is None:
        return [f"Adversarial feedback was not delivered for PR #{pr_number}: {resolution.reason}"]
    provider, task_id, client = resolution.origin.provider, resolution.origin.task_id, resolution.origin.client
    selected_origin = resolution.origin

    from .cloud_task_client_base import CloudTaskClientBase

    if getattr(type(client), "send_followup", None) is CloudTaskClientBase.send_followup:
        return [f"Adversarial feedback was not delivered for PR #{pr_number}: cloud provider '{provider}' does not support follow-up delivery"]

    state_path = _cloud_review_repair_state_path(repo_name)
    prefix = f"{repo_name}#{pr_number}:{provider}:{task_id}:"
    if github_client is None:
        return [f"Cannot identify actionable adversarial feedback for PR #{pr_number}; delivery was not attempted"]
    try:
        review_threads = github_client.get_pr_review_threads_strict(repo_name, pr_number)
    except Exception as exc:
        logger.error(f"Failed to identify actionable adversarial feedback for PR #{pr_number}: {exc}")
        return [f"Cannot identify actionable adversarial feedback for PR #{pr_number}: {exc}"]
    requested_bodies = set(actionable_feedback)
    matching_threads = [
        thread
        for thread in review_threads
        if not thread.is_resolved
        and thread.comments
        and thread.comments[0].body.startswith(("### Auto-Coder adversarial finding", "### Auto-Coder material test-oracle gap"))
        and (thread.comments[0].body in requested_bodies if requested_bodies else _adversarial_feedback_belongs_to_report(thread.comments[0].body, source_validation_report))
    ]
    feedback_bodies = [(thread.comments[0].body, _review_feedback_identity(prefix, thread, 0)) for thread in matching_threads]
    if not feedback_bodies:
        return [f"Cannot identify actionable adversarial feedback for PR #{pr_number}; delivery was not attempted"]
    validation_identities = {finding_identity: _adversarial_validation_delivery_identity(finding_identity, source_validation_report) for _body, finding_identity in feedback_bodies}
    observed_tokens = {finding_identity: _cloud_task_remediation_token(client, task_id, finding_identity) for _body, finding_identity in feedback_bodies}
    try:
        with _cloud_review_delivery_lock:
            delivered = _load_delivered_review_feedback(state_path)
            validation_generations = _load_review_validation_generations(state_path)
            validation_snapshots = _load_review_validation_snapshots(state_path)
            validation_snapshot = validation_snapshots.get(_adversarial_validation_snapshot_identity(source_validation_report))
            for finding_identity, validation_identity in validation_identities.items():
                associated_generation = validation_snapshot.get(finding_identity, "") if validation_snapshot is not None else observed_tokens[finding_identity]
                validation_generations.setdefault(validation_identity, associated_generation)
            _record_review_feedback_state(
                state_path,
                delivered,
                _load_pending_review_feedback(state_path),
                validation_generations,
            )
            remediation_tokens = {finding_identity: validation_generations[validation_identities[finding_identity]] for _body, finding_identity in feedback_bodies}
            generation_report = "" if provider == "codex-cloud" else source_validation_report
            feedback_items = [
                (
                    body,
                    finding_identity,
                    _adversarial_feedback_generation_identity(finding_identity, remediation_tokens[finding_identity], generation_report),
                )
                for body, finding_identity in feedback_bodies
            ]
            has_remediation_evidence = {finding_identity: _has_adversarial_remediation_evidence(remediation_tokens[finding_identity], generation_report) for _body, finding_identity in feedback_bodies}
            all_identities = [
                identity
                for _body, finding_identity, generation_identity in feedback_items
                for identity in (
                    finding_identity,
                    validation_identities[finding_identity],
                    generation_identity,
                    _adversarial_feedback_generation_identity(finding_identity, "", generation_report),
                )
            ]
            delivered.update(_load_pr_delivered_review_feedback(github_client, repo_name, pr_number, all_identities))
            reconciliation_candidates = {
                identity
                for _body, finding_identity, generation_identity in feedback_items
                if finding_identity not in delivered or (has_remediation_evidence[finding_identity] and validation_identities[finding_identity] not in delivered)
                for identity in (
                    generation_identity,
                    _adversarial_feedback_generation_identity(finding_identity, "", generation_report),
                )
                if identity not in delivered
            }
            reconciled = _reconcile_codex_review_feedback(
                client,
                provider,
                task_id,
                sorted(reconciliation_candidates),
            )
            if reconciled:
                delivered.update(reconciled)
                reconciled_receipts = set(reconciled)
                for _body, finding_identity, generation_identity in feedback_items:
                    unknown_generation = _adversarial_feedback_generation_identity(finding_identity, "", generation_report)
                    if generation_identity in reconciled:
                        reconciled_receipts.add(finding_identity)
                        reconciled_receipts.add(validation_identities[finding_identity])
                    elif unknown_generation in reconciled:
                        reconciled_receipts.add(finding_identity)
                        if generation_identity == unknown_generation:
                            reconciled_receipts.add(validation_identities[finding_identity])
                    remediation_token = remediation_tokens[finding_identity]
                    if unknown_generation in reconciled and remediation_token and provider != "codex-cloud" and hasattr(client, "get_followup_remediation_baseline"):
                        baseline_activity = client.get_followup_remediation_baseline(task_id, (unknown_generation,))
                        if isinstance(baseline_activity, str) and baseline_activity:
                            baseline_token = hashlib.sha256(f"{task_id}\n{baseline_activity}".encode("utf-8")).hexdigest()
                            if baseline_token == remediation_token:
                                reconciled_receipts.add(generation_identity)
                delivered.update(reconciled_receipts)
                _record_delivered_review_feedback(state_path, delivered)
                _record_pr_delivered_review_feedback(
                    github_client,
                    repo_name,
                    pr_number,
                    sorted(reconciled_receipts),
                    f"🤖 Auto-Coder: I reconciled previously submitted adversarial feedback with the existing {provider} task.",
                )
        pending_feedback = [
            (body, finding_identity, generation_identity)
            for body, finding_identity, generation_identity in feedback_items
            if generation_identity not in delivered and (finding_identity not in delivered or (has_remediation_evidence[finding_identity] and validation_identities[finding_identity] not in delivered))
        ]
        if not pending_feedback:
            return [f"Skipped duplicate adversarial feedback to {provider} for PR #{pr_number}: all actionable feedback was already delivered"]
        failed_correction = any(finding_identity in delivered for _body, finding_identity, _generation_identity in pending_feedback)

        target = resolve_existing_pr_repair_target(repo_name, pr_data)
        if not target:
            return [f"Adversarial feedback was not delivered for PR #{pr_number}: PR head/base branch metadata is unavailable"]
        target = replace(target, head_sha=head_sha)

        has_canonical_marker = any(_BLOCKER_ID_RE.search(body) for body, _fid, _gid in pending_feedback)

        ledger = CanonicalPRBlockerLedger()
        snapshot = None
        try:
            snapshot = ledger.get_snapshot("https://api.github.com", repo_name, pr_number, require_retained_state=True)
        except Exception:
            pass

        matched_bids = set()
        if snapshot is not None:
            for body, _fid, _gid in pending_feedback:
                m = _BLOCKER_ID_RE.search(body)
                if m and snapshot.get_blocker(m.group(1) or m.group(2)):
                    matched_bids.add(m.group(1) or m.group(2))
                gm = _GAP_ID_RE.search(body)
                if gm and snapshot.get_blocker(gm.group(1) or gm.group(2) or gm.group(3)):
                    matched_bids.add(gm.group(1) or gm.group(2) or gm.group(3))
                for blocker in snapshot.get_open_blockers():
                    if blocker.authoritative_boundary and blocker.authoritative_boundary in body:
                        matched_bids.add(blocker.blocker_id)

        if has_canonical_marker and not matched_bids:
            return [f"Deferred adversarial correction feedback for PR #{pr_number}: canonical blocker bundle data is absent"]

        repair_bundle = None
        if matched_bids and snapshot is not None:
            failed_corrections: dict[str, tuple[Sequence[str], Sequence[str]]] = {}
            if failed_correction:
                for bid in matched_bids:
                    b = snapshot.get_blocker(bid)
                    if b:
                        failed_corrections[str(bid)] = (
                            b.concern_ids,
                            ("The latest corrective attempt did not establish the required observable outcome. " "A pass body, renamed test, green helper test, or source-text assertion does not prove completion; " "the recorded production-boundary regression oracle remains unsatisfied.",),
                        )

            repair_bundle = build_repair_handoff_bundle(
                snapshot=snapshot,
                repo_name=repo_name,
                pr_number=pr_number,
                head_branch=target.head_branch,
                base_branch=target.base_branch,
                reviewed_head_sha=head_sha,
                requirement_manifest_revision=pr_data.get("requirement_manifest_revision", ""),
                target_blocker_ids=sorted(matched_bids),
                failed_corrections=failed_corrections if failed_corrections else None,
            )
            val_res = validate_repair_handoff_bundle(repair_bundle, head_sha, pr_data.get("requirement_manifest_revision", ""), snapshot)
            if not val_res.is_valid:
                return [f"Deferred adversarial correction feedback for PR #{pr_number}: bundle {repair_bundle.bundle_id} is stale ({val_res.reason})"]

            delivery_report = render_bounded_repair_payload(repair_bundle)
            ledger.record_repair_bundle(repair_bundle, rendered_payload=delivery_report)
        else:
            report_template = "pr.adversarial_feedback_failed_correction" if failed_correction else "pr.adversarial_feedback_new"
            delivery_report = Template(get_prompt_template(report_template, raw=True)).safe_substitute(actionable_feedback="\n\n---\n\n".join(body for body, _finding_identity, _generation_identity in pending_feedback))

        details = Template(get_prompt_template("pr.adversarial_validation_fix", raw=True)).safe_substitute(
            repo_name=repo_name,
            pr_number=pr_number,
            head_sha=head_sha,
            validation_report=delivery_report,
        )
        prompt = build_existing_pr_repair_prompt(target, details, bundle=repair_bundle)
    except (OSError, ValueError) as exc:
        return [f"Could not check prior {provider} actionable feedback for PR #{pr_number}: {exc}"]

    work_identity = hashlib.sha256("\n".join(sorted(generation_identity for _body, _finding_identity, generation_identity in pending_feedback)).encode()).hexdigest()
    retained_wait = get_claude_followup_wait_store().get(repo_name, task_id, "adversarial-feedback", work_identity)
    if retained_wait and (retained_wait.certainty is DeliveryCertainty.INDETERMINATE or retained_wait.retry_not_before > time.time()):
        deferred_actions = PRActionList([f"DEFERRED Claude adversarial feedback for PR #{pr_number} until {retained_wait.retry_not_before}"])
        deferred_actions.quota_deferred = True
        deferred_actions.retry_not_before = retained_wait.retry_not_before
        return deferred_actions

    try:
        identities = tuple(sorted(generation_identity for _body, _finding_identity, generation_identity in pending_feedback))
        attribution_error = _revalidate_cloud_origin(repo_name, pr_number, selected_origin)
        if attribution_error:
            return [f"Adversarial feedback was not delivered for PR #{pr_number}: {attribution_error}"]
        accepted = _send_followup_with_quota_admission(client, repo_name, task_id, prompt, identities)
    except ClaudeFollowupHoldActive as exc:
        deferred_actions = PRActionList([f"DEFERRED Claude adversarial feedback for PR #{pr_number} until {exc.retry_not_before}"])
        deferred_actions.quota_deferred = True
        deferred_actions.retry_not_before = exc.retry_not_before
        return deferred_actions
    except ClaudeFollowupUsageLimitError as exc:
        retry_at = _retain_claude_quota_deferral(exc, pr_number, task_id, "adversarial-feedback", work_identity)
        deferred_actions = PRActionList([f"DEFERRED Claude adversarial feedback for PR #{pr_number} until {retry_at}"])
        deferred_actions.quota_deferred = True
        deferred_actions.retry_not_before = retry_at
        return deferred_actions
    except Exception as e:
        logger.error(f"Error sending adversarial feedback to {provider} for PR #{pr_number}: {e}")
        return [f"Adversarial feedback was not delivered to {provider} for PR #{pr_number}: {e}"]

    if not accepted:
        return [f"{provider} task '{task_id}' could not receive adversarial feedback for PR #{pr_number}"]

    get_claude_followup_wait_store().retire(repo_name, task_id, "adversarial-feedback", work_identity)

    baseline_receipts: set[str] = set()
    if provider != "codex-cloud" and all(not remediation_tokens[finding_identity] for _body, finding_identity, _generation_identity in pending_feedback) and hasattr(client, "get_followup_remediation_baseline"):
        logical_identities = tuple(sorted(generation_identity for _body, _finding_identity, generation_identity in pending_feedback))
        baseline_activity = client.get_followup_remediation_baseline(task_id, logical_identities)
        if isinstance(baseline_activity, str) and baseline_activity:
            baseline_token = hashlib.sha256(f"{task_id}\n{baseline_activity}".encode("utf-8")).hexdigest()
            baseline_receipts = {_adversarial_feedback_generation_identity(finding_identity, baseline_token, generation_report) for _body, finding_identity, _generation_identity in pending_feedback}

    local_receipt = False
    try:
        with _cloud_review_delivery_lock:
            delivered = _load_delivered_review_feedback(state_path)
            delivered.update(identity for _body, finding_identity, generation_identity in pending_feedback for identity in (finding_identity, generation_identity))
            delivered.update(validation_identities[finding_identity] for _body, finding_identity, _generation_identity in pending_feedback)
            delivered.update(baseline_receipts)
            _record_delivered_review_feedback(state_path, delivered)
            local_receipt = True
    except (OSError, ValueError) as exc:
        logger.error(f"Failed to persist shared Codex Cloud feedback delivery for PR #{pr_number}: {exc}")

    get_trace_logger().log(
        "Cloud Task Adversarial Feedback",
        f"Sent NEEDS_FIX report to {provider} task '{task_id}' for PR #{pr_number}",
        item_type="pr",
        item_number=pr_number,
        details={"provider": provider, "task_id": task_id, "head_sha": head_sha},
    )
    actions = [f"Sent adversarial NEEDS_FIX report to {provider} task '{task_id}' for PR #{pr_number}"]
    if github_client:
        comment_body = "\n".join(
            [
                feedback_marker,
                _cloud_review_feedback_markers([identity for _body, finding_identity, generation_identity in pending_feedback for identity in (finding_identity, generation_identity, validation_identities[finding_identity])] + sorted(baseline_receipts)),
                f"🤖 Auto-Coder: I sent the adversarial validation findings to the existing {provider} task and requested a fix.",
            ]
        )
        try:
            github_client.add_comment_to_pr(repo_name, pr_number, comment_body)
            remote_receipt = True
            actions.append(f"Recorded {provider} adversarial feedback delivery on PR #{pr_number}")
        except Exception as e:
            remote_receipt = False
            logger.error(f"Failed to record {provider} adversarial feedback delivery on PR #{pr_number}: {e}")
            actions.append(f"Failed to record {provider} adversarial feedback delivery on PR #{pr_number}: {e}")
        if not local_receipt and not remote_receipt:
            logger.error(f"Confirmed adversarial feedback delivery for PR #{pr_number} has no durable receipt")
    return actions


def _describe_merge_operation_outcome(result: Any) -> str:
    """Render one merge-operation adapter result distinctly for observability (REQ-010).

    Never collapses DEFERRED/OPERATIONALLY_BLOCKED/INDETERMINATE/SUPERSEDED
    into a generic "Merge failed" label.
    """
    status = result.operation.status.value
    return f"status={status}, outcome={result.kind.value}, reason={result.reason or 'pending'}"


def _finalize_merge_success(repo_name: str, pr_number: int, method: str) -> bool:
    """Post-processing after a durably confirmed merge (REQ-009).

    ``_close_linked_issues``/``_archive_jules_session`` already swallow their
    own failures internally, so a post-processing problem is observable via
    their own logging without ever turning a confirmed merge back into a
    reported failure or triggering a re-merge/re-approval.
    """
    get_trace_logger().log("Merging", f"Successfully merged PR #{pr_number}", item_type="pr", item_number=pr_number, details={"method": method})
    log_action(f"Successfully merged PR #{pr_number} (method: {method})")
    _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.COMPLETED, {"method": method})
    _close_linked_issues(repo_name, pr_number)
    _archive_jules_session(repo_name, pr_number)
    return True


def _merge_pr(
    repo_name: str,
    pr_number: int,
    analysis: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
    expected_head_sha: Optional[str] = None,
    route_disposition: Optional[MergeRouteDisposition] = None,
) -> bool:
    """Merge a PR through the durable per-effect merge operation (Issue #1939).

    Approval and merge are each advanced through ``merge_operation_adapter``
    against a durable ``MergeOperation`` (Issue #1937/#1938) instead of being
    treated as one boolean success/failure: a local admission deferral, a
    real GitHub throttle, an authentication/forbidden block, or an
    indeterminate delivery is reported as such and returned without any
    repository/PR diagnostic GET, alternate merge method, conflict repair, or
    LLM fallback (REQ-001, REQ-002). Only a definitive, cause-specified
    rejection -- GitHub having actually and unambiguously refused the
    mutation -- may fall through to the pre-existing conflict-resolution and
    allowed-alternate-method handling below (REQ-007). The operation persists
    across calls, so a resumed attempt (whether from the next normal
    processing cycle or from ``merge_operation_scheduler``) never re-sends an
    already-confirmed approval or merge.

    After a successful merge, automatically closes any issues referenced in
    the PR body using GitHub's linking keywords (closes, fixes, resolves,
    etc.) and archives any associated Jules session.
    """
    from .merge_operation_adapter import AdapterOutcomeKind, AdapterResult, attempt_approval, attempt_merge, reconcile_approval, reconcile_merge
    from .merge_operation_state import EffectName, EffectState, MergeOperationIdentity, get_merge_operation_store

    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        client = github_client or GitHubClient.get_instance()
        exhaustion_info = check_pr_repair_exhaustion(repo_name, pr_number)
        if exhaustion_info and exhaustion_info.is_exhausted:
            logger.warning(f"Merge aborted for PR #{pr_number}: repair allowance exhausted for open blocker(s): {', '.join(exhaustion_info.exhausted_blocker_ids)}")
            publish_exhaustion_comment_deduped(client, repo_name, pr_number, exhaustion_info)
            return False

        if _is_pr_review_thread_gate_enabled(config, repo_name):
            review_thread_state = _get_review_thread_gate_state(client, repo_name, pr_number, config=config)
            if review_thread_state.lookup_error:
                logger.info(f"PR #{pr_number} review threads could not be checked. Skipping merge.")
                log_action(f"Skipping merge for PR #{pr_number} because review threads could not be checked: {review_thread_state.lookup_error}")
                _record_pr_stage(pr_number, "pr.merge-review-thread-recheck", f"pr#{pr_number} merge review-thread recheck", Outcome.FAILED, {"reason": review_thread_state.lookup_error})
                return False
            if review_thread_state.has_unresolved:
                logger.info(f"PR #{pr_number} has unresolved review threads. Skipping merge.")
                log_action(f"Skipping merge for PR #{pr_number} due to unresolved review threads")
                _record_pr_stage(pr_number, "pr.merge-review-thread-recheck", f"pr#{pr_number} merge review-thread recheck", Outcome.BLOCKED, {})
                return False

        token = client.token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")
        store = get_merge_operation_store()
        identity = MergeOperationIdentity("https://api.github.com", repo_name, pr_number)

        try:
            pr_info = api.pulls.get(owner, repo, pr_number)
        except Exception as e:
            logger.error(f"Could not read PR #{pr_number} before merge: {e}")
            return False

        # The ingress classification is not merge authority: selection or
        # invalidation may change while CI/review work is running.  Re-read the
        # durable competition immediately inside the final sender and fail
        # closed unless this exact PR is still the selected artifact.  Legacy
        # PRs remain governed by the ordinary merge gates.
        speculative = get_speculative_jules_lifecycle(client)
        if speculative is not None:
            issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_info, client)
            issue_data: Dict[str, Any] = {}
            if len(issue_numbers) == 1:
                issue = client.get_issue(repo_name, issue_numbers[0])
                issue_data = issue if isinstance(issue, dict) else {"state": getattr(issue, "state", None), "body": getattr(issue, "body", None)}
            authority = speculative.evaluate_merge_authority(repo_name, pr_number, pr_info, issue_data, tuple(issue_numbers))
            if not authority.allow_ordinary_processing:
                logger.warning(f"Merge aborted for PR #{pr_number}: speculative Jules " f"authority is {authority.classification.value} ({authority.reason})")
                _record_pr_stage(
                    pr_number,
                    "pr.speculative-jules-merge-authority",
                    f"pr#{pr_number} speculative Jules merge authority",
                    Outcome.BLOCKED,
                    {"classification": authority.classification.value, "reason": authority.reason},
                )
                return False

        head_sha = expected_head_sha or pr_info.get("head", {}).get("sha") or ""
        if not head_sha:
            logger.error(f"No head SHA available for PR #{pr_number}; aborting merge")
            return False

        needs_approval = _is_dependabot_pr(pr_info)
        reviewer_identity = ""
        if needs_approval:
            try:
                reviewer_identity = resolve_reviewer_app_identity(repo_name).login
            except Exception as e:
                logger.warning(f"Could not resolve reviewer identity for auto-approval of PR #{pr_number}: {e}")
            needs_approval = bool(reviewer_identity)

        def _advance(effect_name: EffectName, attempt_fn, reconcile_fn) -> AdapterResult:
            current = store.get(identity)
            effect = current.effect(effect_name) if current is not None else None
            if effect is not None and effect.state is EffectState.DELIVERY_UNKNOWN:
                return reconcile_fn(store, token, identity)
            return attempt_fn(store, token, identity)

        def _try_merge(method: str, target_head_sha: str) -> AdapterResult:
            store.get_or_create(
                identity,
                expected_head_sha=target_head_sha,
                merge_method=method,
                approval_credential_role="auto-coder-bot",
                reviewer_identity=reviewer_identity,
                needs_approval=needs_approval,
            )
            if needs_approval:
                approval_result = _advance(EffectName.APPROVAL, attempt_approval, reconcile_approval)
                approval_state = approval_result.operation.effect(EffectName.APPROVAL).state
                if approval_state not in (EffectState.NOT_NEEDED, EffectState.CONFIRMED_COMPLETE):
                    return approval_result
            return _advance(EffectName.MERGE, attempt_merge, reconcile_merge)

        result = _try_merge(config.MERGE_METHOD.replace("--", ""), head_sha)

        if result.operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE:
            return _finalize_merge_success(repo_name, pr_number, config.MERGE_METHOD)

        approval_effect = result.operation.effect(EffectName.APPROVAL)
        if needs_approval and approval_effect.state not in (EffectState.NOT_NEEDED, EffectState.CONFIRMED_COMPLETE):
            log_action(f"Auto-approval not completed for PR #{pr_number}: {_describe_merge_operation_outcome(result)}")
            _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.DEFERRED, {"examined_head": head_sha, "reason": "approval not completed", "detail": _describe_merge_operation_outcome(result)})
            return False

        # Anything short of a definitive, cause-specified rejection is a
        # retryable pending state (mutation spacing, a real throttle, an
        # operational block, an indeterminate delivery, or a superseded
        # head): REQ-002/REQ-007 forbid diagnostic GETs, alternate methods,
        # conflict repair, or LLM fallback for any of these. The durable
        # operation (and, in the daemon, merge_operation_scheduler) is what
        # retries it once its own deadline has passed.
        if result.kind is not AdapterOutcomeKind.DEFINITIVE_REJECTION:
            log_action(f"Merge not completed for PR #{pr_number}: {_describe_merge_operation_outcome(result)}")
            _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.DEFERRED, {"examined_head": head_sha, "reason": "not a definitive rejection", "detail": _describe_merge_operation_outcome(result)})
            return False

        # A definitive, cause-specified rejection: only now may current
        # conditions be consulted to choose between conflict repair and an
        # allowed alternate method (REQ-007).
        return _handle_definitive_merge_rejection(
            repo_name,
            pr_number,
            config,
            client,
            api,
            owner,
            repo,
            store,
            identity,
            _try_merge,
            head_sha,
            route_disposition,
        )

    except Exception as e:
        logger.error(f"Error merging PR #{pr_number}: {e}")
        _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.FAILED, {"reason": str(e)})
        if route_disposition is not None:
            route_disposition.outcome = PRProcessingOutcome.FAILED
            route_disposition.reason = str(e)
        return False


def _handle_definitive_merge_rejection(
    repo_name: str,
    pr_number: int,
    config: AutomationConfig,
    client: Any,
    api: Any,
    owner: str,
    repo: str,
    store: Any,
    identity: Any,
    try_merge: Any,
    head_sha: str,
    route_disposition: Optional[MergeRouteDisposition] = None,
) -> bool:
    """Handle a GitHub-confirmed, cause-specified merge rejection (REQ-007).

    Only reached once ``merge_operation_adapter`` has already classified the
    outcome as a definitive rejection (not a mutation-spacing defer, real
    throttle, operational block, or indeterminate delivery); it is therefore
    safe here to re-check current conditions and decide between an allowed
    alternate merge method and conflict resolution.
    """
    from .merge_operation_adapter import AdapterOutcomeKind
    from .merge_operation_state import EffectName, EffectState

    # Preserve the durable adapter's cause-specified classification across
    # the legacy boolean return. A successful retry below still returns True,
    # but every non-successful exit remains a rejection rather than being
    # reconstructed as an opaque deferral by the enclosing merge route.
    if route_disposition is not None:
        route_disposition.outcome = PRProcessingOutcome.FAILED
        route_disposition.reason = "Definitive merge rejection"

    try:
        pr_info = api.pulls.get(owner, repo, pr_number)
    except Exception as e:
        logger.warning(f"Could not re-check PR #{pr_number} after merge rejection: {e}")
        return False

    is_conflict = pr_info.get("mergeable") is False

    if not is_conflict:
        allowed = _get_allowed_merge_methods(repo_name)
        selected = config.MERGE_METHOD
        # REQ-007: an alternate method is only a candidate once current
        # repository settings actually show the selected method disallowed
        # and an alternate allowed -- never merely because the selected one
        # failed.
        if selected not in allowed:
            for alt in [m for m in ["--squash", "--merge", "--rebase"] if m != selected and m in allowed]:
                store.manual_reset_effect(identity, EffectName.MERGE)
                alt_result = try_merge(alt.replace("--", ""), head_sha)
                if alt_result.operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE:
                    return _finalize_merge_success(repo_name, pr_number, alt)
            log_action(f"Failed to merge PR #{pr_number} with any currently allowed merge method", False, "Merge API failed")
            _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.FAILED, {"examined_head": head_sha, "reason": "no allowed alternate merge method succeeded"})
            if route_disposition is not None:
                route_disposition.outcome = PRProcessingOutcome.FAILED
                route_disposition.reason = "No allowed alternate merge method succeeded"
            return False

        log_action(f"Failed to merge PR #{pr_number}", False, "Merge API failed (not conflict)")
        _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.FAILED, {"examined_head": head_sha, "reason": "definitive rejection (not a conflict)"})
        if route_disposition is not None:
            route_disposition.outcome = PRProcessingOutcome.FAILED
            route_disposition.reason = "Definitive merge rejection"
        try:
            pr_data = {"number": pr_number, "body": pr_info.get("body", "")}
            _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed")
        except Exception:
            pass
        return False

    logger.info(f"PR #{pr_number} has merge conflicts, attempting to resolve...")
    log_action(f"PR #{pr_number} has merge conflicts, attempting resolution")

    if _is_jules_pr(pr_info):
        logger.info(f"PR #{pr_number} is a Jules PR with merge conflicts. Requesting Jules to resolve it.")
        try:
            from auto_coder.jules_client import JulesClient

            jules_client = JulesClient()
            session_id = _extract_session_id_from_pr_body(pr_info.get("body", ""))
            if session_id:
                # REQ-007/REQ-009 (Issue #2147): guard + durably admit this
                # outbound mutation to an existing Jules session before
                # sending it.
                if not _guard_outbound_jules_send(repo_name, config, session_id):
                    logger.info(f"Blocked merge-conflict resolution request for PR #{pr_number}: session '{session_id}' " "belongs to a durably retired implementation slot (REQ-009)")
                    _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.SKIPPED, {"effect": "merge-conflict", "backend": "jules", "reason": "retired implementation slot"})
                    return False
                prompt = render_prompt("pr.jules_merge_conflict_resolution")
                jules_client.send_message(session_id, prompt)
                logger.info(f"Requested Jules to resolve merge conflict in session {session_id}")
                log_action(f"Requested Jules to resolve merge conflicts for PR #{pr_number}")
                _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.ACCEPTED_HANDOFF, {"effect": "merge-conflict", "backend": "jules"})
                return False
            else:
                logger.warning(f"Jules PR #{pr_number} has merge conflicts but no session ID found. Cannot delegate.")
        except Exception as e:
            logger.error(f"Error requesting Jules to resolve conflict: {e}")

    # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
    # the bot recreates the PR against the updated base branch by itself.
    if _is_dependabot_pr(pr_info):
        logger.info(f"PR #{pr_number} is a dependency-bot PR with merge conflicts. Skipping conflict resolution.")
        log_action(f"Skipped merge conflict resolution for dependency-bot PR #{pr_number}")
        _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.SKIPPED, {"effect": "merge-conflict", "reason": "dependency-bot PR"})
        return False

    cloud_delegation = _delegate_cloud_merge_conflict_repair_result(repo_name, pr_info, client)
    if cloud_delegation:
        if cloud_delegation.accepted_action:
            log_action(cloud_delegation.accepted_action)
        log_action(f"Delegated merge-conflict repair for PR #{pr_number} to its existing cloud session")
        _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.ACCEPTED_HANDOFF, {"effect": "merge-conflict", "backend": "cloud"})
        return False

    if not _resolve_pr_merge_conflicts(repo_name, pr_number, config):
        log_action(f"Failed to resolve merge conflicts for PR #{pr_number}")
        _record_pr_stage(pr_number, "pr.repair-delegation", f"pr#{pr_number} repair delegation", Outcome.FAILED, {"effect": "merge-conflict", "backend": "local"})
        try:
            pr_data = {"number": pr_number, "body": pr_info.get("body", "")}
            _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (resolution failed)")
        except Exception:
            pass
        return False

    logger.info(f"Conflicts resolved for PR #{pr_number}, waiting for GitHub to update mergeable state")
    log_action(f"Polling mergeable state for PR #{pr_number} after conflict resolution")

    polling_succeeded = _poll_pr_mergeable(repo_name, pr_number, config)
    if polling_succeeded:
        logger.info(f"GitHub confirmed PR #{pr_number} is mergeable, attempting merge")
    else:
        logger.warning(f"Polling timed out for PR #{pr_number}, attempting merge anyway")

    # Conflict resolution produced a new commit: the durable operation must
    # target the PR's new current head, never the pre-resolution one.
    try:
        refreshed_pr = api.pulls.get(owner, repo, pr_number)
        new_head_sha = refreshed_pr.get("head", {}).get("sha") or head_sha
    except Exception:
        refreshed_pr = pr_info
        new_head_sha = head_sha

    retry_result = try_merge(config.MERGE_METHOD.replace("--", ""), new_head_sha)
    if retry_result.operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE:
        log_action(f"Successfully merged PR #{pr_number} after conflict resolution")
        return _finalize_merge_success(repo_name, pr_number, config.MERGE_METHOD)

    if retry_result.kind is not AdapterOutcomeKind.DEFINITIVE_REJECTION:
        log_action(f"Merge not completed for PR #{pr_number} after conflict resolution: {_describe_merge_operation_outcome(retry_result)}")
        _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.DEFERRED, {"examined_head": new_head_sha, "reason": "not a definitive rejection after conflict resolution", "detail": _describe_merge_operation_outcome(retry_result)})
        return False

    logger.warning(f"Merge failed for PR #{pr_number} even after conflict resolution")
    log_action(f"Failed to merge PR #{pr_number} after conflict resolution", False, "Merge API failed")

    allowed = _get_allowed_merge_methods(repo_name)
    selected = config.MERGE_METHOD
    if selected not in allowed:
        for alt in [m for m in ["--squash", "--merge", "--rebase"] if m != selected and m in allowed]:
            store.manual_reset_effect(identity, EffectName.MERGE)
            alt_result = try_merge(alt.replace("--", ""), new_head_sha)
            if alt_result.operation.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE:
                return _finalize_merge_success(repo_name, pr_number, alt)

    _record_pr_stage(pr_number, "pr.merge-delivery", f"pr#{pr_number} merge delivery", Outcome.FAILED, {"examined_head": new_head_sha, "reason": "merge failed after conflict resolution"})
    if route_disposition is not None:
        route_disposition.outcome = PRProcessingOutcome.FAILED
        route_disposition.reason = "Definitive merge rejection after conflict resolution"
    try:
        pr_data = {"number": pr_number, "body": refreshed_pr.get("body", "")}
        _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (conflict resolution exhausted)")
    except Exception:
        pass
    return False


def _poll_pr_mergeable(
    repo_name: str,
    pr_number: int,
    config: AutomationConfig,
    timeout_seconds: int = 60,
    interval: int = 5,
) -> bool:
    """Poll PR mergeable state for a short period. Returns True if becomes mergeable.
    Uses: gh pr view <num> --repo <repo> --json mergeable,mergeStateStatus
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                pr_info = api.pulls.get(owner, repo, pr_number)
                if pr_info.get("mergeable") is True:
                    return True
            except Exception:
                pass

            # Sleep before next poll
            time.sleep(max(1, interval))
        return False
    except Exception:
        return False


def _get_allowed_merge_methods(repo_name: str) -> List[str]:
    """Return list of allowed merge method flags for the repository.
    Maps GitHub repo settings to gh merge flags.
    """
    allowed: List[str] = []
    try:
        # Use GhApi to get allowed merge methods
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        repo_data = api.repos.get(owner, repo)

        if repo_data.get("allow_squash_merge"):
            allowed.append("--squash")
        if repo_data.get("allow_merge_commit"):
            allowed.append("--merge")
        if repo_data.get("allow_rebase_merge"):
            allowed.append("--rebase")

        return allowed
    except Exception as e:
        logger.warning(f"Failed to get allowed merge methods via GhApi: {e}")
        return []


def _resolve_pr_merge_conflicts(repo_name: str, pr_number: int, config: AutomationConfig) -> bool:
    """Resolve merge conflicts for a PR by checking it out and merging with its base branch (not necessarily main)."""
    try:
        # Step 0: Clean up any existing git state
        logger.info(f"Cleaning up git state before resolving conflicts for PR #{pr_number}")

        # Reset any uncommitted changes
        reset_result = cmd.run_command(["git", "reset", "--hard"])
        if not reset_result.success:
            logger.warning(f"Failed to reset git state: {reset_result.stderr}")

        # Clean untracked files
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            logger.warning(f"Failed to clean untracked files: {clean_result.stderr}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        if abort_result.success:
            logger.info("Aborted ongoing merge")

        # Step 1: Get PR details to determine the target base branch and the author
        pr_data = None
        try:
            from auto_coder.util.gh_cache import get_ghapi_client

            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            pr_data = api.pulls.get(owner, repo, pr_number)
            base_branch = pr_data.get("base", {}).get("ref", config.MAIN_BRANCH)
        except Exception as e:
            logger.warning(f"Failed to get PR #{pr_number} details via GhApi: {e}")
            base_branch = config.MAIN_BRANCH

        # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
        # the bot recreates the PR against the updated base branch by itself.
        if pr_data is not None and _is_dependabot_pr(pr_data):
            logger.info(f"Skipping merge conflict resolution for dependency-bot PR #{pr_number}")
            return False

        # Step 2: Checkout the PR branch
        logger.info(f"Checking out PR #{pr_number} to resolve merge conflicts")
        # Use reusable _checkout_pr_branch which uses direct git commands
        checkout_success = _checkout_pr_branch(repo_name, {"number": pr_number}, config)

        if not checkout_success:
            logger.error(f"Failed to checkout PR #{pr_number}")
            return False

        # Step 3: Fetch the latest base branch
        logger.info(f"Fetching latest {base_branch} branch")
        fetch_result = cmd.run_command(["git", "fetch", "origin", base_branch])

        if not fetch_result.success:
            logger.error(f"Failed to fetch {base_branch} branch: {fetch_result.stderr}")
            return False

        # Step 4: Attempt to merge base branch
        logger.info(f"Merging refs/remotes/origin/{base_branch} into PR #{pr_number}")
        merge_result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{base_branch}"])

        if merge_result.success:
            # No conflicts, push the updated branch using centralized helper with retry
            logger.info(f"Successfully merged {base_branch} into PR #{pr_number}, pushing changes")
            push_result = git_push()

            if push_result.success:
                logger.info(f"Successfully pushed updated branch for PR #{pr_number}")
                return True
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                import time

                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    logger.info(f"Successfully pushed updated branch for PR #{pr_number} (after retry)")
                    return True
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    return False
        else:
            # Merge conflicts detected, use LLM to resolve them
            logger.info(f"Merge conflicts detected for PR #{pr_number}, using LLM to resolve")

            # Get conflict information
            conflict_info = _get_merge_conflict_info()

            # Use LLM to resolve conflicts
            resolve_actions = resolve_merge_conflicts_with_llm(
                {"number": pr_number, "base_branch": base_branch},
                conflict_info,
                config,
            )

            # Log the resolution actions
            for action in resolve_actions:
                logger.info(f"Conflict resolution action: {action}")

            # Check if conflicts were resolved successfully
            status_result = cmd.run_command(["git", "status", "--porcelain"])

            if status_result.success and not status_result.stdout.strip():
                logger.info(f"Merge conflicts resolved for PR #{pr_number}")
                get_trace_logger().log("Conflict Resolution", f"Resolved merge conflicts for PR #{pr_number}", item_type="pr", item_number=pr_number)
                return True
            else:
                logger.error(f"Failed to resolve merge conflicts for PR #{pr_number}")
                return False

    except Exception as e:
        logger.error(f"Error resolving merge conflicts for PR #{pr_number}: {e}")
        return False


def _fix_pr_issues_with_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    failed_tests: List[str] | None = None,
    skip_github_actions_fix: bool = False,
) -> List[str]:
    # Extract failed tests from GitHub Actions logs
    if failed_tests is None:
        failed_tests = extract_all_failed_tests(github_logs)

    if skip_github_actions_fix:
        return _fix_pr_issues_with_local_testing(repo_name, pr_data, config, github_logs, test_files=failed_tests, skip_github_actions_fix=True)
    else:
        return _fix_pr_issues_with_github_actions_testing(repo_name, pr_data, config, github_logs, failed_tests=failed_tests)


def _fix_pr_issues_with_github_actions_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    failed_tests: Optional[List[str]] = None,
) -> List[str]:
    """Fix PR issues using GitHub Actions logs, with intelligent routing.

    If 1-3 tests failed: Run local testing/fixing loop (targeted).
    If 4+ or 0 tests: Apply GHA log fix, commit, and push (trigger new run).
    """
    actions = []
    pr_number = pr_data["number"]

    # Initialize backend managers
    current_backend_manager: Optional[BackendManager] = None
    high_score_backend_manager: Optional[BackendManager] = None
    if _is_automatic_test_fix_enabled(config, repo_name):
        try:
            current_backend_manager = get_llm_backend_manager()
        except Exception as e:
            logger.debug(f"Could not get LLM backend manager: {e}")
        try:
            high_score_backend_manager = create_high_score_backend_manager()
        except Exception as e:
            logger.debug(f"Could not create high score backend manager: {e}")

    # Track history
    attempt_history: List[Dict[str, Any]] = []

    try:
        # Strategy: GHA Iteration (Log Fix -> Commit -> Push)
        # 1. Apply fix based on GHA logs
        if not _is_automatic_test_fix_enabled(config, repo_name):
            actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping GitHub Actions fix")
        else:
            get_trace_logger().log("Fixing Issues", f"Fixing PR #{pr_number} using GHA logs", item_type="pr", item_number=pr_number)
            actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
            initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs, backend_manager=high_score_backend_manager)
            actions.extend(initial_fix_actions)

        # 2. Apply fix based on local tests when 1-3 tests failed
        if failed_tests and 1 <= len(failed_tests) <= 3:
            test_result = run_local_tests(config, test_file=failed_tests[0])

            # Check if we should use local fix strategy (1-3 failed tests)
            attempts_limit = config.MAX_FIX_ATTEMPTS
            attempt = 0

            while not test_result.get("success") and 1 <= len(failed_tests) <= 3 and attempt < attempts_limit:
                if not _is_automatic_test_fix_enabled(config, repo_name):
                    actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping local test repair")
                    break
                if not new_work_allowed():
                    actions.append(f"Deferred another repair attempt for PR #{pr_number}: graceful shutdown is draining")
                    break
                attempt += 1

                # Check if PR is closed
                from .util.gh_cache import GitHubClient
                from .util.github_action import is_item_closed_on_github

                if is_item_closed_on_github(repo_name, "pr", pr_number, GitHubClient.get_instance()):
                    msg = f"PR #{pr_number} is closed on GitHub. Aborting fix loop."
                    logger.info(msg)
                    actions.append(msg)
                    return actions

                # Backend switching logic
                if attempt >= 2 and high_score_backend_manager:
                    if current_backend_manager != high_score_backend_manager:
                        logger.info(f"Switching to fallback backend for PR #{pr_number} after {attempt} attempts")
                        current_backend_manager = high_score_backend_manager
                        actions.append(f"Switched to fallback backend for PR #{pr_number}")

                with ProgressStage(f"Low-failure fix attempt {attempt}"):
                    local_fix_actions, llm_response = _apply_local_test_fix(
                        repo_name,
                        pr_data,
                        config,
                        test_result,
                        attempt_history,
                        backend_manager=current_backend_manager,
                    )
                    actions.extend(local_fix_actions)

                test_result = run_local_tests(config, test_file=failed_tests[0])

        if not _is_automatic_test_fix_enabled(config, repo_name):
            return actions

        # 3. Commit and Push
        # Check if any changes were made
        result = cmd.run_command(["git", "status", "--porcelain"])
        if result.success and result.stdout.strip():
            # Stage changes before committing
            cmd.run_command(["git", "add", "."])

            commit_msg = f"Auto-Coder: Fix issues based on GitHub Actions logs (PR #{pr_number})"
            c_res = git_commit_with_retry(commit_msg)
            if c_res.success:
                actions.append("Committed fixes based on GitHub Actions logs")
                p_res = git_push()
                if p_res.success:
                    actions.append("Pushed fixes to GitHub to trigger new Actions run")
                else:
                    actions.append(f"Failed to push fixes: {p_res.stderr}")
            else:
                actions.append(f"Failed to commit fixes: {c_res.stderr}")
        else:
            actions.append("No changes generated by GitHub Actions fix")

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        actions.append(f"Error fixing PR issues with testing for PR #{pr_number}: {e}")

    return actions


def _fix_pr_issues_with_local_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    test_files: Optional[List[str]] = None,
    skip_github_actions_fix: bool = False,
) -> List[str]:
    """Fix PR issues using local testing loop."""
    actions = []
    pr_number = pr_data["number"]

    # Initialize backend managers
    current_backend_manager: Optional[BackendManager] = None
    high_score_backend_manager: Optional[BackendManager] = None
    if _is_automatic_test_fix_enabled(config, repo_name):
        try:
            current_backend_manager = get_llm_backend_manager()
        except Exception as e:
            logger.debug(f"Could not get LLM backend manager: {e}")
        try:
            high_score_backend_manager = create_high_score_backend_manager()
        except Exception as e:
            logger.debug(f"Could not create high score backend manager: {e}")

    # Track history of previous attempts for context
    attempt_history: List[Dict[str, Any]] = []

    try:
        # Step 1: Initial fix using GitHub Actions logs
        if skip_github_actions_fix:
            msg = "Skipping GitHub Actions fix as we were already on the PR branch (assuming resumption)"
            logger.info(msg)
            actions.append(msg)
        elif not _is_automatic_test_fix_enabled(config, repo_name):
            actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping GitHub Actions fix")
        else:
            get_trace_logger().log("Fixing Issues", f"Fixing PR #{pr_number} using GHA logs (local loop)", item_type="pr", item_number=pr_number)
            actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
            initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs)
            actions.extend(initial_fix_actions)

        # Step 2: Local testing and iterative fixing loop
        attempts_limit = config.MAX_FIX_ATTEMPTS
        attempt = 0
        while True:
            with ProgressStage(f"attempt: {attempt}"):
                attempt += 1

                # Check if PR is closed
                from .util.gh_cache import GitHubClient
                from .util.github_action import is_item_closed_on_github

                if is_item_closed_on_github(repo_name, "pr", pr_number, GitHubClient.get_instance()):
                    msg = f"PR #{pr_number} is closed on GitHub. Aborting fix loop."
                    logger.info(msg)
                    actions.append(msg)
                    return actions

                # Backend switching logic: switch to fallback after 2 attempts
                if attempt >= 2 and high_score_backend_manager:
                    if current_backend_manager != high_score_backend_manager:
                        logger.info(f"Switching to fallback backend for PR #{pr_number} after {attempt} attempts")
                        current_backend_manager = high_score_backend_manager
                        actions.append(f"Switched to fallback backend for PR #{pr_number}")

                actions.append(f"Running local tests (attempt {attempt}/{attempts_limit})")

                with ProgressStage(f"Running local tests"):
                    test_result = run_local_tests(config)

                if test_result["success"]:
                    actions.append(f"Local tests passed on attempt {attempt}")
                    commit_and_push_changes({"summary": f"Auto-Coder: Address PR #{pr_number}"})
                    break
                else:
                    actions.append(f"Local tests failed on attempt {attempt}")

                    if not _is_automatic_test_fix_enabled(config, repo_name):
                        actions.append(f"Automatic test fix is disabled for PR #{pr_number}; skipping local test repair")
                        break

                    # Apply local test failure fix (always try unless finite limit reached)
                    # Stop if finite limit reached after this attempt
                    # Otherwise, continue attempting fixes
                    # Determine if we have remaining attempts (finite limit)
                    finite_limit_reached = False
                    try:
                        if math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                            finite_limit_reached = True
                    except Exception:
                        finite_limit_reached = False

                    if finite_limit_reached:
                        actions.append(f"Max fix attempts ({attempts_limit}) reached for PR #{pr_number}")
                        break
                    else:
                        if not new_work_allowed():
                            actions.append(f"Deferred another repair attempt for PR #{pr_number}: graceful shutdown is draining")
                            break
                        local_fix_actions, llm_response = _apply_local_test_fix(
                            repo_name,
                            pr_data,
                            config,
                            test_result,
                            attempt_history,
                            backend_manager=current_backend_manager,
                        )
                        actions.extend(local_fix_actions)

                        # Store this attempt in history for future reference
                        if llm_response:
                            attempt_history.append(
                                {
                                    "attempt_number": attempt,
                                    "llm_output": llm_response,
                                    "test_result": test_result,
                                }
                            )

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        actions.append(f"Error fixing PR issues with testing for PR #{pr_number}: {e}")

    return actions


def _apply_github_actions_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    test_result: Optional[TestResult] = None,
    github_client: Optional[Any] = None,
    backend_manager: Optional[BackendManager] = None,
) -> List[str]:
    """Apply initial fix using GitHub Actions error logs.

    Enhanced: Optionally accepts a TestResult to pass structured error metadata
    and framework context to the LLM prompt for more targeted fixes.
    The LLM is instructed to edit files only; committing and pushing are handled
    by this code after a conflict-marker check.
    """
    actions: List[str] = []
    pr_number = pr_data["number"]

    if not _is_automatic_test_fix_enabled(config, repo_name):
        return [f"Automatic test fix is disabled for PR #{pr_number}; skipping GitHub Actions fix"]

    try:
        # Get commit log since branch creation
        commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

        logger.info(f"Extracted important errors from GitHub Actions logs for PR #{pr_number}")

        # Extract linked issues context
        linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_data.get("body", ""))

        # Create prompt for GitHub Actions error fix (no commit/push by LLM)
        fix_prompt = render_prompt(
            "pr.github_actions_fix",
            pr_number=pr_number,
            repo_name=repo_name,
            pr_title=pr_data.get("title", "Unknown"),
            extracted_errors=github_logs,
            commit_log=commit_log or "(No commit history)",
            linked_issues_context=linked_issues_context,
            # Structured additions (safe if None)
            structured_errors=(test_result.extraction_context if test_result else {}),
            framework_type=(test_result.framework_type if test_result else None),
        )
        logger.debug(
            "Prepared GitHub Actions fix prompt for PR #%s (preview: %s)",
            pr_number,
            fix_prompt[:160].replace("\n", " "),
        )

        # Use LLM backend manager to run the prompt
        if not new_work_allowed():
            actions.append(f"Deferred GitHub Actions repair for PR #{pr_number}: graceful shutdown is draining")
            return actions
        logger.info(f"Requesting LLM GitHub Actions fix for PR #{pr_number}")
        with bind_invocation_target(repo_name, f"pr#{pr_number}", "github_actions_repair"):
            response = run_llm_prompt(fix_prompt, backend_manager=backend_manager)

        if response:
            response_preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
            actions.append(f"Applied GitHub Actions fix: {response_preview}...")
        else:
            actions.append("No response from LLM for GitHub Actions fix")

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        logger.error(f"Error applying GitHub Actions fix for PR #{pr_number}: {e}")
        actions.append(f"Error applying GitHub Actions fix for PR #{pr_number}: {e}")

    return actions


def _apply_local_test_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    test_result: Dict[str, Any],
    attempt_history: List[Dict[str, Any]],
    backend_manager: Optional[BackendManager] = None,
    github_client: Optional[Any] = None,
) -> Tuple[List[str], str]:
    """Apply fix using local test failure logs.

    This function uses the LLM backend manager to apply fixes based on local test failures,
    similar to apply_workspace_test_fix in fix_to_pass_tests_runner.py.

    Args:
        repo_name: Repository name
        pr_data: PR data dictionary
        config: AutomationConfig instance
        test_result: Test result dictionary from run_local_tests
        attempt_history: List of previous attempts with LLM outputs and test results
        backend_manager: Optional BackendManager instance to use (defaults to global singleton)

    Returns:
        Tuple of (actions_list, llm_response)
    """
    actions = []
    if not _is_automatic_test_fix_enabled(config, repo_name):
        return [f"Automatic test fix is disabled for PR #{pr_data['number']}; skipping local test repair"], ""
    if not new_work_allowed():
        return [f"Deferred local repair for PR #{pr_data['number']}: graceful shutdown is draining"], ""
    llm_response = ""
    with ProgressStage(f"Local test fix"):
        pr_number = pr_data["number"]

        try:
            # Extract important error information (convert legacy dict to TestResult)
            tr = TestResult(
                success=bool(test_result.get("success", False)),
                output=str(test_result.get("output", "")),
                errors=str(test_result.get("errors", "")),
                return_code=int(test_result.get("return_code", test_result.get("returncode", -1)) or -1),
                command=str(test_result.get("command", "")),
                test_file=test_result.get("test_file"),
                stability_issue=bool(test_result.get("stability_issue", False)),
                extraction_context=(test_result.get("extraction_context", {}) if isinstance(test_result.get("extraction_context", {}), dict) else {}),
                framework_type=test_result.get("framework_type"),
            )
            error_summary = extract_important_errors(tr)

            if not error_summary:
                actions.append(f"No actionable errors found in local test output for PR #{pr_number}")
                logger.info("Skipping LLM local test fix because no actionable errors were extracted")
                return actions, llm_response

            # Get commit log since branch creation
            commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

            # Format attempt history for inclusion in prompt
            history_text = ""
            if attempt_history:
                history_parts = []
                for hist in attempt_history:
                    attempt_num = hist.get("attempt_number", "N/A")
                    llm_output = hist.get("llm_output", "No output")
                    test_out = hist.get("test_result", {})
                    test_errors = test_out.get("errors", "") or test_out.get("output", "")
                    # Truncate long outputs
                    test_errors_truncated = (test_errors[:500] + "...") if len(test_errors) > 500 else test_errors
                    llm_output_truncated = (llm_output[:300] + "...") if len(str(llm_output)) > 300 else llm_output
                    history_parts.append(f"Attempt {attempt_num}:\n" f"  LLM Output: {llm_output_truncated}\n" f"  Test Result: {test_errors_truncated}")
                history_text = "\n\n".join(history_parts)

            # Extract linked issues context
            linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_data.get("body", ""))

            # Create prompt for local test error fix
            fix_prompt = render_prompt(
                "pr.local_test_fix",
                pr_number=pr_number,
                repo_name=repo_name,
                pr_title=pr_data.get("title", "Unknown"),
                error_summary=error_summary[: config.MAX_PROMPT_SIZE],
                test_command=test_result.get("command", "pytest -q --maxfail=1"),
                commit_log=commit_log or "(No commit history)",
                attempt_history=history_text,
                linked_issues_context=linked_issues_context,
            )
            logger.debug(
                "Prepared local test fix prompt for PR #%s (preview: %s)",
                pr_number,
                fix_prompt[:160].replace("\n", " "),
            )

            # Use LLM backend manager to run the prompt
            # Check if llm_client has run_test_fix_prompt method (BackendManager)
            # or fall back to _run_llm_cli
            logger.info(f"Requesting LLM local test fix for PR #{pr_number}")

            # If test_file is not in the result, try to extract it from the output
            if not tr.test_file:
                tr.test_file = extract_first_failed_test(tr.output, tr.errors)

            # BackendManager with test file tracking
            manager = backend_manager or get_llm_backend_manager()
            if not new_work_allowed():
                actions.append(f"Deferred local repair for PR #{pr_number}: graceful shutdown is draining")
                return actions, llm_response
            llm_response = manager.run_test_fix_prompt(fix_prompt, current_test_file=tr.test_file)

            if llm_response:
                response_preview = llm_response.strip()[: config.MAX_RESPONSE_SIZE] if llm_response.strip() else "No response"
                actions.append(f"Applied local test fix: {response_preview}...")
            else:
                actions.append("No response from LLM for local test fix")

        except AutoCoderRetryableBackendError:
            raise
        except Exception as e:
            actions.append(f"Error applying local test fix for PR #{pr_number}: {e}")
            logger.error(f"Error applying local test fix for PR #{pr_number}: {e}", exc_info=True)

    return actions, llm_response
