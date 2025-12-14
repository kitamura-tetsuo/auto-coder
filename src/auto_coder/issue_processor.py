"""
Issue processing functionality for Auto-Coder automation engine.
"""

import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from auto_coder.util.github_action import _check_github_actions_status, check_and_handle_closed_state, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .attempt_manager import get_current_attempt
from .automation_config import AutomationConfig, ProcessedIssueResult, ProcessResult
from .backend_manager import get_llm_backend_manager, parse_llm_output_as_json, run_llm_noedit_prompt
from .cloud_manager import CloudManager
from .gh_logger import get_gh_logger
from .git_branch import branch_context, extract_attempt_from_branch
from .git_commit import commit_and_push_changes
from .git_info import get_commit_log, get_current_branch
from .github_client import GitHubClient
from .jules_client import JulesClient
from .label_manager import LabelManager, LabelManagerContext, LabelOperationError, resolve_pr_labels_with_priority
from .logger_config import get_logger
from .progress_footer import ProgressStage, newline_progress, set_progress_item
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()


def ensure_parent_issue_open(
    github_client: GitHubClient,
    repo_name: str,
    parent_issue_details: Dict[str, Any],
    issue_number: int,
) -> bool:
    """
    Ensure parent issue is open, reopening if necessary.

    This hook checks the state of the parent issue and reopens it if it's closed
    before processing the child issue. This ensures parent issues remain open
    during sub-issue processing and can track progress properly.

    Args:
        github_client: GitHub client for API operations
        repo_name: Repository name (e.g., 'owner/repo')
        parent_issue_details: Parent issue details from get_parent_issue_details
        issue_number: Current issue number being processed

    Returns:
        bool: True if parent issue is open (or was reopened), False otherwise

    Note:
        Adds an audit comment when reopening to track when and why the parent
        issue was reopened for child issue processing.
    """
    parent_issue_number = parent_issue_details.get("number")
    parent_state = parent_issue_details.get("state", "UNKNOWN").upper()

    if parent_state == "OPEN":
        logger.debug(
            "Parent issue #%s for issue #%s is already OPEN",
            parent_issue_number,
            issue_number,
        )
        return True

    if parent_state == "CLOSED":
        try:
            logger.info(
                "Reopening closed parent issue #%s before processing child issue #%s",
                parent_issue_number,
                issue_number,
            )

            # Add an audit comment when reopening
            audit_comment = f"Auto-Coder: Reopened this parent issue to process child issue #{issue_number}. Branch and base selection will use the parent context."

            # Call GitHub API to reopen the issue
            github_client.reopen_issue(repo_name, parent_issue_number, audit_comment)

            logger.info(
                "Successfully reopened parent issue #%s for child issue #%s",
                parent_issue_number,
                issue_number,
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to reopen parent issue #%s for child issue #%s: %s",
                parent_issue_number,
                issue_number,
                e,
            )
            return False

    logger.warning(
        "Unexpected parent issue #%s state '%s' for issue #%s",
        parent_issue_number,
        parent_state,
        issue_number,
    )
    return False


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


