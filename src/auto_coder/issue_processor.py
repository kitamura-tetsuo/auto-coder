"""
Issue processing functionality for Auto-Coder automation engine.
"""

import json
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, TypedDict, Union, cast

from dateutil import parser

from auto_coder.util.gh_cache import get_ghapi_client, is_implementation_ready, resolve_authoritative_item_type
from auto_coder.util.github_action import _check_github_actions_status, check_and_handle_closed_state, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .attempt_manager import get_current_attempt, increment_attempt
from .automation_config import AutomationConfig, ProcessedIssueResult, ProcessResult, StaleJulesIssueResult
from .backend_manager import BackendManager, get_llm_backend_manager, parse_llm_output_as_json, run_llm_noedit_prompt
from .branch_manager import BranchManager
from .cloud_manager import CloudManager
from .exceptions import AutoCoderRetryableBackendError, AutoCoderUsageLimitError
from .git_branch import branch_context, extract_attempt_from_branch
from .git_commit import commit_and_push_changes
from .git_info import get_commit_log, get_current_branch
from .implementation_slots import ImplementationOwner, ImplementationSlotRepository
from .issue_context import get_linked_issues_context, validate_issue_references
from .jules_client import JulesClient
from .jules_engine import get_session_pull_request, is_session_stopped, mark_session_stopped
from .label_manager import LabelManager, LabelManagerContext, LabelOperationError, resolve_pr_labels_with_priority
from .logger_config import get_gh_logger, get_logger
from .progress_footer import ProgressStage, newline_progress, set_progress_item
from .prompt_loader import render_prompt
from .shutdown_context import new_work_allowed
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()


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


