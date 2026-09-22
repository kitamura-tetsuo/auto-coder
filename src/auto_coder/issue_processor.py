"""
Issue processing functionality for Auto-Coder automation engine.
"""

import hashlib
import json
import os
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypedDict, Union, cast

from dateutil import parser

from auto_coder.util.gh_cache import get_ghapi_client, is_implementation_ready, resolve_authoritative_item_type
from auto_coder.util.github_action import _check_github_actions_status, check_and_handle_closed_state, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .attempt_manager import get_current_attempt, increment_attempt
from .automation_config import AutomationConfig, ProcessedIssueResult, ProcessResult, StaleJulesIssueResult
from .backend_manager import BackendManager, get_llm_backend_manager, parse_llm_output_as_json, run_llm_noedit_prompt
from .branch_manager import BranchManager
from .cloud_manager import CloudManager, CloudTaskBinding
from .exceptions import AutoCoderRetryableBackendError, AutoCoderUsageLimitError, CloudSubmissionNotStartedError
from .execution_trace import EventKind, Outcome, get_trace_collector
from .git_branch import branch_context, extract_attempt_from_branch
from .git_commit import commit_and_push_changes
from .git_info import get_commit_log, get_current_branch
from .implementation_ownership import confirm_implementation_ownership
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .invocation_admission import bind_invocation_target, take_pending_invocation_handle
from .issue_context import get_linked_issues_context, validate_issue_references
from .issue_dispatch import AdapterOutcome, DispatchResult
from .issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from .jules_client import JulesClient
from .jules_engine import get_session_pull_request, is_session_stopped, mark_session_stopped
from .label_manager import LabelManager, LabelManagerContext, LabelOperationError, filter_legacy_auto_coder_label, resolve_pr_labels_with_priority
from .logger_config import get_gh_logger, get_logger
from .progress_footer import ProgressStage, newline_progress, set_progress_item
from .prompt_loader import render_prompt
from .retry_dispatch import RetryDispatchRepository
from .shutdown_context import new_work_allowed
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()


@dataclass(frozen=True)
class IssueDispatchExecution:
    """Structured ordinary-dispatch result plus its presentation actions."""

    result: DispatchResult
    actions: List[str]


def _durable_retry_authority(
    repository: str,
    issue_number: int,
    supplied: ImplementationRetryRequest,
) -> ImplementationRetryRequest:
    """Reload and validate retry authority at the provider creation boundary."""
    if supplied.status != "owned" or not supplied.ownership_reference:
        raise ValueError("retry authority has not acquired real implementation ownership")
    routing_path = Path(os.environ.get("AUTO_CODER_ISSUE_STAGE_ROUTING_DB", "~/.auto-coder/issue-stage-routing.sqlite3")).expanduser()
    retained = IssueStageRoutingStore(routing_path).retry_request(supplied.request_id)
    if retained is None:
        raise ValueError("durable retry request is missing")
    expected = (
        repository,
        issue_number,
        supplied.generation,
        supplied.attempt_id,
        supplied.status,
        supplied.ownership_reference,
    )
    actual = (
        retained.repository,
        retained.target_number,
        retained.generation,
        retained.attempt_id,
        retained.status,
        retained.ownership_reference,
    )
    if actual != expected or retained.status != "owned" or not retained.ownership_reference:
        raise ValueError("durable retry request does not match owned dispatch authority")
    return retained


def _retry_predecessor(authority: ImplementationRetryRequest) -> Optional["CloudTaskBinding"]:
    from .cloud_manager import CloudTaskBinding

    if authority.predecessor_provider and authority.predecessor_task_id:
        return CloudTaskBinding(
            authority.predecessor_provider,
            authority.predecessor_task_id,
            authority.predecessor_backend_name or "",
        )
    return None


def _recognized_retry_predecessors(dispatch: RetryDispatchRepository, request_id: str) -> tuple[CloudTaskBinding, ...]:
    return tuple(CloudTaskBinding(provider, task_id, backend) for provider, task_id, backend in dispatch.accepted_predecessors(request_id))


def _acknowledge_retry_projection(
    manager: CloudManager,
    dispatch: RetryDispatchRepository,
    request_id: str,
    issue_number: int,
    binding: CloudTaskBinding,
) -> str:
    disposition = manager.confirm_retry_binding(
        issue_number,
        binding,
        lambda: dispatch.is_latest_accepted(request_id),
        lambda: dispatch.mark_tracking_complete(request_id),
    )
    if disposition == "historical":
        dispatch.mark_historical(request_id, "a later accepted retry owns the current pointer")
    return disposition


def generate_work_branch_name(issue_number: int, attempt: int) -> str:
    """
    Generate the work branch name based on the issue number and attempt.

    Args:
        issue_number: The issue number.
        attempt: The attempt number.

    Returns:
        The generated work branch name.

    Note:
        Uses underscore separator (_) instead of slash (/) to avoid Git ref namespace conflicts.
        Format: issue-<number>_attempt-<attempt>
        This is the new format introduced in v1.x.x to replace the legacy slash format (issue-<number>/attempt-<attempt>)
        Both formats are supported for backward compatibility.
    """
    if attempt > 0:
        return f"issue-{issue_number}_attempt-{attempt}"
    return f"issue-{issue_number}"


def _record_dispatch_stage(
    issue_number: int,
    stage_id: str,
    label: str,
    outcome: Outcome,
    facts: Optional[Dict[str, Any]] = None,
    kind: EventKind = EventKind.STAGE_RESULT,
) -> None:
    """Record a dispatch/implementation stage event using the ambient execution scope.

    The scope is bound by ``AutomationEngine`` before this module's dispatch
    functions run (REQ-002); when none is bound, ``TraceCollector`` retains
    the event as legacy/unscoped instead of inventing one. A diagnostic-
    recorder failure is caught here and never propagates into the dispatch
    decision it is describing (REQ-008).
    """
    try:
        merged_facts = {"issue_number": issue_number, **(facts or {})}
        get_trace_collector().record_event(
            kind,
            stage_id=stage_id,
            origin=stage_id,
            label=label,
            outcome=outcome,
            facts=merged_facts,
        )
    except Exception:
        logger.debug(f"Diagnostic trace recording failed for issue#{issue_number} stage {stage_id}; continuing", exc_info=True)