def _create_pr_for_parent_issue(
    repo_name: str,
    issue_data: Dict[str, Any],
    github_client: GitHubClient,
    config: AutomationConfig,
    summary: str,
    reasoning: str,
) -> str:
    """
    Create a PR for a parent issue after verification passes.

    This function creates a PR that confirms the completion of a parent issue.
    The PR links to the parent issue and documents the verification results.

    The function manages branch creation/switching for the parent issue, creates
    a completion marker file documenting the verification results, and creates
    a PR that properly links to the parent issue for tracking.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        github_client: GitHub client for API operations
        config: AutomationConfig instance
        summary: Summary from verification
        reasoning: Reasoning from verification

    Returns:
        Action message describing the PR creation result
    """
    issue_number = issue_data["number"]
    issue_title = issue_data.get("title", "Unknown")
    actions_list = []

    try:
        current_attempt = get_current_attempt(repo_name, issue_number)
        logger.info(f"Current attempt for parent issue #{issue_number}: {current_attempt}")

        # Generate branch name for parent issue
        parent_branch = generate_work_branch_name(issue_number, current_attempt)

        # Check if branch already exists
        check_branch = cmd.run_command(["git", "rev-parse", "--verify", parent_branch])
        branch_exists = check_branch.returncode == 0

        # If branch doesn't exist, create it from main
        if not branch_exists:
            logger.info(f"Creating parent issue branch: {parent_branch}")
            create_branch_result = cmd.run_command(["git", "checkout", "-b", parent_branch, config.MAIN_BRANCH])
            if not create_branch_result.success:
                logger.error(f"Failed to create branch {parent_branch}: {create_branch_result.stderr}")
                return f"Failed to create branch for parent issue #{issue_number}"

            # Push the new branch
            push_result = cmd.run_command(["git", "push", "-u", "origin", parent_branch])
            if not push_result.success:
                logger.warning(f"Failed to push branch {parent_branch}: {push_result.stderr}")
                return f"Warning: Created branch {parent_branch} but failed to push it"

            actions_list.append(f"Created and published parent branch: {parent_branch}")
            logger.info(f"Successfully created and published parent branch: {parent_branch}")
        else:
            # Switch to existing branch
            switch_result = cmd.run_command(["git", "checkout", parent_branch])
            if not switch_result.success:
                logger.error(f"Failed to switch to branch {parent_branch}: {switch_result.stderr}")
                return f"Failed to switch to parent branch for issue #{issue_number}"

        # Check if there are any changes to commit
        status_result = cmd.run_command(["git", "status", "--porcelain"])
        has_changes = bool(status_result.stdout.strip())

        # Create a completion marker file or update existing files if needed
        # For a clean PR, we'll create a simple completion marker
        completion_file_path = "PARENT_ISSUE_COMPLETION.md"

        # Check if completion file already exists
        check_file = cmd.run_command(["test", "-f", completion_file_path])
        file_exists = check_file.returncode == 0

        if not file_exists or has_changes:
            # Create or update the completion marker
            completion_content = f"""# Parent Issue Completion

## Parent Issue
**Number:** {issue_number}
**Title:** {issue_title}

## Verification Summary
{summary}

## Verification Reasoning
{reasoning}

## Completion Status
This parent issue has been successfully completed. All sub-issues have been processed and their requirements verified.

Generated by Auto-Coder on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""
            write_result = cmd.run_command(["sh", "-c", f"cat > {completion_file_path} << 'EOF'\n{completion_content}\nEOF"])
            if not write_result.success:
                logger.warning(f"Failed to create completion marker: {write_result.stderr}")
            else:
                has_changes = True

        # Commit changes if any
        if has_changes:
            commit_message = f"Auto-Coder: Mark parent issue #{issue_number} as complete"
            add_result = cmd.run_command(["git", "add", completion_file_path])
            if add_result.success:
                from .git_branch import git_commit_with_retry

                commit_result = git_commit_with_retry(commit_message)
                if commit_result.success:
                    # Push the commit
                    push_result = cmd.run_command(["git", "push", "origin", parent_branch])
                    if not push_result.success:
                        logger.warning(f"Failed to push commits: {push_result.stderr}")
                        return f"Warning: Created commit for parent issue #{issue_number} but failed to push"
                else:
                    logger.warning(f"Failed to commit completion marker: {commit_result.stderr}")
                    return f"Warning: Could not commit changes for parent issue #{issue_number}"

        # Create PR using gh CLI
        gh_logger = get_gh_logger()
        pr_title = f"Complete parent issue #{issue_number}: {issue_title}"
        pr_body = f"""## Parent Issue Completion

This PR confirms the completion of parent issue #{issue_number}.

### Verification Summary
{summary}

### Verification Reasoning
{reasoning}

### Completion Status
All sub-issues have been processed and their requirements verified. This parent issue is now complete.