def _take_issue_actions(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_manager: Optional[BackendManager] = None,
    check_labels: Optional[bool] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation.

    Args:
        backend_manager: Backend manager used for the implementation run.
            Defaults to the current LLM backend manager.
        check_labels: Whether an existing @auto-coder label blocks processing.
            Defaults to ``config.CHECK_LABELS``. Pass False when this run already
            owns the label and must keep it in place.
    """
    actions = []
    issue_number = issue_data["number"]

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
            action_results = _apply_issue_actions_directly(
                repo_name,
                issue_data,
                config,
                github_client,
                backend_manager=backend_manager,
                check_labels=check_labels,
            )
        else:
            action_results = _apply_issue_actions_directly(
                repo_name,
                issue_data,
                config,
                github_client,
                backend_manager=backend_manager,
                check_labels=check_labels,
                implementation_slots=implementation_slots,
            )
        actions.extend(action_results)

    except AutoCoderRetryableBackendError:
        raise
    except Exception as e:
        logger.error(f"Error taking actions on issue #{issue_number}: {e}")
        actions.append(f"Error processing issue #{issue_number}: {e}")

    return actions


def _process_issue_jules_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
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

    try:
        # Initialize Jules client
        jules_client = JulesClient()

        # Prepare the prompt for Jules
        # Prepare the prompt for Jules
        issue_labels_list = []
        for label in issue_data.get("labels", []):
            if isinstance(label, dict):
                issue_labels_list.append(label.get("name", ""))
            elif isinstance(label, str):
                issue_labels_list.append(label)

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
            return [f"Deferred Jules session for issue #{issue_number}: graceful shutdown is draining"]

        logger.info(f"Starting Jules session for issue #{issue_number}")

        # Determine base branch (default to main)
        base_branch = config.MAIN_BRANCH

        # Start Jules session
        session_title = f"{issue_title} (#{issue_number})"
        session_id = jules_client.start_session(action_prompt, repo_name, base_branch, title=session_title)

        # Store session ID in cloud.csv
        cloud_manager = CloudManager(repo_name)
        success = cloud_manager.add_session(issue_number, session_id, provider="jules")

        if not success:
            logger.warning(f"Failed to save session ID to cloud.csv for issue #{issue_number}")
            actions.append(f"Warning: Could not save session ID for issue #{issue_number}")
        else:
            logger.info(f"Saved session ID '{session_id}' for issue #{issue_number}")

        # Comment on the issue with session ID
        try:
            comment_body = f"I started a Jules session to work on this issue. Session ID: {session_id}\n\nhttps://jules.google.com/session/{session_id}"
            github_client.add_comment_to_issue(repo_name, issue_number, comment_body)
            actions.append(f"Commented on issue #{issue_number} with Jules session ID")
            logger.info(f"Added comment with session ID to issue #{issue_number}")

            # Add @auto-coder label
            try:
                github_client.add_labels(repo_name, issue_number, ["@auto-coder"])
                logger.info(f"Added @auto-coder label to issue #{issue_number}")
            except Exception as e:
                logger.warning(f"Failed to add @auto-coder label to issue #{issue_number}: {e}")

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


def _process_issue_claude_routine_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_name: Optional[str] = None,
    label_context: Optional[LabelManagerContext] = None,
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

    try:
        from .claude_routine_client import ClaudeRoutineClient

        routine_client = ClaudeRoutineClient(backend_name=backend_name, repo_name=repo_name)

        issue_labels_list = []
        for label in issue_data.get("labels", []):
            if isinstance(label, dict):
                issue_labels_list.append(label.get("name", ""))
            elif isinstance(label, str):
                issue_labels_list.append(label)

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
            return [f"Deferred Claude Routine session for issue #{issue_number}: graceful shutdown is draining"]

        logger.info(f"Starting Claude Routine session for issue #{issue_number}")

        base_branch = config.MAIN_BRANCH

        session_title = f"{issue_title} (#{issue_number})"
        session_id, session_url = routine_client.fire_routine(action_prompt, repo_name=repo_name, base_branch=base_branch, title=session_title)

        cloud_manager = CloudManager(repo_name)
        success = cloud_manager.add_session(
            issue_number,
            session_id,
            provider="claude-routine",
            backend_name=backend_name or "claude-routine",
        )

        if not success:
            logger.warning(f"Failed to save session ID to cloud.csv for issue #{issue_number}")
            actions.append(f"Warning: Could not save session ID for issue #{issue_number}")
        else:
            logger.info(f"Saved session ID '{session_id}' for issue #{issue_number}")

        try:
            comment_body = f"I started a Claude Routine session to work on this issue. Session ID: {session_id}"
            if session_url:
                comment_body += f"\n\n{session_url}"
            github_client.add_comment_to_issue(repo_name, issue_number, comment_body)
            actions.append(f"Commented on issue #{issue_number} with Claude Routine session ID")
            logger.info(f"Added comment with session ID to issue #{issue_number}")

            try:
                github_client.add_labels(repo_name, issue_number, ["@auto-coder"])
                logger.info(f"Added @auto-coder label to issue #{issue_number}")
            except Exception as e:
                logger.warning(f"Failed to add @auto-coder label to issue #{issue_number}: {e}")

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
) -> List[str]:
    """Submit an issue to Codex Cloud and persist its task identifier.

    Dispatch is guarded by a durable `CloudRun` record keyed by
    (issue_number, attempt): if a Codex Cloud run already exists for the
    issue's current attempt, `CodexCloudClient.start_task()` is not called
    again. This protection is independent of the `@auto-coder` label, so it
    remains effective across a process restart or when the label is absent,
    stale, or temporarily inconsistent (see issue #1606).
    """
    from .cloud_run import CloudRun, CloudRunRepository
    from .codex_cloud_client import CodexCloudClient

    issue_number = issue_data["number"]
    issue_title = issue_data.get("title", "Unknown")

    attempt = get_current_attempt(repo_name, issue_number)
    cloud_run_repo = CloudRunRepository(repo_name)
    existing_run = cloud_run_repo.get(issue_number, attempt)
    if existing_run is not None and existing_run.provider == "codex-cloud":
        logger.info(f"Codex Cloud run '{existing_run.task_id}' already exists for issue #{issue_number} attempt {attempt}; not starting a duplicate task")
        if label_context:
            label_context.keep_label()
        return [f"Codex Cloud task '{existing_run.task_id}' already running for issue #{issue_number} attempt {attempt}; skipped duplicate dispatch"]

    issue_labels = [label.get("name", "") if isinstance(label, dict) else str(label) for label in issue_data.get("labels", [])]
    prompt = render_prompt(
        "issue.action",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_data.get("body", ""),
        issue_labels=", ".join(issue_labels),
        issue_state=issue_data.get("state", "open"),
        issue_author=issue_data.get("user", {}).get("login", "unknown"),
        commit_log=get_commit_log(base_branch=config.MAIN_BRANCH) or "(No commit history)",
        is_jules=True,
    )

    if not new_work_allowed():
        return [f"Deferred Codex Cloud task for issue #{issue_number}: graceful shutdown is draining"]

    client = CodexCloudClient(backend_name=backend_name, repo_name=repo_name)
    task_id = client.start_task(
        prompt,
        repo_name=repo_name,
        base_branch=config.MAIN_BRANCH,
        title=f"{issue_title} (#{issue_number})",
    )
    cloud_run_repo.save(
        CloudRun(
            repo_name=repo_name,
            issue_number=issue_number,
            attempt=attempt,
            provider="codex-cloud",
            task_id=task_id,
        )
    )
    CloudManager(repo_name).add_session(issue_number, task_id, provider="codex-cloud")

    task_url = client.task_urls.get(task_id)
    comment = f"I started a Codex Cloud task to work on this issue. Task ID: {task_id}"
    if task_url:
        comment += f"\n\n{task_url}"
    github_client.add_comment_to_issue(repo_name, issue_number, comment)
    github_client.add_labels(repo_name, issue_number, ["@auto-coder"])

    if label_context:
        label_context.keep_label()

    get_trace_logger().log(
        "Codex Cloud Task",
        f"Started Codex Cloud task for issue #{issue_number}",
        item_type="issue",
        item_number=issue_number,
        details={"task_id": task_id, "task_url": task_url},
    )
    return [f"Started Codex Cloud task '{task_id}' for issue #{issue_number}"]


def _process_issue_high_score_cloud(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
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

    for backend_name in candidates:
        b_cfg = llm_config.get_backend_config(backend_name)
        backend_type = (b_cfg and b_cfg.backend_type) or backend_name

        try:
            if backend_type == "claude-routine":
                return _process_issue_claude_routine_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                )
            elif backend_type == "codex-cloud":
                return _process_issue_codex_cloud_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                )
            elif backend_type == "jules":
                return _process_issue_jules_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    label_context=label_context,
                )
        except AutoCoderUsageLimitError as e:
            logger.warning(f"Cloud backend '{backend_name}' hit usage limit: {e}. Trying next backend.")
            continue
        except Exception as e:
            logger.warning(f"Cloud backend '{backend_name}' failed: {e}. Trying next backend.")
            continue

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
        check_labels=False,
        implementation_slots=implementation_slots,
    )


def _process_issue_cloud_backend(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    label_context: Optional[LabelManagerContext] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
) -> List[str]:
    """Process an issue using the backend_cloud configuration with failover support.

    If backend_cloud is not configured, falls back to Jules mode.

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

    llm_config = get_llm_config(repo_name=repo_name)
    cloud_order = llm_config.backend_cloud_order
    cloud_priority_groups = llm_config.backend_cloud_priority_groups
    cloud_config = llm_config.get_backend_cloud()

    priority_candidates: Union[List[str], List[List[str]]]
    if cloud_priority_groups:
        priority_candidates = list(cloud_priority_groups)
    elif cloud_order:
        priority_candidates = list(cloud_order)
    elif cloud_config:
        priority_candidates = [cloud_config.name]
    else:
        # Default to jules if backend_cloud is not explicitly configured
        priority_candidates = ["jules"]

    from .quota_selector import rank_high_score_backends_by_quota

    candidates = rank_high_score_backends_by_quota(priority_candidates, llm_config)

    for backend_name in candidates:
        b_cfg = llm_config.get_backend_config(backend_name)
        backend_type = (b_cfg and b_cfg.backend_type) or backend_name

        try:
            if backend_type == "claude-routine":
                return _process_issue_claude_routine_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                )
            elif backend_type == "codex-cloud":
                return _process_issue_codex_cloud_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    backend_name=backend_name,
                    label_context=label_context,
                )
            elif backend_type == "jules":
                return _process_issue_jules_mode(
                    repo_name,
                    issue_data,
                    config,
                    github_client,
                    label_context=label_context,
                )
        except AutoCoderUsageLimitError as e:
            logger.warning(f"Cloud backend '{backend_name}' hit usage limit: {e}. Trying next backend.")
            continue
        except Exception as e:
            logger.warning(f"Cloud backend '{backend_name}' failed: {e}. Trying next backend.")
            continue

    from .cli_helpers import create_cloud_backend_manager

    backend_manager = create_cloud_backend_manager()
    if backend_manager is None:
        logger.warning("backend_cloud is not configured or all candidates failed; using the default backend")

    return _take_issue_actions(
        repo_name,
        issue_data,
        config,
        github_client,
        backend_manager=backend_manager,
        check_labels=False,
        implementation_slots=implementation_slots,
    )


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
) -> bool:
    """Request a stop and confirm authoritative remote termination.

    Returns:
        True only when the Jules API subsequently reports a terminal state.
    """
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
                if not _stop_jules_session_for_issue(jules_client, repo_name, issue_number, session_id, timeout_hours, github_client):
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

                replacement_execution_id = implementation_slots.start_execution(owner)
                if replacement_execution_id is None:
                    logger.info(f"Deferring stale Jules replacement for issue #{issue_number}: implementation capacity is occupied")
                    continue

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
                    # check_labels=False makes the label gate let this run through instead of
                    # skipping the issue it already owns.
                    result.actions.extend(
                        _take_issue_actions(
                            repo_name,
                            issue_data,
                            config,
                            github_client,
                            backend_manager=backend_manager,
                            check_labels=False,
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
                    issue_labels = issue_data.get("labels", [])

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

            return f"Successfully created PR for issue #{issue_number}: {pr_title}"
        except Exception as e:
            logger.error(f"Failed to create PR via GhApi for issue #{issue_number}: {e}")
            return f"Failed to create PR for issue #{issue_number}: {e}"

    except Exception as e:
        logger.error(f"Error creating PR for issue #{issue_number}: {e}")
        return f"Error creating PR for issue #{issue_number}: {e}"


def _apply_issue_actions_directly(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
    backend_manager: Optional[BackendManager] = None,
    check_labels: Optional[bool] = None,
    implementation_slots: Optional[ImplementationSlotRepository] = None,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly.

    Args:
        backend_manager: Backend manager used for the implementation run.
            Defaults to the current LLM backend manager.
        check_labels: Whether an existing @auto-coder label blocks processing.
            Defaults to ``config.CHECK_LABELS``. Pass False when this run already
            owns the label and must keep it in place.
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

        with LabelManager(
            github_client,
            repo_name,
            issue_number,
            item_type="issue",
            config=config,
            check_labels=config.CHECK_LABELS if check_labels is None else check_labels,
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
                # Extract issue labels for label-based prompt selection
                issue_labels_list = []
                for label in issue_data.get("labels", []):
                    if isinstance(label, dict):
                        issue_labels_list.append(label.get("name", ""))
                    elif isinstance(label, str):
                        issue_labels_list.append(label)

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

                response = (backend_manager or get_llm_backend_manager())._run_llm_cli(action_prompt)

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