def _take_issue_actions(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_manager: Optional[BackendManager] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    retry_authority: Optional[ImplementationRetryRequest] = None,
    raise_on_failure: bool = False,
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation.

    Args:
        backend_manager: Backend manager used for the implementation run.
            Defaults to the current LLM backend manager.
    """
    actions = []
    issue_number = issue_data["number"]

    retry_dispatch: Optional[RetryDispatchRepository] = None
    if retry_authority is not None:
        if retry_authority.status != "owned" or not retry_authority.ownership_reference:
            return [f"Deferred local implementation for issue #{issue_number}: retry dispatch authority is unavailable: retry authority has not acquired real implementation ownership"]
        if backend_manager is None:
            try:
                backend_manager = get_llm_backend_manager()
            except Exception as exc:
                return [f"Deferred local implementation for issue #{issue_number}: selected backend is unavailable: {exc}"]
        retry_dispatch = RetryDispatchRepository(repo_name)
        current_backend = getattr(backend_manager, "_current_backend_name", None)
        backend_identity = str(getattr(backend_manager, "backend_name", None) or getattr(backend_manager, "name", None) or (current_backend() if callable(current_backend) else None) or "local")
        try:
            handoff, may_create = retry_dispatch.claim(
                retry_authority,
                "local",
                backend_identity,
                {"base_branch": config.MAIN_BRANCH},
            )
        except Exception as exc:
            return [f"Deferred local implementation for issue #{issue_number}: retry dispatch authority is unavailable: {exc}"]
        if not may_create:
            if handoff.outcome == "completed" and handoff.diagnostic:
                try:
                    checkpoint = json.loads(handoff.diagnostic)
                    retained_actions = checkpoint.get("actions") if isinstance(checkpoint, dict) else None
                    if isinstance(retained_actions, list) and all(isinstance(action, str) for action in retained_actions):
                        return retained_actions
                except (TypeError, ValueError):
                    pass
            return [f"Deferred local implementation for issue #{issue_number}: retry creation is {handoff.outcome}; no replacement invocation was started"]

    try:
        get_trace_logger().log("Issue Processing", f"Processing issue #{issue_number}", item_type="issue", item_number=issue_number)

        # Check if this is a parent issue (has sub-issues, no parent, all sub-issues closed)
        all_sub_issues = github_client.get_all_sub_issues(repo_name, issue_number)
        parent_issue_details = github_client.get_parent_issue_details(repo_name, issue_number)
        open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)

        is_parent_issue = len(all_sub_issues) > 0 and parent_issue_details is None and len(open_sub_issues) == 0  # Has sub-issues  # No parent  # All sub-issues closed

        if is_parent_issue:
            logger.info(f"Issue #{issue_number} detected as parent issue with all sub-issues closed")
            get_trace_logger().log("Issue Type", f"Issue #{issue_number} is a parent issue", item_type="issue", item_number=issue_number, details={"is_parent": True})
            if backend_manager is None:
                from .cli_helpers import create_high_score_backend_manager, create_high_score_cloud_backend_manager

                backend_manager = create_high_score_cloud_backend_manager() or create_high_score_backend_manager()

        # Ask LLM CLI to analyze the issue and take appropriate actions
        if implementation_slots is None:
            if raise_on_failure:
                action_results = _apply_issue_actions_directly(repo_name, issue_data, config, github_client, backend_manager=backend_manager, raise_on_failure=True)
            else:
                action_results = _apply_issue_actions_directly(repo_name, issue_data, config, github_client, backend_manager=backend_manager)
        else:
            if raise_on_failure:
                action_results = _apply_issue_actions_directly(repo_name, issue_data, config, github_client, backend_manager=backend_manager, implementation_slots=implementation_slots, raise_on_failure=True)
            else:
                action_results = _apply_issue_actions_directly(repo_name, issue_data, config, github_client, backend_manager=backend_manager, implementation_slots=implementation_slots)
        actions.extend(action_results)
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(
                retry_authority.request_id,
                "completed",
                external_id=retry_authority.ownership_reference,
                diagnostic=json.dumps({"actions": actions}, sort_keys=True),
                tracking_complete=True,
            )

    except AutoCoderRetryableBackendError as exc:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "indeterminate", diagnostic=str(exc))
        raise
    except Exception as e:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "indeterminate", diagnostic=str(e))
        logger.error(f"Error taking actions on issue #{issue_number}: {e}")
        if raise_on_failure:
            raise
        actions.append(f"Error processing issue #{issue_number}: {e}")

    return actions


def _process_issue_jules_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    backend_name: str = "jules",
    retry_authority: Optional[ImplementationRetryRequest] = None,
    acceptance_observer: Optional[Callable[["AdapterOutcome"], None]] = None,
) -> List[str]:
    """Process an issue using Jules API for session-based AI interaction.

    This function:
    1. Starts a Jules session for the issue
    2. Saves the session ID to cloud.csv
    3. Comments on the issue with the session ID
    4. Uses Jules to process the issue
    5. Creates a PR if changes are made

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        config: AutomationConfig instance
        github_client: GitHub client for API operations
        label_context: Optional LabelManagerContext to keep label on success

    Returns:
        List of action strings describing what was done
    """
    actions = []
    issue_number = issue_data["number"]
    issue_title = issue_data.get("title", "Unknown")
    issue_body = issue_data.get("body", "")
    retry_dispatch: Optional[RetryDispatchRepository] = None

    try:
        configured_width = getattr(config, "JULES_SPECULATIVE_PARALLELISM", 1)
        if isinstance(configured_width, bool) or not isinstance(configured_width, int) or configured_width < 1:
            raise ValueError("[jules].speculative_parallelism must be a positive integer (booleans are not valid)")
        width = configured_width

        # Initialize Jules client
        jules_client = JulesClient(backend_name=backend_name)

        # Prepare the prompt for Jules
        # Extract issue labels, excluding the retired "@auto-coder" legacy
        # label so it never reaches the LLM prompt (FTR-1792).
        issue_labels_list = filter_legacy_auto_coder_label(issue_data.get("labels", []))

        action_prompt = render_prompt(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_labels=", ".join(issue_labels_list),
            issue_state=issue_data.get("state", "open"),
            issue_author=issue_data.get("user", {}).get("login", "unknown"),
            commit_log=get_commit_log(base_branch=config.MAIN_BRANCH) or "(No commit history)",
            is_jules=True,
        )

        if not new_work_allowed():
            _record_dispatch_stage(issue_number, "issue.dispatch.jules", f"issue#{issue_number} Jules dispatch", Outcome.DEFERRED, {"backend": "jules", "reason": "graceful shutdown is draining"})
            return [f"Deferred Jules session for issue #{issue_number}: graceful shutdown is draining"]

        if retry_authority is not None:
            retry_dispatch = RetryDispatchRepository(repo_name)
            retry_authority = _durable_retry_authority(repo_name, issue_number, retry_authority)
            predecessor_binding = _retry_predecessor(retry_authority)
            handoff, may_create = retry_dispatch.claim(
                retry_authority,
                "jules",
                backend_name,
                {"base_branch": config.MAIN_BRANCH},
                predecessor=(predecessor_binding.provider, predecessor_binding.task_id, predecessor_binding.backend_name) if predecessor_binding is not None else None,
            )
            if not may_create:
                if handoff.outcome in {"accepted", "completed"} and handoff.external_id:
                    disposition = CloudManager(repo_name).promote_retry_binding(
                        issue_number,
                        CloudTaskBinding("jules", handoff.external_id, handoff.backend_name),
                        predecessor_binding,
                        lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                        _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                        retry_authority.predecessor_captured,
                    )
                    if disposition == "historical":
                        retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                        return [f"Retained historical Jules session '{handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                    retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
                    if (
                        _acknowledge_retry_projection(
                            CloudManager(repo_name),
                            retry_dispatch,
                            retry_authority.request_id,
                            issue_number,
                            CloudTaskBinding("jules", handoff.external_id, handoff.backend_name),
                        )
                        == "historical"
                    ):
                        return [f"Retained historical Jules session '{handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                    return [f"Jules session '{handoff.external_id}' already accepted for retry {retry_authority.attempt_id}; skipped duplicate dispatch"]
                return [f"Deferred Jules session for issue #{issue_number}: retry creation is {handoff.outcome}; no replacement work was started"]

        if width > 1 and retry_authority is None:
            return _dispatch_jules_competition(
                repo_name,
                issue_data,
                config,
                action_prompt,
                jules_client,
                label_context,
                implementation_slots,
            )

        logger.info(f"Starting Jules session for issue #{issue_number}")

        # Determine base branch (default to main)
        base_branch = config.MAIN_BRANCH

        # Start Jules session
        session_title = f"{issue_title} (#{issue_number})"
        try:
            session_id = jules_client.start_session(action_prompt, repo_name, base_branch, title=session_title)
        except Exception as exc:
            if retry_dispatch is not None and retry_authority is not None:
                retry_dispatch.record_outcome(retry_authority.request_id, "indeterminate", diagnostic=str(exc))
            _record_dispatch_stage(issue_number, "issue.dispatch.jules", f"issue#{issue_number} Jules dispatch", Outcome.FAILED, {"backend": "jules"})
            raise
        _record_dispatch_stage(issue_number, "issue.dispatch.jules", f"issue#{issue_number} Jules dispatch", Outcome.ACCEPTED_HANDOFF, {"backend": "jules", "session_id": session_id})
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "accepted", external_id=session_id)

        # Store session ID in cloud.csv
        cloud_manager = CloudManager(repo_name)
        if retry_dispatch is not None and retry_authority is not None:
            disposition = cloud_manager.promote_retry_binding(
                issue_number,
                CloudTaskBinding("jules", session_id, backend_name),
                _retry_predecessor(retry_authority),
                lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                retry_authority.predecessor_captured,
            )
            if disposition == "historical":
                retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                return [f"Retained historical Jules session '{session_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
            success = True
        else:
            success = cloud_manager.add_session(issue_number, session_id, provider="jules", backend_name=backend_name)

        if acceptance_observer is not None:
            from .issue_dispatch import AdapterOutcome, DispatchOutcome

            acceptance_observer(
                AdapterOutcome(
                    DispatchOutcome.REMOTE_ACCEPTED,
                    session_id,
                    "" if success else "CloudManager binding persistence failed",
                    tracking_complete=success,
                )
            )

        if not success:
            if retry_dispatch is not None and retry_authority is not None:
                return [f"Accepted Jules session '{session_id}' for issue #{issue_number}, but tracking is incomplete"]
            logger.warning(f"Failed to save session ID to cloud.csv for issue #{issue_number}")
            actions.append(f"Warning: Could not save session ID for issue #{issue_number}")
        else:
            logger.info(f"Saved session ID '{session_id}' for issue #{issue_number}")
            if retry_dispatch is not None and retry_authority is not None:
                retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
                if (
                    _acknowledge_retry_projection(
                        cloud_manager,
                        retry_dispatch,
                        retry_authority.request_id,
                        issue_number,
                        CloudTaskBinding("jules", session_id, backend_name),
                    )
                    == "historical"
                ):
                    return [f"Retained historical Jules session '{session_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]

        # Comment on the issue with session ID
        try:
            comment_body = f"I started a Jules session to work on this issue. Session ID: {session_id}\n\nhttps://jules.google.com/session/{session_id}"
            github_client.add_comment_to_issue(repo_name, issue_number, comment_body)
            actions.append(f"Commented on issue #{issue_number} with Jules session ID")
            logger.info(f"Added comment with session ID to issue #{issue_number}")

        except Exception as e:
            logger.warning(f"Failed to add comment to issue #{issue_number}: {e}")
            actions.append(f"Warning: Could not comment on issue #{issue_number}")

        # For Jules mode, we don't immediately process the issue here
        # Instead, Jules will create a PR that will be detected and processed by _process_jules_pr
        # This is the feedback loop - Jules processes the issue and creates a PR
        actions.append(f"Started Jules session '{session_id}' for issue #{issue_number}")
        logger.info(f"Jules session started successfully for issue #{issue_number}")

        get_trace_logger().log("Jules Session", f"Started Jules session for issue #{issue_number}", item_type="issue", item_number=issue_number, details={"session_id": session_id})

        # Keep the @auto-coder label if context was provided
        if label_context:
            label_context.keep_label()
            logger.info(f"Keeping @auto-coder label for issue #{issue_number} (Jules session started)")

    except Exception as e:
        logger.error(f"Error processing issue #{issue_number} in Jules mode: {e}")
        actions.append(f"Error processing issue #{issue_number} in Jules mode: {e}")

    return actions


def _dispatch_jules_competition(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    task_payload: str,
    jules_client: JulesClient,
    label_context: Optional[LabelManagerContext],
    implementation_slots: Optional[ImplementationSlotRepository],
) -> List[str]:
    """Create or resume one fixed-width Jules competition before any POST.

    This boundary is reached only after the ordinary controller admission gates.
    Persisting the complete candidate set first makes repeated explicit runs
    resumptions rather than additional logical attempts.
    """
    from .jules_candidate_submission import CandidateRequest, JulesCandidateSubmissionAdapter
    from .jules_competition_ledger import CapturedPolicySettings, JulesCompetitionLedger, SpeculativeGenerationBundle

    issue_number = int(issue_data["number"])
    if not config.pr_adversarial_validation:
        return [f"Deferred Jules competition for issue #{issue_number}: independent PR adversarial validation is disabled"]
    from .cli_helpers import create_adversarial_validation_backend_manager

    if create_adversarial_validation_backend_manager(validation_kind="pr") is None:
        return [f"Deferred Jules competition for issue #{issue_number}: no independent PR adversarial validation backend is configured"]
    oracle = str(issue_data.get("body") or "")
    fingerprint = hashlib.sha256(oracle.encode("utf-8")).hexdigest()
    root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder")) / "state"
    ledger = JulesCompetitionLedger()
    snapshot = ledger.get_namespace_snapshot(repo_name, issue_number)
    generation = snapshot.get_active_generation()
    if generation is None:
        candidate_ids = tuple(f"candidate-{index + 1}" for index in range(config.JULES_SPECULATIVE_PARALLELISM))
        bundle = SpeculativeGenerationBundle(
            source_attempt_number=get_current_attempt(repo_name, issue_number),
            candidate_ids=candidate_ids,
            issue_oracle_snapshot=oracle,
            issue_oracle_fingerprint=fingerprint,
            source_branch=config.MAIN_BRANCH,
            policy_settings=CapturedPolicySettings(
                timeout_seconds=config.JULES_ISSUE_PR_TIMEOUT_HOURS * 3600,
                provider_name="jules",
                extra_settings=(("pr_ci_timeout_hours", str(config.JULES_PR_CI_TIMEOUT_HOURS)),),
            ),
        )
        admitted = ledger.create_generation(
            repo_name,
            issue_number,
            f"production-admit:{repo_name}:{issue_number}:{fingerprint}",
            snapshot.epoch,
            bundle,
        )
        generation = admitted.snapshot.get_generation(admitted.generation_id or "") if admitted.admitted else admitted.snapshot.get_active_generation()
    if generation is None:
        return [f"Deferred Jules competition for issue #{issue_number}: generation admission unavailable"]

    adapter = JulesCandidateSubmissionAdapter(ledger, jules_client, root / "jules_candidate_submissions.db")
    results = []
    for candidate_id in generation.candidate_ids:
        current_generation = ledger.get_namespace_snapshot(repo_name, issue_number).get_generation(generation.generation_id)
        if not new_work_allowed() or current_generation is None or not current_generation.is_active() or current_generation.has_winner():
            break
        results.append(adapter.submit(CandidateRequest(repo_name, issue_number, generation.generation_id, candidate_id, f"{repo_name}#{issue_number}", task_payload)))

    accepted_sessions = tuple(result.session_id for result in results if result.session_id)
    if implementation_slots is not None:
        owner = ImplementationOwner("issue", issue_number)
        for session_id in accepted_sessions:
            if not implementation_slots.record_provider_session(owner, session_id):
                raise RuntimeError(f"Could not retain Jules candidate ownership for issue #{issue_number}")
    if label_context:
        label_context.keep_label()
    unknown = sum(result.outcome.value == "UNKNOWN" for result in results)
    exhausted = sum(result.outcome.value == "DEFINITELY_NOT_ACCEPTED" for result in results)
    get_trace_logger().log(
        "Jules Competition",
        f"Dispatched Jules competition for issue #{issue_number}",
        item_type="issue",
        item_number=issue_number,
        details={"generation_id": generation.generation_id, "requested": len(generation.candidate_ids), "accepted": len(accepted_sessions), "unknown": unknown, "exhausted": exhausted},
    )
    return [f"Jules competition {generation.generation_id} for issue #{issue_number}: " f"requested={len(generation.candidate_ids)}, accepted={len(accepted_sessions)}, unknown={unknown}, exhausted={exhausted}"]


def _process_issue_claude_routine_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_name: Optional[str] = None,
    label_context: Optional[LabelManagerContext] = None,
    manual_retry: bool = False,
    retry_authority: Optional[ImplementationRetryRequest] = None,
    acceptance_observer: Optional[Callable[["AdapterOutcome"], None]] = None,
) -> List[str]:
    """Process an issue using Claude Routine for cloud-based AI routine execution.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        config: AutomationConfig instance
        github_client: GitHub client for API operations
        backend_name: Name of the claude-routine backend configuration
        label_context: Optional LabelManagerContext to keep label on success

    Returns:
        List of action strings describing what was done
    """
    actions = []
    issue_number = issue_data["number"]
    issue_title = issue_data.get("title", "Unknown")
    issue_body = issue_data.get("body", "")
    retry_dispatch: Optional[RetryDispatchRepository] = None
    if manual_retry and retry_authority is None:
        return [f"Deferred Claude Routine session for issue #{issue_number}: durable retry authority is required"]

    try:
        from .claude_routine_client import ClaudeRoutineClient

        routine_client = ClaudeRoutineClient(backend_name=backend_name, repo_name=repo_name)

        # Extract issue labels, excluding the retired "@auto-coder" legacy
        # label so it never reaches the LLM prompt (FTR-1792).
        issue_labels_list = filter_legacy_auto_coder_label(issue_data.get("labels", []))

        action_prompt = render_prompt(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_labels=", ".join(issue_labels_list),
            issue_state=issue_data.get("state", "open"),
            issue_author=issue_data.get("user", {}).get("login", "unknown"),
            commit_log=get_commit_log(base_branch=config.MAIN_BRANCH) or "(No commit history)",
            is_jules=True,
        )

        if not new_work_allowed():
            _record_dispatch_stage(issue_number, "issue.dispatch.claude-routine", f"issue#{issue_number} Claude Routine dispatch", Outcome.DEFERRED, {"backend": "claude-routine", "reason": "graceful shutdown is draining"})
            return [f"Deferred Claude Routine session for issue #{issue_number}: graceful shutdown is draining"]

        effective_backend = backend_name or "claude-routine"
        if retry_authority is not None:
            retry_dispatch = RetryDispatchRepository(repo_name)
            retry_authority = _durable_retry_authority(repo_name, issue_number, retry_authority)
            predecessor_binding = _retry_predecessor(retry_authority)
            handoff, may_create = retry_dispatch.claim(
                retry_authority,
                "claude-routine",
                effective_backend,
                {"base_branch": config.MAIN_BRANCH},
                predecessor=(predecessor_binding.provider, predecessor_binding.task_id, predecessor_binding.backend_name) if predecessor_binding is not None else None,
            )
            if not may_create:
                if handoff.outcome in {"accepted", "completed"} and handoff.external_id:
                    disposition = CloudManager(repo_name).promote_retry_binding(
                        issue_number,
                        CloudTaskBinding("claude-routine", handoff.external_id, handoff.backend_name),
                        predecessor_binding,
                        lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                        _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                        retry_authority.predecessor_captured,
                    )
                    if disposition == "historical":
                        retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                        return [f"Retained historical Claude Routine session '{handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                    retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
                    if (
                        _acknowledge_retry_projection(
                            CloudManager(repo_name),
                            retry_dispatch,
                            retry_authority.request_id,
                            issue_number,
                            CloudTaskBinding("claude-routine", handoff.external_id, handoff.backend_name),
                        )
                        == "historical"
                    ):
                        return [f"Retained historical Claude Routine session '{handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                    return [f"Claude Routine session '{handoff.external_id}' already accepted for retry {retry_authority.attempt_id}; skipped duplicate dispatch"]
                return [f"Deferred Claude Routine session for issue #{issue_number}: retry creation is {handoff.outcome}; no replacement work was started"]

        logger.info(f"Starting Claude Routine session for issue #{issue_number}")

        base_branch = config.MAIN_BRANCH

        session_title = f"{issue_title} (#{issue_number})"
        try:
            session_id, session_url = routine_client.fire_routine(action_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)
        except AutoCoderUsageLimitError:
            if retry_dispatch is not None and retry_authority is not None:
                retry_dispatch.record_outcome(retry_authority.request_id, "definitely-not-started", diagnostic="usage limit before routine submission")
            _record_dispatch_stage(issue_number, "issue.dispatch.claude-routine", f"issue#{issue_number} Claude Routine dispatch", Outcome.DEFERRED, {"backend": "claude-routine", "reason": "usage limit"})
            raise
        except Exception as exc:
            if retry_dispatch is not None and retry_authority is not None:
                retry_dispatch.record_outcome(retry_authority.request_id, "indeterminate", diagnostic=str(exc))
            _record_dispatch_stage(issue_number, "issue.dispatch.claude-routine", f"issue#{issue_number} Claude Routine dispatch", Outcome.FAILED, {"backend": "claude-routine"})
            raise
        _record_dispatch_stage(issue_number, "issue.dispatch.claude-routine", f"issue#{issue_number} Claude Routine dispatch", Outcome.ACCEPTED_HANDOFF, {"backend": "claude-routine", "session_id": session_id})
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(
                retry_authority.request_id,
                "accepted",
                external_id=session_id,
                external_url=session_url,
            )

        cloud_manager = CloudManager(repo_name)
        if retry_dispatch is not None and retry_authority is not None:
            disposition = cloud_manager.promote_retry_binding(
                issue_number,
                CloudTaskBinding("claude-routine", session_id, effective_backend),
                _retry_predecessor(retry_authority),
                lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                retry_authority.predecessor_captured,
            )
            if disposition == "historical":
                retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                return [f"Retained historical Claude Routine session '{session_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
            success = True
        else:
            success = cloud_manager.add_session(
                issue_number,
                session_id,
                provider="claude-routine",
                backend_name=effective_backend,
            )

        if acceptance_observer is not None:
            from .issue_dispatch import AdapterOutcome, DispatchOutcome

            acceptance_observer(
                AdapterOutcome(
                    DispatchOutcome.REMOTE_ACCEPTED,
                    session_id,
                    "" if success else "CloudManager binding persistence failed",
                    tracking_complete=success,
                )
            )

        if not success and manual_retry:
            raise RuntimeError(f"New Claude Routine session {session_id} was accepted, but tracking could not be updated")
        if not success and retry_authority is not None:
            return [f"Accepted Claude Routine session '{session_id}' for issue #{issue_number}, but tracking is incomplete"]
        if not success:
            logger.warning(f"Failed to save session ID to cloud.csv for issue #{issue_number}")
            actions.append(f"Warning: Could not save session ID for issue #{issue_number}")
        else:
            logger.info(f"Saved session ID '{session_id}' for issue #{issue_number}")
            if retry_dispatch is not None and retry_authority is not None:
                retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
                if (
                    _acknowledge_retry_projection(
                        cloud_manager,
                        retry_dispatch,
                        retry_authority.request_id,
                        issue_number,
                        CloudTaskBinding("claude-routine", session_id, effective_backend),
                    )
                    == "historical"
                ):
                    return [f"Retained historical Claude Routine session '{session_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]

        try:
            comment_body = f"I started a Claude Routine session to work on this issue. Session ID: {session_id}"
            if session_url:
                comment_body += f"\n\n{session_url}"
            github_client.add_comment_to_issue(repo_name, issue_number, comment_body)
            actions.append(f"Commented on issue #{issue_number} with Claude Routine session ID")
            logger.info(f"Added comment with session ID to issue #{issue_number}")

        except Exception as e:
            logger.warning(f"Failed to add comment to issue #{issue_number}: {e}")
            actions.append(f"Warning: Could not comment on issue #{issue_number}")

        actions.append(f"Started Claude Routine session '{session_id}' for issue #{issue_number}")
        logger.info(f"Claude Routine session started successfully for issue #{issue_number}")

        get_trace_logger().log(
            "Claude Routine Session",
            f"Started Claude Routine session for issue #{issue_number}",
            item_type="issue",
            item_number=issue_number,
            details={"session_id": session_id, "session_url": session_url},
        )

        if label_context:
            label_context.keep_label()
            logger.info(f"Keeping @auto-coder label for issue #{issue_number} (Claude Routine session started)")

    except AutoCoderUsageLimitError:
        raise
    except Exception as e:
        if manual_retry or retry_authority is not None:
            raise
        logger.error(f"Error processing issue #{issue_number} in Claude Routine mode: {e}")
        actions.append(f"Error processing issue #{issue_number} in Claude Routine mode: {e}")

    return actions


def _process_issue_codex_cloud_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_name: str,
    label_context: Optional[LabelManagerContext] = None,
    manual_retry: bool = False,
    retry_authority: Optional[ImplementationRetryRequest] = None,
) -> List[str]:
    """Submit an issue to Codex Cloud and persist its task identifier.

    Dispatch is guarded by a durable `CloudRun` record keyed by
    (issue_number, attempt): if a Codex Cloud run already exists for the
    issue's current attempt, `CodexCloudClient.start_task()` is not called
    again. This protection is independent of the `@auto-coder` label, so it
    remains effective across a process restart or when the label is absent,
    stale, or temporarily inconsistent (see issue #1606).
    """
    from .cloud_manager import CloudTaskBinding
    from .cloud_run import CloudRun, CloudRunRepository
    from .codex_cloud_client import CodexCloudClient, CodexSubmissionOutcome

    issue_number = issue_data["number"]
    issue_title = issue_data.get("title", "Unknown")
    if manual_retry and retry_authority is None:
        return [f"Deferred Codex Cloud task for issue #{issue_number}: durable retry authority is required"]

    attempt = get_current_attempt(repo_name, issue_number)
    cloud_run_repo = CloudRunRepository(repo_name)
    cloud_manager = CloudManager(repo_name)
    retry_dispatch: Optional[RetryDispatchRepository] = None
    retry_handoff = None
    if retry_authority is not None:
        retry_dispatch = RetryDispatchRepository(repo_name)
        try:
            retry_authority = _durable_retry_authority(repo_name, issue_number, retry_authority)
            attributed_predecessor = _retry_predecessor(retry_authority)
            predecessor = (attributed_predecessor.provider, attributed_predecessor.task_id, attributed_predecessor.backend_name) if attributed_predecessor is not None else None
            retry_handoff, may_create = retry_dispatch.claim(
                retry_authority,
                "codex-cloud",
                backend_name,
                {"base_branch": config.MAIN_BRANCH},
                predecessor=predecessor,
            )
            retry_handoff = retry_dispatch.allocate_numeric_attempt(
                retry_authority.request_id,
                [attempt] + [run.attempt for run in cloud_run_repo.list_for_issue(issue_number)],
            )
            allocated_attempt = retry_handoff.numeric_attempt
            assert allocated_attempt is not None
            attempt = allocated_attempt
            if may_create:
                retry_handoff = retry_dispatch.bind_route_config(
                    retry_authority.request_id,
                    {
                        "base_branch": config.MAIN_BRANCH,
                        "publication_head_repository": repo_name,
                        "publication_head_ref": f"issue-{issue_number}-attempt-{attempt}-codex-cloud",
                    },
                )
        except Exception as exc:
            return [f"Deferred Codex Cloud task for issue #{issue_number}: retry dispatch authority is unavailable: {exc}"]

        if not may_create:
            if retry_handoff.outcome == "claimed":
                retained_run = cloud_run_repo.get(issue_number, attempt)
                if retained_run is not None and retained_run.provider == "codex-cloud" and retained_run.backend_name == retry_handoff.backend_name and retained_run.task_id and retained_run.submission_outcome == "accepted":
                    retry_handoff = retry_dispatch.record_outcome(
                        retry_authority.request_id,
                        "accepted",
                        external_id=retained_run.task_id,
                        external_url=retained_run.task_url or None,
                    )
            if retry_handoff.outcome in {"accepted", "completed"} and retry_handoff.external_id:
                retained_config = json.loads(retry_handoff.route_config)
                recovered = CloudRun(
                    repo_name=repo_name,
                    issue_number=issue_number,
                    attempt=attempt,
                    provider="codex-cloud",
                    task_id=retry_handoff.external_id,
                    backend_name=retry_handoff.backend_name,
                    environment_id=retry_handoff.environment_id or "",
                    base_branch=str(retained_config.get("base_branch", "")),
                    submission_outcome="accepted",
                    task_url=retry_handoff.external_url or "",
                    launch_identity=retry_authority.request_id,
                    publication_head_repository=str(retained_config.get("publication_head_repository", "")),
                    publication_head_ref=str(retained_config.get("publication_head_ref", "")),
                )
                try:
                    cloud_run_repo.repair_accepted(recovered)
                    binding = CloudTaskBinding("codex-cloud", retry_handoff.external_id, retry_handoff.backend_name)
                    predecessor_binding = _retry_predecessor(retry_authority)
                    disposition = cloud_manager.promote_retry_binding(
                        issue_number,
                        binding,
                        predecessor_binding,
                        lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                        _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                        retry_authority.predecessor_captured,
                    )
                    if disposition == "historical":
                        retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                        return [f"Retained historical Codex Cloud task '{retry_handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                    retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
                    if (
                        _acknowledge_retry_projection(
                            cloud_manager,
                            retry_dispatch,
                            retry_authority.request_id,
                            issue_number,
                            binding,
                        )
                        == "historical"
                    ):
                        return [f"Retained historical Codex Cloud task '{retry_handoff.external_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
                except Exception as exc:
                    retry_dispatch.mark_tracking_incomplete(retry_authority.request_id, str(exc))
                    return [f"Accepted Codex Cloud task '{retry_handoff.external_id}' for issue #{issue_number}, but tracking is incomplete: {exc}"]
                return [f"Codex Cloud task '{retry_handoff.external_id}' already accepted for retry {retry_authority.attempt_id}; skipped duplicate dispatch"]
            return [f"Deferred Codex Cloud task for issue #{issue_number}: retry creation is {retry_handoff.outcome}; no replacement work was started"]

        try:
            recorded_attempt = increment_attempt(repo_name, issue_number, attempt_number=attempt)
        except Exception as exc:
            return [f"Deferred Codex Cloud task for issue #{issue_number}: retry attempt projection is indeterminate: {exc}"]
        if recorded_attempt != attempt:
            return [f"Deferred Codex Cloud task for issue #{issue_number}: retry attempt projection did not retain allocated attempt {attempt}"]
    elif manual_retry:
        # Preserve all earlier runs; a human request authorizes a new attempt.
        runs = cloud_run_repo.list_for_issue(issue_number)
        previous_attempt = max([attempt] + [run.attempt for run in runs])
        attempt = increment_attempt(repo_name, issue_number, attempt_number=previous_attempt + 1)
        if attempt <= previous_attempt:
            raise RuntimeError("Could not record the manual retry attempt")
    try:
        existing_run = cloud_run_repo.get(issue_number, attempt)
        candidate_binding = cloud_manager.read_bindings_strict().get(str(issue_number))
        csv_binding = candidate_binding if isinstance(candidate_binding, CloudTaskBinding) else None
    except Exception as exc:
        return [f"Deferred Codex Cloud task for issue #{issue_number}: required ownership state is unreadable: {exc}"]

    if existing_run is not None:
        if existing_run.provider != "codex-cloud":
            return [f"Deferred Codex Cloud task for issue #{issue_number}: contradictory provider ownership"]
        if not existing_run.task_id:
            return [f"Deferred Codex Cloud task for issue #{issue_number}: submission is {existing_run.submission_outcome}; provider task identity is unresolved and requires operator attention"]
        expected = CloudTaskBinding("codex-cloud", existing_run.task_id, existing_run.backend_name)
        try:
            if csv_binding is not None and csv_binding != expected:
                raise ValueError("cloud.csv names a different task, provider, or backend")
            if not cloud_manager.ensure_binding(issue_number, expected):
                raise OSError("cloud.csv write failed")
        except Exception as exc:
            return [f"Accepted Codex Cloud task '{existing_run.task_id}' for issue #{issue_number}, but tracking is incomplete: {exc}"]
        if label_context:
            label_context.keep_label()
        _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.SKIPPED, {"backend": "codex-cloud", "task_id": existing_run.task_id, "reason": "duplicate dispatch"})
        return [f"Codex Cloud task '{existing_run.task_id}' already running for issue #{issue_number} attempt {attempt}; skipped duplicate dispatch"]
    if csv_binding is not None and not manual_retry and retry_authority is None:
        return [f"Deferred Codex Cloud task for issue #{issue_number}: legacy cloud.csv ownership has no authoritative Issue attempt; operator attention required"]

    # Extract issue labels, excluding the retired "@auto-coder" legacy label
    # so it never reaches the LLM prompt (FTR-1792).
    issue_labels = filter_legacy_auto_coder_label(issue_data.get("labels", []))
    launch_identity = retry_authority.request_id if retry_authority is not None else f"{repo_name}#{issue_number}:attempt:{attempt}"
    publication_head_ref = f"issue-{issue_number}-attempt-{attempt}-codex-cloud"
    prompt = render_prompt(
        "codex_cloud.initial_issue_implementation",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_url=f"https://github.com/{repo_name}/issues/{issue_number}",
        issue_title=issue_title,
        issue_body=issue_data.get("body", ""),
        issue_labels=", ".join(issue_labels),
        issue_state=issue_data.get("state", "open"),
        issue_author=issue_data.get("user", {}).get("login", "unknown"),
        issue_attempt=attempt,
        backend_name=backend_name,
        base_branch=config.MAIN_BRANCH,
        publication_head_repository=repo_name,
        publication_head_ref=publication_head_ref,
        commit_log=get_commit_log(base_branch=config.MAIN_BRANCH) or "(No commit history)",
        parent_issue_number=issue_data.get("parent_issue_number"),
        parent_issue_title=issue_data.get("parent_issue_title", ""),
        parent_issue_body=issue_data.get("parent_issue_body", ""),
        linked_issues_context=issue_data.get("linked_issues_context", ""),
    )

    if not new_work_allowed():
        return [f"Deferred Codex Cloud task for issue #{issue_number}: graceful shutdown is draining"]

    client = CodexCloudClient(backend_name=backend_name, repo_name=repo_name)
    claim = CloudRun(
        repo_name=repo_name,
        issue_number=issue_number,
        attempt=attempt,
        provider="codex-cloud",
        backend_name=backend_name,
        environment_id=client.environment_id if isinstance(client.environment_id, str) else "",
        base_branch=config.MAIN_BRANCH,
        submission_outcome="indeterminate",
        launch_identity=launch_identity,
        publication_head_repository=repo_name,
        publication_head_ref=publication_head_ref,
    )
    try:
        claim, acquired = cloud_run_repo.acquire_submission_claim(claim)
    except Exception as exc:
        return [f"Deferred Codex Cloud task for issue #{issue_number}: could not persist submission claim: {exc}"]
    if not acquired:
        return [f"Deferred Codex Cloud task for issue #{issue_number}: a suppressing submission claim already exists"]

    try:
        submission = client.submit_task(prompt, repo_name=repo_name, base_branch=config.MAIN_BRANCH, title=f"{issue_title} (#{issue_number})")
    except AutoCoderUsageLimitError:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "definitely-not-started", diagnostic="usage limit before submission")
        claim.submission_outcome = "definitely-not-submitted"
        cloud_run_repo.update_claim(claim)
        cloud_run_repo.release_definitely_not_submitted(issue_number, attempt)
        _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.DEFERRED, {"backend": "codex-cloud", "reason": "usage limit"})
        raise
    claim.submission_outcome = submission.outcome.value
    claim.task_id = submission.task_id
    claim.task_url = submission.task_url
    try:
        cloud_run_repo.update_claim(claim)
    except Exception as exc:
        if submission.outcome is CodexSubmissionOutcome.ACCEPTED and retry_dispatch is not None and retry_authority is not None:
            try:
                retry_dispatch.record_outcome(
                    retry_authority.request_id,
                    "accepted",
                    external_id=submission.task_id,
                    external_url=submission.task_url,
                    environment_id=claim.environment_id,
                    diagnostic=f"CloudRun projection failed: {exc}",
                )
            except Exception as receipt_exc:
                return [f"Accepted Codex Cloud task '{submission.task_id}' for issue #{issue_number}, but its run and retry receipt could not be persisted: {receipt_exc}"]
            return [f"Accepted Codex Cloud task '{submission.task_id}' for issue #{issue_number}, but tracking is incomplete: {exc}"]
        return [f"Deferred Codex Cloud task for issue #{issue_number}: submission outcome could not be persisted and is indeterminate: {exc}"]
    if submission.outcome is CodexSubmissionOutcome.DEFINITELY_NOT_SUBMITTED:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "definitely-not-started", diagnostic=submission.diagnostic)
        cloud_run_repo.release_definitely_not_submitted(issue_number, attempt)
        _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.FAILED, {"backend": "codex-cloud", "reason": "definitely not submitted"})
        raise CloudSubmissionNotStartedError(f"Codex Cloud task for issue #{issue_number} definitely not submitted: {submission.diagnostic}")
    if submission.outcome is CodexSubmissionOutcome.INDETERMINATE:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.record_outcome(retry_authority.request_id, "indeterminate", diagnostic=submission.diagnostic)
        _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.UNKNOWN, {"backend": "codex-cloud", "reason": "indeterminate submission"})
        return [f"Deferred Codex Cloud task for issue #{issue_number}: submission is indeterminate and requires operator attention: {submission.diagnostic}"]

    task_id = submission.task_id
    if retry_dispatch is not None and retry_authority is not None:
        try:
            retry_dispatch.record_outcome(
                retry_authority.request_id,
                "accepted",
                external_id=task_id,
                external_url=submission.task_url,
                environment_id=claim.environment_id,
            )
        except Exception as exc:
            return [f"Accepted Codex Cloud task '{task_id}' for issue #{issue_number}, but its retry receipt could not be persisted and is indeterminate: {exc}"]
    try:
        binding = CloudTaskBinding("codex-cloud", task_id, backend_name)
        if retry_dispatch is not None and retry_authority is not None and retry_handoff is not None:
            predecessor_binding = (
                CloudTaskBinding(
                    retry_handoff.predecessor_provider,
                    retry_handoff.predecessor_task_id,
                    retry_handoff.predecessor_backend_name or "",
                )
                if retry_handoff.predecessor_provider and retry_handoff.predecessor_task_id
                else None
            )
            disposition = cloud_manager.promote_retry_binding(
                issue_number,
                binding,
                predecessor_binding,
                lambda: retry_dispatch.is_latest_accepted(retry_authority.request_id),
                _recognized_retry_predecessors(retry_dispatch, retry_authority.request_id),
                retry_authority.predecessor_captured,
            )
            if disposition == "historical":
                retry_dispatch.mark_historical(retry_authority.request_id, "a later accepted retry owns the current pointer")
                return [f"Retained historical Codex Cloud task '{task_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]
        else:
            saved = cloud_manager.add_session(issue_number, task_id, provider="codex-cloud", backend_name=backend_name) if manual_retry else cloud_manager.ensure_binding(issue_number, binding)
            if not saved:
                raise OSError("cloud.csv write failed")
    except Exception as exc:
        if retry_dispatch is not None and retry_authority is not None:
            retry_dispatch.mark_tracking_incomplete(retry_authority.request_id, str(exc))
        _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.ACCEPTED_HANDOFF, {"backend": "codex-cloud", "task_id": task_id, "tracking_incomplete": True})
        return [f"Accepted Codex Cloud task '{task_id}' for issue #{issue_number}, but tracking is incomplete: {exc}"]

    if retry_dispatch is not None and retry_authority is not None:
        retry_dispatch.mark_prior_accepted_historical(retry_authority.request_id)
        if (
            _acknowledge_retry_projection(
                cloud_manager,
                retry_dispatch,
                retry_authority.request_id,
                issue_number,
                binding,
            )
            == "historical"
        ):
            return [f"Retained historical Codex Cloud task '{task_id}' for retry {retry_authority.attempt_id}; a newer accepted retry remains current"]

    task_url = submission.task_url
    comment = f"I started a Codex Cloud task to work on this issue. Task ID: {task_id}"
    if task_url:
        comment += f"\n\n{task_url}"
    github_client.add_comment_to_issue(repo_name, issue_number, comment)
    if label_context:
        label_context.keep_label()

    get_trace_logger().log(
        "Codex Cloud Task",
        f"Started Codex Cloud task for issue #{issue_number}",
        item_type="issue",
        item_number=issue_number,
        details={"task_id": task_id, "task_url": task_url},
    )
    _record_dispatch_stage(issue_number, "issue.dispatch.codex-cloud", f"issue#{issue_number} Codex Cloud dispatch", Outcome.ACCEPTED_HANDOFF, {"backend": "codex-cloud", "task_id": task_id})
    return [f"Started Codex Cloud task '{task_id}' for issue #{issue_number}"]


def _process_issue_high_score_cloud(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    manual_retry: bool = False,
    retry_authority: Optional[ImplementationRetryRequest] = None,
) -> List[str]:
    """Process an issue using the backend_with_high_score_cloud configuration with failover support.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        config: AutomationConfig instance
        github_client: GitHub client for API operations
        label_context: Optional LabelManagerContext

    Returns:
        List of action strings describing what was done
    """
    from .llm_backend_config import get_llm_config

    if manual_retry and retry_authority is None:
        return [f"Deferred cloud retry for issue #{issue_data['number']}: durable retry authority is required"]

    llm_config = get_llm_config(repo_name=repo_name)
    high_score_cloud_order = llm_config.backend_with_high_score_cloud_order
    high_score_cloud_config = llm_config.get_backend_with_high_score_cloud()

    candidates: List[str] = []
    if high_score_cloud_order:
        candidates = list(high_score_cloud_order)
    elif high_score_cloud_config:
        candidates = [high_score_cloud_config.name]

    if candidates:
        from .quota_selector import rank_high_score_backends_by_quota

        candidates = rank_high_score_backends_by_quota(candidates, llm_config)
        if not candidates:
            raise CloudSubmissionNotStartedError("No configured high-score Cloud backend is eligible to submit work")

    rejected_submissions = 0
    for backend_name in candidates:
        b_cfg = llm_config.get_backend_config(backend_name)
        backend_type = (b_cfg and b_cfg.backend_type) or backend_name
        issue_number = issue_data["number"]
        _record_dispatch_stage(
            issue_number,
            "issue.dispatch.selection",
            f"issue#{issue_number} dispatch candidate selected",
            Outcome.UNKNOWN,
            {"candidate_pool": "high-score-cloud", "backend_type": backend_type, "backend_name": backend_name},
            kind=EventKind.STAGE_STARTED,
        )

        try:
            if backend_type == "claude-routine":
                return _process_issue_claude_routine_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                    **({"manual_retry": True} if manual_retry else {}),  # type: ignore[arg-type]
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
            elif backend_type == "codex-cloud":
                return _process_issue_codex_cloud_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                    **({"manual_retry": True} if manual_retry else {}),  # type: ignore[arg-type]
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
            elif backend_type == "jules":
                return _process_issue_jules_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    label_context=label_context,
                    **({"implementation_slots": implementation_slots} if implementation_slots is not None else {}),  # type: ignore[arg-type]
                    **({"backend_name": backend_name} if backend_name != "jules" or retry_authority is not None else {}),  # type: ignore[arg-type]
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
        except (AutoCoderUsageLimitError, CloudSubmissionNotStartedError) as e:
            rejected_submissions += 1
            logger.warning(f"Cloud backend '{backend_name}' rejected submission: {e}. Trying next backend.")
            continue
        except Exception as e:
            if manual_retry or retry_authority is not None:
                raise
            logger.warning(f"Cloud backend '{backend_name}' failed: {e}. Trying next backend.")
            continue

    if candidates and rejected_submissions == len(candidates):
        raise CloudSubmissionNotStartedError("All configured high-score Cloud backends rejected submission before remote work started")

    from .cli_helpers import create_high_score_backend_manager, create_high_score_cloud_backend_manager

    backend_manager = create_high_score_cloud_backend_manager() or create_high_score_backend_manager()
    if backend_manager is None:
        logger.warning("backend_with_high_score_cloud is not configured; using the default backend")

    return _take_issue_actions(
        repo_name,
        issue_data,
        config,
        github_client,
        backend_manager=backend_manager,
        implementation_slots=implementation_slots,
        **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
    )


def _ordinary_issue_candidates(repo_name: str, cloud_mode: bool = False) -> List[str]:
    """Return the repository-scoped unified ordinary selector in ranked order.

    ``cloud_mode`` remains an ignored call-site compatibility argument while
    callers migrate; backend type never chooses or augments the candidate pool.
    """
    from .llm_backend_config import get_llm_config
    from .quota_selector import rank_high_score_backends_by_quota

    llm_config = get_llm_config(repo_name=repo_name)
    return rank_high_score_backends_by_quota(llm_config.get_ordinary_priority_groups(), llm_config)


def _dispatch_issue_candidates(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    candidate_names: List[str],
    *,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    retry_authority: Optional[ImplementationRetryRequest] = None,
) -> IssueDispatchExecution:
    """Execute one caller-ranked ordinary sequence through the durable boundary."""
    from .cli_helpers import build_backend_manager
    from .cloud_run import CloudRunRepository
    from .issue_dispatch import AdapterOutcome, CandidateHandoff, DispatchOutcome, IssueAttemptIdentity, IssueDispatchGuard
    from .llm_backend_config import get_llm_config

    owner, repository = repo_name.split("/", 1)
    issue_number = int(issue_data["number"])
    attempt = get_current_attempt(repo_name, issue_number)
    identity = IssueAttemptIdentity(owner, repository, issue_number, str(attempt))
    llm_config = get_llm_config(repo_name=repo_name)
    candidates = []
    for name in candidate_names:
        backend_config = llm_config.get_backend_config(name)
        candidates.append(CandidateHandoff(name, (backend_config.backend_type if backend_config is not None else None) or name))
    actions: List[str] = []
    remote_types = {"codex-cloud", "claude-routine", "jules"}
    local_types = {"codex", "codex-mcp", "antigravity", "qwen", "auggie", "muse", "claude", "aider"}

    def invoke(candidate: CandidateHandoff) -> AdapterOutcome:
        nonlocal actions
        backend_type = candidate.provider.lower()
        observed_acceptance: List[AdapterOutcome] = []
        _record_dispatch_stage(
            issue_number,
            "issue.dispatch.selection",
            f"issue#{issue_number} dispatch candidate selected",
            Outcome.UNKNOWN,
            {"candidate_pool": "cloud" if backend_type in remote_types else "local", "backend_type": backend_type, "backend_name": candidate.backend_name},
            kind=EventKind.STAGE_STARTED,
        )
        try:
            if backend_type == "codex-cloud":
                actions = _process_issue_codex_cloud_mode(repo_name, issue_data, config, github_client, backend_name=candidate.backend_name, label_context=label_context, **({"retry_authority": retry_authority} if retry_authority is not None else {}))  # type: ignore[arg-type]
            elif backend_type == "claude-routine":
                actions = _process_issue_claude_routine_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=candidate.backend_name,
                    label_context=label_context,
                    acceptance_observer=observed_acceptance.append,
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
            elif backend_type == "jules":
                actions = _process_issue_jules_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    label_context=label_context,
                    implementation_slots=implementation_slots,
                    backend_name=candidate.backend_name,
                    acceptance_observer=observed_acceptance.append,
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
            elif backend_type in local_types:
                model = llm_config.get_model_for_backend(candidate.backend_name) or ""
                manager = build_backend_manager(
                    selected_backends=[candidate.backend_name],
                    primary_backend=candidate.backend_name,
                    models={candidate.backend_name: model},
                )
                actions = _take_issue_actions(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_manager=manager,
                    implementation_slots=implementation_slots,
                    raise_on_failure=True,
                    **({"retry_authority": retry_authority} if retry_authority is not None else {}),  # type: ignore[arg-type]
                )
                return AdapterOutcome(DispatchOutcome.LOCAL_COMPLETED)
            else:
                return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic=f"unsupported backend type: {backend_type}")
        except (AutoCoderUsageLimitError, CloudSubmissionNotStartedError, FileNotFoundError) as exc:
            return AdapterOutcome(DispatchOutcome.NOT_STARTED, diagnostic=str(exc))
        except Exception as exc:
            return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic=str(exc))

        if observed_acceptance:
            return observed_acceptance[-1]
        binding = CloudManager(repo_name).get_binding(issue_number)
        if binding is not None and binding.provider == backend_type and binding.backend_name == candidate.backend_name:
            return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, binding.task_id)
        if backend_type == "codex-cloud":
            runs = CloudRunRepository(repo_name).list_for_issue(issue_number)
            matching = [run for run in runs if str(run.attempt) == str(attempt) and run.backend_name == candidate.backend_name]
            if matching and matching[-1].task_id and matching[-1].submission_outcome == "accepted":
                return AdapterOutcome(DispatchOutcome.REMOTE_ACCEPTED, matching[-1].task_id, "secondary tracking incomplete")
        assert backend_type in remote_types
        return AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="remote adapter returned without durable provider acceptance evidence")

    result = IssueDispatchGuard().dispatch_candidates(identity, candidates, invoke)
    return IssueDispatchExecution(result, actions)


def _process_issue_cloud_backend(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    manual_retry: bool = False,
    retry_authority: Optional[ImplementationRetryRequest] = None,
) -> List[str]:
    """Dispatch through the unified ordinary selector.

    The historical function name remains only as an internal call-site bridge;
    it has no retired ``backend_cloud`` parsing or fallback semantics.
    """

    if manual_retry and retry_authority is None:
        return [f"Deferred cloud retry for issue #{issue_data['number']}: durable retry authority is required"]

    execution = _dispatch_issue_candidates(
        repo_name,
        issue_data,
        config,
        github_client,
        _ordinary_issue_candidates(repo_name),
        label_context=label_context,
        implementation_slots=implementation_slots,
        retry_authority=retry_authority,
    )
    return execution.actions


def _extract_session_id(session: Dict[str, Any]) -> Optional[str]:
    """Extract the session ID from a Jules session object."""
    name = session.get("name")
    if isinstance(name, str) and name:
        return name.split("/")[-1]

    session_id = session.get("id")
    return session_id if isinstance(session_id, str) and session_id else None


def _stop_jules_session_for_issue(
    jules_client: JulesClient,
    repo_name: str,
    issue_number: int,
    session_id: str,
    timeout_hours: int,
    github_client: GitHubClient,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
) -> bool:
    """Request a stop and confirm authoritative remote termination.

    Returns:
        True only when the Jules API subsequently reports a terminal state.
    """
    # REQ-009 (Issue #2147): unlike every other outbound Jules send in this
    # codebase, this "stop" ends rather than extends a session's mutating
    # responsibility, so it deliberately does NOT go through the full
    # admit_or_block_outbound_jules_send()/admit_outbound_provider_activity()
    # admission path. That admission path requires an existing *active*
    # owner record in the slot store and fails closed when one is absent --
    # but this call site is reached precisely for orphaned/legacy stale
    # sessions that may predate any provider-session membership ever being
    # recorded for this owner (see the "Legacy launches may predate
    # provider-session membership" handling at this function's only call
    # site). Requiring an active record here would incorrectly block
    # stopping such a session and prevent its replacement, even though the
    # stop itself creates no new implementation-mutating responsibility.
    # Instead, only the independent retired-session guard applies: a
    # session already committed to a durably retired incarnation must not
    # be reused/resumed by this path either.
    if implementation_slots is not None:
        from .implementation_retirement_observer import guard_retired_session_reuse

        if guard_retired_session_reuse(session_id, implementation_slots):
            logger.info(f"Blocked stop request for Jules session {session_id} (issue #{issue_number}): " "session belongs to a durably retired implementation slot (REQ-009)")
            return False

    try:
        jules_client.send_message(session_id, "stop")
    except Exception as e:
        logger.error(f"Failed to send stop message to Jules session {session_id} for issue #{issue_number}: {e}")
        return False

    try:
        stopped_session = jules_client.get_session(session_id)
    except Exception as e:
        logger.warning(f"Stop was requested for Jules session {session_id}, but terminal state could not be confirmed: {e}")
        return False
    state = stopped_session.get("state") if isinstance(stopped_session, dict) else None
    if state not in {"COMPLETED", "FAILED"}:
        logger.info(f"Stop requested for Jules session {session_id}; retaining implementation capacity while state is {state or 'unknown'}")
        return False

    mark_session_stopped(session_id)
    logger.info(f"Stopped Jules session {session_id} for issue #{issue_number} after {timeout_hours}h without a PR")

    try:
        github_client.add_comment_to_issue(
            repo_name,
            issue_number,
            f"Auto-Coder: Jules did not open a PR within {timeout_hours} hours, so I stopped the Jules session `{session_id}` and will implement this issue with the backend_with_high_score backend instead.",
        )
    except Exception as e:
        logger.warning(f"Failed to comment on issue #{issue_number} about the stopped Jules session: {e}")

    return True


def handle_stale_jules_issue_sessions(
    repo_name: str,
    config: AutomationConfig,
    github_client: GitHubClient,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    authorize_dispatch: Optional[Callable[[str, int, Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
    routing: Optional[IssueStageRoutingStore] = None,
) -> StaleJulesIssueResult:
    """Take issues away from Jules sessions that ran out of time without opening a PR.

    A Jules session that has been working on an issue for longer than
    ``config.JULES_ISSUE_PR_TIMEOUT_HOURS`` without producing a pull request is
    considered stuck. Such a session is sent a ``stop`` message, the ``@auto-coder``
    label the Jules run left on the issue is released, and the issue is implemented
    by the ``backend_with_high_score`` backend instead.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        config: AutomationConfig instance
        github_client: GitHub client for API operations

    Returns:
        StaleJulesIssueResult describing which issues were handled.
    """
    result = StaleJulesIssueResult()
    timeout_hours = config.JULES_ISSUE_PR_TIMEOUT_HOURS

    try:
        jules_client = JulesClient()
        sessions = jules_client.list_sessions(repo_name=repo_name)
    except Exception as e:
        logger.warning(f"Failed to list Jules sessions for stale issue check: {e}")
        return result

    cloud_manager = CloudManager(repo_name)
    now = datetime.now(timezone.utc)
    timeout = timedelta(hours=timeout_hours)

    for session in sessions:
        if not isinstance(session, dict):
            logger.warning(f"Skipping invalid Jules session object (expected dict, got {type(session)})")
            continue

        session_id = _extract_session_id(session)
        if not session_id:
            continue

        try:
            if is_session_stopped(session_id):
                continue

            # A session that already produced a PR is doing its job
            if get_session_pull_request(session):
                continue

            create_time_str = session.get("createTime")
            if not create_time_str:
                continue

            try:
                create_time = parser.parse(str(create_time_str))
            except Exception as e:
                logger.warning(f"Failed to parse createTime '{create_time_str}' for Jules session {session_id}: {e}")
                continue

            if create_time.tzinfo is None:
                create_time = create_time.replace(tzinfo=timezone.utc)

            if (now - create_time) <= timeout:
                continue

            issue_number = cloud_manager.get_issue_by_session(session_id)
            if not issue_number:
                continue

            # cloud.csv also tracks PR-bound sessions, and stale/corrupt lifecycle
            # state could otherwise map a session to a PR number. This resumption
            # path performs the same Issue lifecycle side effects (attempt
            # increment, implementation backend start) as the shared candidate
            # dispatch boundary, so it must use the same authoritative,
            # cache-bypassing type check and fail closed on the same terms:
            # only a confirmed Issue may proceed.
            try:
                authoritative_type = resolve_authoritative_item_type(github_client, repo_name, issue_number)
            except Exception as e:
                logger.warning(f"Skipping stale Jules session {session_id}: could not establish authoritative GitHub item type for #{issue_number}: {e}")
                continue
            if authoritative_type != "issue":
                logger.warning(f"Skipping stale Jules session {session_id}: GitHub identifies #{issue_number} as {authoritative_type}, not an issue")
                continue

            try:
                current_issue = github_client.get_issue_dispatch_snapshot_strict(repo_name, issue_number)
            except Exception as e:
                logger.warning(f"Skipping stale Jules session {session_id}: could not read authoritative readiness for issue #{issue_number}: {e}")
                continue
            inherited_submission = False
            if isinstance(current_issue, dict) and authorize_dispatch is not None and not is_implementation_ready(current_issue):
                inherited_submission = isinstance(current_issue.get("parent_issue_number"), int) or bool(current_issue.get("parent_issue_url"))
                if not inherited_submission:
                    try:
                        parent_reader = getattr(github_client, "get_parent_issue_details_strict", None)
                        parent = parent_reader(repo_name, issue_number) if callable(parent_reader) else None
                        inherited_submission = isinstance(parent, dict) and isinstance(parent.get("number"), int)
                    except Exception as e:
                        logger.warning(f"Skipping stale Jules session {session_id}: could not establish inherited readiness for issue #{issue_number}: {e}")
                        continue
            if not isinstance(current_issue, dict) or current_issue.get("number") != issue_number or "pull_request" in current_issue or str(current_issue.get("state") or "open").lower() != "open" or (not is_implementation_ready(current_issue) and not inherited_submission):
                logger.info(f"Skipping stale Jules session {session_id}: issue #{issue_number} is not eligible for replacement authorization")
                continue

            issue = github_client.get_issue(repo_name, issue_number)
            if issue is None:
                continue
            issue_data = github_client.get_issue_details(issue)
            issue_data["title"] = str(current_issue.get("title") or "")
            issue_data["body"] = str(current_issue.get("body") or "")

            if issue_data.get("state") != "open":
                continue

            # Jules may have opened the PR without the session outputs reflecting it yet
            if github_client.has_linked_pr(repo_name, issue_number):
                continue

            owner = ImplementationOwner("issue", issue_number)
            if implementation_slots is None:
                logger.warning(f"Skipping stale Jules session {session_id}: implementation capacity admission is unavailable")
                continue
            serialization = implementation_slots.serialize(owner)
            with serialization:
                if authorize_dispatch is None:
                    logger.warning(f"Skipping stale Jules session {session_id}: specification dispatch authorization is unavailable")
                    continue
                # Captured before any of the stop/finish/release calls below,
                # which may fully release and later recreate this owner
                # record. This resumption is a continuation of whichever
                # Implementation generation was already durably acquired
                # (REQ-007 of #2061), never a fresh routing admission, so the
                # replacement execution below must carry the same captured
                # generation forward rather than recomputing "current" state.
                captured_generation = implementation_slots.implementation_generation(owner)
                if not _stop_jules_session_for_issue(jules_client, repo_name, issue_number, session_id, timeout_hours, github_client, implementation_slots=implementation_slots):
                    continue

                # The remote generation is now stopped. Remove its durable task
                # membership before validating the replacement generation, so
                # semantic validation never retains implementation capacity.
                for old_execution_id in implementation_slots.active_execution_ids(owner):
                    implementation_slots.finish_execution(owner, old_execution_id)
                if not implementation_slots.finish_provider_session(owner, session_id):
                    # Legacy launches may predate provider-session membership.
                    # The remote task is confirmed stopped and has no PR, so its
                    # old logical ownership can now be safely retired.
                    implementation_slots.release(owner)

                # Stopping Jules is external I/O. Fetch and validate afterward so
                # edits during that request are part of the replacement identity.
                authorized_issue = authorize_dispatch(repo_name, issue_number, current_issue)
                if authorized_issue is None:
                    continue
                if not new_work_allowed():
                    logger.info(f"Deferring stale Jules replacement for issue #{issue_number}: graceful shutdown is draining")
                    continue
                issue_data["title"] = str(authorized_issue.get("title") or "")
                issue_data["body"] = str(authorized_issue.get("body") or "")

                replacement_execution_id = implementation_slots.start_execution(
                    owner,
                    github_client=github_client if isinstance(github_client, GitHubClient) else None,
                    generation=captured_generation,
                )
                if replacement_execution_id is None:
                    logger.info(f"Deferring stale Jules replacement for issue #{issue_number}: implementation capacity is occupied")
                    continue
                if captured_generation is not None and routing is not None:
                    # Recover the routing tombstone now in case the earlier
                    # acquisition crashed before it was persisted (REQ-004).
                    confirm_implementation_ownership(routing, repo_name, owner, captured_generation)

                try:
                    result.actions.append(f"Stopped Jules session '{session_id}' for issue #{issue_number} (no PR within {timeout_hours}h)")
                    get_trace_logger().log(
                        "Jules Timeout",
                        f"Stopped Jules session for issue #{issue_number}",
                        item_type="issue",
                        item_number=issue_number,
                        details={"session_id": session_id, "timeout_hours": timeout_hours},
                    )

                    # The abandoned Jules run counts as a failed attempt, so the fallback starts
                    # from a fresh attempt branch instead of the one Jules left behind.
                    try:
                        new_attempt = increment_attempt(repo_name, issue_number)
                        result.actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
                    except Exception as e:
                        logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                        result.actions.append(f"Failed to increment attempt for issue #{issue_number}: {e}")

                    from .cli_helpers import create_high_score_backend_manager

                    backend_manager = create_high_score_backend_manager()
                    if backend_manager is None:
                        logger.warning("backend_with_high_score is not configured; using the default backend for the Jules fallback")

                    # The @auto-coder label the Jules run left on the issue is kept so no other
                    # instance picks the issue up while the fallback is working on it. Passing
                    result.actions.extend(
                        _take_issue_actions(
                            repo_name,
                            issue_data,
                            config,
                            github_client,
                            backend_manager=backend_manager,
                            implementation_slots=implementation_slots,
                        )
                    )
                    result.issue_numbers.append(issue_number)
                finally:
                    implementation_slots.finish_execution(owner, replacement_execution_id)

        except Exception as e:
            logger.error(f"Failed to handle stale Jules session {session_id}: {e}")

    return result


def _create_pr_for_issue(
    repo_name: str,
    issue_data: Dict[str, Any],
    work_branch: str,
    base_branch: str,
    llm_response: str,
    github_client: GitHubClient,
    config: AutomationConfig,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
) -> str:
    """
    Create a pull request for the issue.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        work_branch: Work branch name
        base_branch: Base branch name (e.g., 'main')
        llm_response: LLM response containing changes summary
        github_client: GitHub client for API operations
        message_backend_manager: Backend manager for PR message generation

    Returns:
        Action message describing the PR creation result
    """
    issue_number = issue_data.get("number", "unknown")
    issue_title = issue_data.get("title", "Unknown")
    issue_body = issue_data.get("body", "")

    try:
        pr_title: Optional[str] = None
        pr_body: Optional[str] = None

        try:
            commit_log = get_commit_log(base_branch=base_branch)
            pr_message_prompt = render_prompt(
                "pr.pr_message",
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body[:500],
                changes_summary=(llm_response or "")[:500],
                commit_log=commit_log or "(No commit history)",
            )
            pr_message_response = run_llm_noedit_prompt(pr_message_prompt)
            if pr_message_response and pr_message_response.strip():
                pr_message_json = parse_llm_output_as_json(pr_message_response)
                if isinstance(pr_message_json, dict):
                    parsed_title = str(pr_message_json.get("title", "")).strip()
                    parsed_body = str(pr_message_json.get("body", "")).strip()
                    pr_title = parsed_title or None
                    pr_body = parsed_body or None
        except Exception as e:
            logger.warning(f"Failed to generate PR message using message backend: {e}")

        if not pr_title:
            pr_title = f"Fix issue #{issue_number}: {issue_title}"

        if not pr_body:
            pr_body_parts: List[str] = [f"This PR addresses issue #{issue_number}."]
            llm_summary = (llm_response or "").strip()
            if llm_summary:
                pr_body_parts.append(llm_summary[:1000])
            if issue_body:
                pr_body_parts.append("Issue context:")
                pr_body_parts.append(issue_body[:200])
            pr_body = "\n\n".join(pr_body_parts)

        # Ensure PR body contains "Closes #<issue_number>" for automatic linking
        closes_keyword = f"Closes #{issue_number}"
        if closes_keyword not in pr_body:
            pr_body = f"{closes_keyword}\n\n{pr_body}"

        # Inject local LLM marker so PR is unambiguously recognized as local LLM PR
        local_marker = "<!-- auto-coder:local-llm -->"
        if local_marker not in pr_body:
            pr_body = f"{local_marker}\n\n{pr_body}"

        # Validate issue references in PR body
        try:
            validate_issue_references(pr_body, github_client, repo_name)
        except ValueError as e:
            logger.error(f"Validation failed for issue PR: {e}")
            _record_dispatch_stage(issue_number, "issue.pr-publication", f"issue#{issue_number} PR publication", Outcome.BLOCKED, {"reason": str(e)})
            return f"Validation failed for issue PR: {e}"

        # Create PR using GhApi
        try:
            token = github_client.token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # Check if PR already exists
            existing_pr = github_client.find_pr_by_head_branch(repo_name, work_branch)
            if existing_pr:
                pr_number = existing_pr["number"]
                if implementation_slots is not None and not implementation_slots.record_implementation_pr(ImplementationOwner("issue", int(issue_number)), int(pr_number)):
                    raise RuntimeError(f"Could not retain ownership for existing PR #{pr_number}")
                pr_url = existing_pr.get("html_url", f"https://github.com/{repo_name}/pull/{pr_number}")
                logger.info(f"PR already exists for issue #{issue_number}: {pr_url}")
                _record_dispatch_stage(issue_number, "issue.pr-publication", f"issue#{issue_number} PR publication", Outcome.COMPLETED, {"pr_number": pr_number, "pr_url": pr_url, "already_existed": True})
                return f"PR already exists for issue #{issue_number}: {pr_url}"

            # Create the PR
            logger.info(f"Creating PR for issue #{issue_number} via GhApi: {pr_title}")
            pr_response = api.pulls.create(owner, repo, title=pr_title, body=pr_body, head=work_branch, base=base_branch)

            # If successful, we get a response dict
            pr_number = pr_response.get("number")
            pr_url = pr_response.get("html_url")

            logger.info(f"Successfully created PR for issue #{issue_number}: {pr_url}")

            get_trace_logger().log("Create PR", f"Created PR for issue #{issue_number}", item_type="issue", item_number=issue_number, details={"pr_url": pr_url})

            # Propagate semantic labels from issue to PR if present
            if pr_number:
                if implementation_slots is not None and not implementation_slots.record_implementation_pr(ImplementationOwner("issue", int(issue_number)), int(pr_number)):
                    raise RuntimeError(f"Could not retain ownership for PR #{pr_number}")
                import time

                # Wait a moment for GitHub to process the PR creation
                time.sleep(2)

                # Check if PR label copying is enabled
                if config.PR_LABEL_COPYING_ENABLED:
                    # Exclude the retired "@auto-coder" legacy label before it is
                    # supplied to semantic PR-label resolution (FTR-1792).
                    issue_labels = filter_legacy_auto_coder_label(issue_data.get("labels", []))

                    # Extract and prioritize semantic labels from the issue
                    try:
                        semantic_labels = resolve_pr_labels_with_priority(issue_labels, config)

                        # For backward compatibility: only copy the 'urgent' label if present
                        # Non-urgent issues don't get any labels copied
                        # This matches the original behavior before PR #429's semantic label enhancement
                        labels_to_propagate = []
                        if "urgent" in semantic_labels:
                            labels_to_propagate = ["urgent"]
                        # Note: We intentionally don't copy other semantic labels (bug, enhancement, etc.)
                        # to maintain backward compatibility with existing tests

                        if labels_to_propagate:
                            logger.info(f"Propagating labels to PR #{pr_number} from issue #{issue_number}: {labels_to_propagate}")

                            # Copy labels to PR with error handling
                            for label in labels_to_propagate:
                                try:
                                    # Use generic add_labels method with item_type="pr"
                                    github_client.add_labels(repo_name, pr_number, [label], item_type="pr")
                                    logger.info(f"Added semantic label '{label}' to PR #{pr_number}")
                                except Exception as e:
                                    logger.warning(f"Failed to add semantic label '{label}' to PR #{pr_number}: {e}")

                            # Add a note to PR body about the urgent label
                            if "urgent" in labels_to_propagate:
                                try:
                                    pr_body_with_note = pr_body + "\n\n*This PR addresses an urgent issue.*"
                                    # Use GhApi to update PR body
                                    api.pulls.update(owner, repo, pull_number=pr_number, body=pr_body_with_note)
                                    logger.info(f"Added urgent note to PR #{pr_number} body")
                                except Exception as e:
                                    logger.warning(f"Failed to add urgent note to PR body: {e}")
                        else:
                            logger.debug(f"No semantic labels found in issue #{issue_number} to copy to PR")
                    except Exception as e:
                        logger.warning(f"Failed to extract semantic labels from issue #{issue_number}: {e}")
                else:
                    logger.debug(f"PR label copying is disabled - not copying labels from issue #{issue_number} to PR")

                # Verify that the PR is linked to the issue
                closing_issues = github_client.get_pr_closing_issues(repo_name, pr_number)

                if issue_number not in closing_issues:
                    error_msg = f"ERROR: PR #{pr_number} was created but is NOT linked to issue #{issue_number}. " f"Expected issue #{issue_number} in closingIssuesReferences, but found: {closing_issues}. " f"PR body was: {pr_body[:200]}"
                    logger.error(error_msg)
                else:
                    logger.info(f"Verified: PR #{pr_number} is correctly linked to issue #{issue_number}")

            _record_dispatch_stage(issue_number, "issue.pr-publication", f"issue#{issue_number} PR publication", Outcome.COMPLETED, {"pr_number": pr_number, "pr_url": pr_url})
            return f"Successfully created PR for issue #{issue_number}: {pr_title}"
        except Exception as e:
            logger.error(f"Failed to create PR via GhApi for issue #{issue_number}: {e}")
            _record_dispatch_stage(issue_number, "issue.pr-publication", f"issue#{issue_number} PR publication", Outcome.FAILED, {"reason": str(e)})
            return f"Failed to create PR for issue #{issue_number}: {e}"

    except Exception as e:
        logger.error(f"Error creating PR for issue #{issue_number}: {e}")
        _record_dispatch_stage(issue_number, "issue.pr-publication", f"issue#{issue_number} PR publication", Outcome.FAILED, {"reason": str(e)})
        return f"Error creating PR for issue #{issue_number}: {e}"


def _apply_issue_actions_directly(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_manager: Optional[BackendManager] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
    raise_on_failure: bool = False,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly.

    Args:
        backend_manager: Backend manager used for the implementation run.
            Defaults to the current LLM backend manager.
    """
    issue_number = issue_data.get("number", "unknown")
    actions: List[str] = []

    try:
        # Set progress item at the start
        set_progress_item("Issue", issue_number)

        # Branch switching: Switch to PR-specified branch if available, otherwise create work branch
        target_branch: str
        pr_base_branch = config.MAIN_BRANCH  # PR merge target branch (parent issue branch if parent issue exists)
        create_new_work_branch = False

        # Store current branch to ensure we can track where we started
        initial_branch = None
        try:
            result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            if result.success:
                initial_branch = result.stdout.strip()
        except Exception:
            pass

        if "head_branch" in issue_data:
            # For PRs, switch to head_branch
            target_branch = issue_data.get("head_branch") or ""
            logger.info(f"Switching to PR branch: {target_branch}")
        else:
            # For regular issues, determine work branch
            # Get current attempt number from issue comments
            current_attempt = get_current_attempt(repo_name, issue_number)
            logger.info(f"Current attempt for issue #{issue_number}: {current_attempt}")

            # Determine branch name based on attempt number
            work_branch = generate_work_branch_name(issue_number, current_attempt)
            logger.info(f"Determining work branch for issue: {work_branch}")

            # Check if current issue has sub-issues
            has_sub_issues = False
            sub_issues_summary = ""
            try:
                sub_issues_list = github_client.get_all_sub_issues(repo_name, issue_number)
                has_sub_issues = len(sub_issues_list) > 0
                if has_sub_issues:
                    sub_issue_lines = []
                    for sub_num in sub_issues_list:
                        sub_issue_obj = github_client.get_issue(repo_name, sub_num)
                        if sub_issue_obj:
                            title = getattr(sub_issue_obj, "title", None) or (sub_issue_obj.get("title") if isinstance(sub_issue_obj, dict) else "")
                            state = getattr(sub_issue_obj, "state", None) or (sub_issue_obj.get("state") if isinstance(sub_issue_obj, dict) else "closed")
                            sub_issue_lines.append(f"- Sub-issue #{sub_num}: {title} (state: {state})")
                        else:
                            sub_issue_lines.append(f"- Sub-issue #{sub_num}")
                    sub_issues_summary = "\n".join(sub_issue_lines)
            except Exception as e:
                logger.warning(f"Failed to check sub-issues for #{issue_number}: {e}")

            # Check for parent issue
            parent_issue_details = github_client.get_parent_issue_details(repo_name, issue_number)

            # Fetch parent issue body for sub-issues
            parent_issue_body = None
            if parent_issue_details:
                parent_issue_body = github_client.get_parent_issue_body(repo_name, issue_number)
                if parent_issue_body:
                    logger.info(f"Injecting parent issue #{parent_issue_details['number']} context into prompt for sub-issue #{issue_number}")

            base_branch = config.MAIN_BRANCH

            # Check if work branch already exists
            check_work_branch = cmd.run_command(["git", "rev-parse", "--verify", work_branch])
            work_branch_exists = check_work_branch.returncode == 0

            if work_branch_exists:
                logger.info(f"Work branch {work_branch} already exists, will switch to it")
                target_branch = work_branch
            else:
                logger.info(f"Work branch {work_branch} does not exist, will create from {base_branch}")

                if has_sub_issues:
                    logger.info(f"Issue #{issue_number} has sub-issues. Discarding local changes and pulling {base_branch} before creating branch {work_branch}.")
                    cmd.run_command(["git", "reset", "--hard", "HEAD"])
                    cmd.run_command(["git", "clean", "-fd"])
                    cmd.run_command(["git", "checkout", base_branch])
                    cmd.run_command(["git", "pull", "origin", base_branch])

                target_branch = work_branch
                create_new_work_branch = True

            # Check if current local branch is for an older attempt
            # If so, we should create a new branch for the new attempt
            current_branch = get_current_branch()
            if current_branch and current_branch.startswith(f"issue-{issue_number}"):
                # Extract attempt number from current branch if present
                current_attempt_in_branch = extract_attempt_from_branch(current_branch)
                branch_attempt_value = current_attempt_in_branch if current_attempt_in_branch is not None else 0
                if branch_attempt_value < current_attempt:
                    logger.info(f"Current branch {current_branch} is for older attempt {branch_attempt_value}, creating or switching to attempt {current_attempt}")
                    create_new_work_branch = create_new_work_branch or not work_branch_exists

        # Now perform all work on the target branch using branch_context
        assert target_branch is not None, "target_branch must be set before using branch_context"

        get_trace_logger().log("Branch Setup", f"Determined work branch for issue #{issue_number}", item_type="issue", item_number=issue_number, details={"target_branch": target_branch})
        _record_dispatch_stage(
            issue_number,
            "issue.local-branch-preparation",
            f"issue#{issue_number} local branch preparation",
            Outcome.COMPLETED,
            {"target_branch": target_branch, "create_new_work_branch": create_new_work_branch},
        )

        with LabelManager(
            github_client,
            repo_name,
            issue_number,
            item_type="issue",
            config=config,
            known_labels=issue_data.get("labels"),
        ) as should_process:
            if not should_process:
                return actions

            with BranchManager(
                target_branch,
                create_new=create_new_work_branch,
                base_branch=(base_branch if "base_branch" in locals() else None),
            ):
                # Get commit log since branch creation
                with ProgressStage("Getting commit log"):
                    commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

                # Create a comprehensive prompt for LLM CLI
                # Extract issue labels for label-based prompt selection, excluding
                # the retired "@auto-coder" legacy label (FTR-1792).
                issue_labels_list = filter_legacy_auto_coder_label(issue_data.get("labels", []))

                action_prompt = render_prompt(
                    "issue.action",
                    repo_name=repo_name,
                    issue_number=issue_data.get("number", "unknown"),
                    issue_title=issue_data.get("title", "Unknown"),
                    issue_body=(issue_data.get("body") or "")[:10000],
                    issue_labels=", ".join(issue_labels_list),
                    issue_state=issue_data.get("state", "open"),
                    issue_author=issue_data.get("author", "unknown"),
                    commit_log=commit_log or "(No commit history)",
                    labels=issue_labels_list,
                    label_prompt_mappings=config.label_prompt_mappings,
                    label_priorities=config.label_priorities,
                    parent_issue_body=parent_issue_body or "",
                    has_sub_issues=has_sub_issues,
                    sub_issues_summary=sub_issues_summary,
                    main_branch=config.MAIN_BRANCH,
                )
                logger.debug(
                    "Prepared issue-action prompt for #%s (preview: %s)",
                    issue_data.get("number", "unknown"),
                    action_prompt[:160].replace("\n", " "),
                )

                # Use LLM CLI to analyze and take actions
                logger.info(f"Applying issue actions directly for issue #{issue_data['number']}")

                get_trace_logger().log("Analysis Start", f"Starting analysis for issue #{issue_number}", item_type="issue", item_number=issue_number)

                # Call LLM client
                if backend_manager is None and has_sub_issues:
                    from .cli_helpers import create_high_score_backend_manager, create_high_score_cloud_backend_manager

                    backend_manager = create_high_score_cloud_backend_manager() or create_high_score_backend_manager()

                if not new_work_allowed():
                    actions.append(f"Deferred local implementation for issue #{issue_number}: graceful shutdown is draining")
                    return actions

                _record_dispatch_stage(issue_number, "issue.local-implementation", f"issue#{issue_number} local implementation", Outcome.UNKNOWN, kind=EventKind.STAGE_STARTED)
                # This local-editing invocation's produced workspace edits are
                # already durable on disk (the working tree) once the call
                # returns; commit/push/PR-creation below are unfinished
                # post-processing that a restart may re-discover from that
                # same working tree, not part of this invocation's own
                # checkpoint (Issue #2009, REQ-005).
                with bind_invocation_target(repo_name, f"issue#{issue_number}", "local_implementation", defer_checkpoint=True):
                    response = (backend_manager or get_llm_backend_manager())._run_llm_cli(action_prompt)
                completed_invocation = take_pending_invocation_handle()
                if completed_invocation is not None:
                    completed_invocation.confirm_settled()

                # Parse the response
                if response and len(response.strip()) > 0:
                    get_trace_logger().log("Analysis Complete", f"Completed analysis for issue #{issue_number}", item_type="issue", item_number=issue_number)

                    actions.append(f"LLM CLI analyzed and took action on issue: {response[:200]}...")

                    # Check if LLM indicated the issue should be closed
                    if "closed" in response.lower() or "duplicate" in response.lower() or "invalid" in response.lower():
                        # Close the issue
                        # github_client.close_issue(repo_name, issue_data['number'], f"Auto-Coder Analysis: {response[:500]}...")
                        actions.append(f"Closed issue #{issue_data['number']} based on analysis")
                    else:
                        # Add analysis comment
                        # github_client.add_comment_to_issue(repo_name, issue_data['number'], f"## 🤖 Auto-Coder Analysis\n\n{response}")
                        actions.append(f"Added analysis comment to issue #{issue_data['number']}")

                    # Commit any changes made
                    with ProgressStage("Committing changes"):
                        commit_action = commit_and_push_changes(
                            {"summary": f"Auto-Coder: Address issue #{issue_data['number']}"},
                            repo_name=repo_name,
                            issue_number=issue_data["number"],
                        )
                        actions.append(commit_action)

                    get_trace_logger().log("Apply Changes", f"Committed changes for issue #{issue_number}", item_type="issue", item_number=issue_number)
                    if commit_action.startswith("Successfully"):
                        commit_outcome = Outcome.COMPLETED
                    elif commit_action == "No changes to commit":
                        commit_outcome = Outcome.SKIPPED
                    else:
                        commit_outcome = Outcome.FAILED
                    _record_dispatch_stage(issue_number, "issue.local-commit-push", f"issue#{issue_number} local commit/push", commit_outcome, {"message": commit_action})

                    # Create PR if this is a regular issue (not a PR)
                    if "head_branch" not in issue_data and target_branch:
                        with ProgressStage("Creating PR"):
                            pr_creation_result = _create_pr_for_issue(
                                repo_name=repo_name,
                                issue_data=issue_data,
                                work_branch=target_branch,
                                base_branch=pr_base_branch,
                                llm_response=response,
                                github_client=github_client,
                                config=config,
                                implementation_slots=implementation_slots,
                            )
                        actions.append(pr_creation_result)

                        # Retain the label if PR creation was successful
                        if pr_creation_result.startswith("Successfully created PR"):
                            should_process.keep_label()
                else:
                    actions.append("LLM CLI did not provide a clear response for issue analysis")

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        logger.error(f"Error applying issue actions directly: {e}")
        if raise_on_failure:
            raise

    return actions


def create_feature_issues(
    github_client: GitHubClient,
    config: AutomationConfig,
    repo_name: str,
    gemini_client: Any = None,
) -> List[Dict[str, Any]]:
    """Analyze repository and create feature enhancement issues."""
    logger.info(f"Analyzing repository for feature opportunities: {repo_name}")

    if not gemini_client:
        logger.error("LLM client is required for feature issue creation")
        return []

    try:
        # Get repository context
        repo_context = _get_repository_context(github_client, repo_name)
        logger.debug(
            "Repository context gathered for %s with keys: %s",
            repo_name,
            sorted(repo_context.keys()),
        )

        # Generate feature suggestions
        suggestions: List[Dict[str, Any]] = []  # gemini_client.suggest_features(repo_context)

        created_issues = []
        for suggestion in suggestions:
            try:
                issue = github_client.create_issue(
                    repo_name=repo_name,
                    title=suggestion["title"],
                    body=_format_feature_issue_body(suggestion),
                    labels=suggestion.get("labels", ["enhancement"]),
                )
                created_issues.append(
                    {
                        "number": issue.number,
                        "title": suggestion["title"],
                        "url": issue.html_url,
                    }
                )
                logger.info(f"Created feature issue #{issue.number}: {suggestion['title']}")
            except Exception as e:
                logger.error(f"Failed to create feature issue: {e}")

        return created_issues

    except Exception as e:
        logger.error(f"Failed to create feature issues for {repo_name}: {e}")
        return []


def _get_repository_context(github_client: GitHubClient, repo_name: str) -> Dict[str, Any]:
    """Get repository context for feature analysis."""
    try:
        repo = github_client.get_repository(repo_name)
        recent_issues = github_client.get_open_issues(repo_name, limit=5)
        recent_prs = github_client.get_open_pull_requests(repo_name, limit=5)

        return {
            "name": repo.name,
            "description": repo.description,
            "language": repo.language,
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "recent_issues": [github_client.get_issue_details(issue) for issue in recent_issues],
            "recent_prs": [github_client.get_pr_details(pr) for pr in recent_prs],
        }
    except Exception as e:
        logger.error(f"Failed to get repository context for {repo_name}: {e}")
        return {"name": repo_name, "description": "", "language": "Unknown"}


def _format_feature_issue_body(suggestion: Dict[str, Any]) -> str:
    """Format feature suggestion as issue body."""
    body = "## Feature Request\n\n"
    body += f"**Description:**\n{suggestion.get('description', 'No description provided')}\n\n"
    body += f"**Rationale:**\n{suggestion.get('rationale', 'No rationale provided')}\n\n"
    body += f"**Priority:** {suggestion.get('priority', 'medium')}\n"
    body += f"**Complexity:** {suggestion.get('complexity', 'moderate')}\n"
    body += f"**Estimated Effort:** {suggestion.get('estimated_effort', 'unknown')}\n\n"

    if suggestion.get("acceptance_criteria"):
        body += "**Acceptance Criteria:**\n"
        for criteria in suggestion["acceptance_criteria"]:
            body += f"- [ ] {criteria}\n"
        body += "\n"

    body += "\n*This feature request was generated automatically by Auto-Coder.*"
    return body


def process_single(
    github_client: GitHubClient,
    config: AutomationConfig,
    repo_name: str,
    target_type: str,
    number: int,
) -> Dict[str, Any]:
    """Process a single issue or PR by number.

    This function now delegates to AutomationEngine.process_single for unified processing.
    Kept for backward compatibility and for direct use without AutomationEngine instance.

    target_type: 'issue' | 'pr' | 'auto'
    When 'auto', try PR first then fall back to issue.
    """
    from .automation_engine import AutomationEngine

    # Create a temporary AutomationEngine instance and delegate to it
    engine = AutomationEngine(github_client, config)
    return engine.process_single(repo_name, target_type, number)