Closes #{issue_number}
"""

        create_pr_result = gh_logger.execute_with_logging(
            [
                "gh",
                "pr",
                "create",
                "--base",
                config.MAIN_BRANCH,
                "--head",
                parent_branch,
                "--title",
                pr_title,
                "--body",
                pr_body,
            ],
            repo=repo_name,
        )

        if create_pr_result.success:
            logger.info(f"Successfully created PR for parent issue #{issue_number}")

            # Extract PR number from the output
            pr_url = create_pr_result.stdout.strip()
            pr_number = None
            if pr_url:
                try:
                    pr_number = int(pr_url.split("/")[-1])
                    logger.info(f"Extracted PR number: {pr_number}")
                except (ValueError, IndexError) as e:
                    logger.warning(f"Failed to extract PR number from URL '{pr_url}': {e}")

            # Verify that the PR is linked to the issue
            if pr_number:
                closing_issues = github_client.get_pr_closing_issues(repo_name, pr_number)

                if issue_number not in closing_issues:
                    error_msg = f"ERROR: PR #{pr_number} was created but is NOT linked to issue #{issue_number}."
                    logger.error(error_msg)
                else:
                    logger.info(f"Verified: PR #{pr_number} is correctly linked to issue #{issue_number}")

            return f"Successfully created PR for parent issue #{issue_number}: {pr_title}"
        else:
            logger.error(f"Failed to create PR for parent issue #{issue_number}: {create_pr_result.stderr}")
            return f"Failed to create PR for parent issue #{issue_number}: {create_pr_result.stderr}"

    except Exception as e:
        logger.error(f"Error creating PR for parent issue #{issue_number}: {e}")
        return f"Error creating PR for parent issue #{issue_number}: {e}"


def _take_issue_actions(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation."""
    actions = []
    issue_number = issue_data["number"]

    try:
        # Check if this is a parent issue (has sub-issues, no parent, all sub-issues closed)
        all_sub_issues = github_client.get_all_sub_issues(repo_name, issue_number)
        parent_issue_details = github_client.get_parent_issue_details(repo_name, issue_number)
        open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)

        is_parent_issue = len(all_sub_issues) > 0 and parent_issue_details is None and len(open_sub_issues) == 0  # Has sub-issues  # No parent  # All sub-issues closed

        if is_parent_issue:
            logger.info(f"Issue #{issue_number} detected as parent issue with all sub-issues closed")
            # Create PR directly for parent issue
            pr_action = _create_pr_for_parent_issue(
                repo_name=repo_name,
                issue_data=issue_data,
                github_client=github_client,
                config=config,
                summary="All sub-issues completed",
                reasoning="All sub-issues have been closed",
            )
            actions.append(pr_action)
        else:
            # Ask LLM CLI to analyze the issue and take appropriate actions
            action_results = _apply_issue_actions_directly(
                repo_name,
                issue_data,
                config,
                github_client,
            )
            actions.extend(action_results)

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
        action_prompt = render_prompt(
            "issue.action",
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
        )

        logger.info(f"Starting Jules session for issue #{issue_number}")

        # Determine base branch (default to main)
        base_branch = config.MAIN_BRANCH

        # Check for parent issue
        parent_issue_details = github_client.get_parent_issue_details(repo_name, issue_number)
        if parent_issue_details:
            parent_issue_number = parent_issue_details["number"]
            # Call hook to ensure parent issue is open
            parent_is_open = ensure_parent_issue_open(github_client, repo_name, parent_issue_details, issue_number)

            if parent_is_open:
                # If parent issue exists and is OPEN, use parent issue branch as base
                # Check if parent issue has attempts and use the appropriate parent branch
                parent_attempt = get_current_attempt(repo_name, parent_issue_number)
                if parent_attempt > 0:
                    parent_branch = f"issue-{parent_issue_number}_attempt-{parent_attempt}"
                else:
                    parent_branch = f"issue-{parent_issue_number}"

                logger.info(f"Issue #{issue_number} has OPEN parent issue #{parent_issue_number}, using branch {parent_branch} as base for Jules session")

                # Check if parent issue branch exists
                check_parent_branch = cmd.run_command(["git", "rev-parse", "--verify", parent_branch])
                if check_parent_branch.returncode == 0:
                    base_branch = parent_branch
                else:
                    # Check if branch exists on remote
                    check_remote = cmd.run_command(["git", "ls-remote", "--exit-code", "--heads", "origin", parent_branch])

                    if check_remote.returncode == 0:
                        # Exists on remote but not locally
                        logger.info(f"Parent branch {parent_branch} exists on remote but not locally. Using it as base.")
                        base_branch = parent_branch
                    else:
                        # Doesn't exist on remote either - create and push it
                        logger.info(f"Parent branch {parent_branch} does not exist locally or on remote. Creating it from {config.MAIN_BRANCH}...")

                        # Create branch locally (without checkout)
                        create_result = cmd.run_command(["git", "branch", parent_branch, config.MAIN_BRANCH])
                        if not create_result.success:
                            logger.error(f"Failed to create parent branch {parent_branch}: {create_result.stderr}")
                            # Still try to use it as base, though it will likely fail later if it doesn't exist
                            base_branch = parent_branch
                        else:
                            # Push to remote
                            push_result = cmd.run_command(["git", "push", "-u", "origin", parent_branch])
                            if not push_result.success:
                                logger.warning(f"Failed to push parent branch {parent_branch}: {push_result.stderr}")
                            else:
                                logger.info(f"Successfully created and pushed parent branch {parent_branch}")

                            base_branch = parent_branch

        # Start Jules session
        session_title = f"{issue_title} (#{issue_number})"
        session_id = jules_client.start_session(action_prompt, repo_name, base_branch, title=session_title)

        # Store session ID in cloud.csv
        cloud_manager = CloudManager(repo_name)
        success = cloud_manager.add_session(issue_number, session_id)

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

        # Keep the @auto-coder label if context was provided
        if label_context:
            label_context.keep_label()
            logger.info(f"Keeping @auto-coder label for issue #{issue_number} (Jules session started)")

    except Exception as e:
        logger.error(f"Error processing issue #{issue_number} in Jules mode: {e}")
        actions.append(f"Error processing issue #{issue_number} in Jules mode: {e}")

    return actions


def _create_pr_for_issue(
    repo_name: str,
    issue_data: Dict[str, Any],
    work_branch: str,
    base_branch: str,
    llm_response: str,
    github_client: GitHubClient,
    config: AutomationConfig,
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
        # Generate PR message using message backend if available
        pr_title = None
        pr_body = None

        try:
            # Get commit log since branch creation for PR message context
            commit_log = get_commit_log(base_branch=base_branch)

            pr_message_prompt = render_prompt(
                "pr.pr_message",
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body[:500],
                changes_summary=llm_response[:500],
                commit_log=commit_log or "(No commit history)",
            )
            pr_message_response = run_llm_noedit_prompt(pr_message_prompt)

            if pr_message_response and len(pr_message_response.strip()) > 0:
                # Parse the JSON response using the standalone parser
                # This handles conversation history and extracts the last message
                try:
                    pr_message_json = parse_llm_output_as_json(pr_message_response)
                    pr_title = pr_message_json.get("title", "")
                    pr_body = pr_message_json.get("body", "")
                    logger.info(f"Generated PR message using message backend: {pr_title}")
                except (json.JSONDecodeError, ValueError) as e:
                    # Fallback to direct JSON parsing if backend parser fails
                    logger.debug(f"Backend JSON parsing failed, trying direct parse: {e}")
                    try:
                        pr_message_json = json.loads(pr_message_response.strip())
                        pr_title = pr_message_json.get("title", "")
                        pr_body = pr_message_json.get("body", "")
                        logger.info(f"Generated PR message using message backend: {pr_title}")
                    except json.JSONDecodeError as json_error:
                        # Fallback to old format parsing if not valid JSON
                        logger.debug(f"Direct JSON parsing failed, using fallback: {json_error}")
                        lines = pr_message_response.strip().split("\n")
                        pr_title = lines[0].strip()
                        if len(lines) > 2:
                            pr_body = "\n".join(lines[2:]).strip()
                        logger.info(f"Generated PR message using message backend (fallback): {pr_title}")
        except Exception as e:
            logger.warning(f"Failed to generate PR message using message backend: {e}")

        # Fallback to default PR message if generation failed
        if not pr_title:
            pr_title = f"Fix issue #{issue_number}: {issue_title}"
        if not pr_body:
            pr_body = f"This PR addresses issue #{issue_number}.\n\n{issue_body[:200]}"

        # Ensure PR body contains "Closes #<issue_number>" for automatic linking
        closes_keyword = f"Closes #{issue_number}"
        if closes_keyword not in pr_body:
            pr_body = f"{closes_keyword}\n\n{pr_body}"

        # Create PR using gh CLI
        gh_logger = get_gh_logger()
        create_pr_result = gh_logger.execute_with_logging(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                work_branch,
                "--title",
                pr_title,
                "--body",
                pr_body,
            ],
            repo=repo_name,
        )

        if create_pr_result.success:  # type: ignore[attr-defined]
            logger.info(f"Successfully created PR for issue #{issue_number}")

            # Extract PR number from the output
            # gh pr create outputs URL like: https://github.com/owner/repo/pull/123
            pr_url = create_pr_result.stdout.strip()
            pr_number = None
            if pr_url:
                try:
                    pr_number = int(pr_url.split("/")[-1])
                    logger.info(f"Extracted PR number: {pr_number}")
                except (ValueError, IndexError) as e:
                    logger.warning(f"Failed to extract PR number from URL '{pr_url}': {e}")

            # Propagate semantic labels from issue to PR if present
            if pr_number:
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
                                    gh_logger = get_gh_logger()
                                    gh_logger.execute_with_logging(
                                        [
                                            "gh",
                                            "pr",
                                            "edit",
                                            str(pr_number),
                                            "--body",
                                            pr_body_with_note,
                                        ],
                                        repo=repo_name,
                                    )
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
        else:
            logger.error(f"Failed to create PR for issue #{issue_number}: {create_pr_result.stderr}")
            return f"Failed to create PR for issue #{issue_number}: {create_pr_result.stderr}"

    except Exception as e:
        logger.error(f"Error creating PR for issue #{issue_number}: {e}")
        return f"Error creating PR for issue #{issue_number}: {e}"


def _apply_issue_actions_directly(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
    issue_number = issue_data.get("number", "unknown")
    actions = []

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

            # Check for parent issue
            parent_issue_details = github_client.get_parent_issue_details(repo_name, issue_number)

            # Fetch parent issue body for sub-issues
            parent_issue_body = None
            if parent_issue_details:
                parent_issue_body = github_client.get_parent_issue_body(repo_name, issue_number)
                if parent_issue_body:
                    logger.info(f"Injecting parent issue #{parent_issue_details['number']} context into prompt for sub-issue #{issue_number}")

            base_branch = config.MAIN_BRANCH
            if parent_issue_details:
                parent_issue_number = parent_issue_details["number"]
                # Call hook to ensure parent issue is open
                parent_is_open = ensure_parent_issue_open(github_client, repo_name, parent_issue_details, issue_number)

                if parent_is_open:
                    # If parent issue exists and is OPEN, use parent issue branch as base
                    # Check if parent issue has attempts and use the appropriate parent branch
                    parent_attempt = get_current_attempt(repo_name, parent_issue_number)
                    if parent_attempt > 0:
                        parent_branch = f"issue-{parent_issue_number}_attempt-{parent_attempt}"
                    else:
                        parent_branch = f"issue-{parent_issue_number}"
                    logger.info(f"Issue #{issue_number} has OPEN parent issue #{parent_issue_number}, using branch {parent_branch} as base")

                    # Check if parent issue branch exists
                    check_parent_branch = cmd.run_command(["git", "rev-parse", "--verify", parent_branch])

                    if check_parent_branch.returncode == 0:
                        # Use parent issue branch if it exists
                        base_branch = parent_branch
                        pr_base_branch = parent_branch  # Also set PR merge target to parent issue branch
                        logger.info(f"Parent branch {parent_branch} exists, using it as base")
                    else:
                        # Create parent issue branch if it doesn't exist
                        logger.info(f"Parent branch {parent_branch} does not exist, creating it")

                        # Create parent issue branch from the configured main branch (automatically pushed to remote)
                        with branch_context(parent_branch, create_new=True, base_branch=config.MAIN_BRANCH):
                            actions.append(f"Created and published parent branch: {parent_branch}")
                            logger.info(f"Successfully created and published parent branch: {parent_branch}")

                        base_branch = parent_branch
                        pr_base_branch = parent_branch  # Also set PR merge target to parent issue branch
                else:
                    parent_state_for_log = parent_issue_details.get("state", "UNKNOWN").upper()
                    logger.info(f"Issue #{issue_number} has parent issue #{parent_issue_number} but it is {parent_state_for_log}. Ignoring parent branch and using {config.MAIN_BRANCH} as base.")

            # Check if work branch already exists
            check_work_branch = cmd.run_command(["git", "rev-parse", "--verify", work_branch])
            work_branch_exists = check_work_branch.returncode == 0

            if work_branch_exists:
                logger.info(f"Work branch {work_branch} already exists, will switch to it")
                target_branch = work_branch
            else:
                logger.info(f"Work branch {work_branch} does not exist, will create from {base_branch}")
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
        with LabelManager(github_client, repo_name, issue_number, item_type="issue", config=config, check_labels=config.CHECK_LABELS) as should_process:
            if not should_process:
                return actions

            with branch_context(
                target_branch,
                create_new=create_new_work_branch,
                base_branch=(base_branch if "base_branch" in locals() else None),
            ):
                # Get commit log since branch creation
                with ProgressStage("Getting commit log"):
                    commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

                # Create a comprehensive prompt for LLM CLI
                # Extract issue labels for label-based prompt selection
                issue_labels_list = issue_data.get("labels", [])

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
                )
                logger.debug(
                    "Prepared issue-action prompt for #%s (preview: %s)",
                    issue_data.get("number", "unknown"),
                    action_prompt[:160].replace("\n", " "),
                )

                # Use LLM CLI to analyze and take actions
                logger.info(f"Applying issue actions directly for issue #{issue_data['number']}")

                # Call LLM client
                response = get_llm_backend_manager()._run_llm_cli(action_prompt)

                # Parse the response
                if response and len(response.strip()) > 0:
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
                            )
                        actions.append(pr_creation_result)

                        # Retain the label if PR creation was successful
                        if pr_creation_result.startswith("Successfully created PR"):
                            should_process.keep_label()
                else:
                    actions.append("LLM CLI did not provide a clear response for issue analysis")

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
