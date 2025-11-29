"""
Issue processing functionality for Auto-Coder automation engine.
"""

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from auto_coder.util.github_action import _check_github_actions_status, check_and_handle_closed_state, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .attempt_manager import get_current_attempt
from .automation_config import AutomationConfig, ProcessedIssueResult, ProcessResult
from .backend_manager import get_llm_backend_manager, run_llm_message_prompt
from .cloud_manager import CloudManager
from .gh_logger import get_gh_logger
from .git_branch import branch_context, extract_attempt_from_branch
from .git_commit import commit_and_push_changes
from .git_info import get_commit_log, get_current_branch
from .github_client import GitHubClient
from .jules_client import JulesClient
from .label_manager import LabelManager, LabelOperationError, resolve_pr_labels_with_priority
from .logger_config import get_logger
from .progress_footer import ProgressStage, newline_progress, set_progress_item
from .prompt_loader import render_prompt
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
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation."""
    actions = []
    issue_number = issue_data["number"]

    try:
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
            pr_message_response = run_llm_message_prompt(pr_message_prompt)

            if pr_message_response and len(pr_message_response.strip()) > 0:
                # Parse the response (first line is title, rest is body)
                lines = pr_message_response.strip().split("\n")
                pr_title = lines[0].strip()
                if len(lines) > 2:
                    pr_body = "\n".join(lines[2:]).strip()
                logger.info(f"Generated PR message using message backend: {pr_title}")
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

            base_branch = config.MAIN_BRANCH
            if parent_issue_details:
                parent_issue_number = parent_issue_details["number"]
                parent_state = parent_issue_details.get("state", "OPEN").upper()

                if parent_state == "OPEN":
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
                    logger.info(f"Issue #{issue_number} has parent issue #{parent_issue_number} but it is {parent_state}. Ignoring parent branch and using {config.MAIN_BRANCH} as base.")

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
    jules_mode: bool = False,
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
    return engine.process_single(repo_name, target_type, number, jules_mode)


def _process_issue_jules_mode(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: GitHubClient,
) -> List[str]:
    """Process an issue using Jules session mode.

    This function:
    1. Checks if the issue is already managed via CloudManager
    2. If not managed:
       - Starts a Jules session using JulesClient
       - Saves issue/session mapping to cloud.csv
       - Adds a comment to the GitHub issue
    3. Uses the existing issue.action prompt content as the prompt for Jules

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        config: AutomationConfig instance
        github_client: GitHub client for API operations

    Returns:
        List of action messages describing what was done
    """
    actions = []
    issue_number = issue_data.get("number")
    issue_title = issue_data.get("title", "Unknown")
    issue_body = issue_data.get("body", "")

    if not issue_number:
        logger.error("Issue data missing issue number")
        return ["Error: Issue data missing issue number"]

    try:
        # Initialize CloudManager to check if issue is already managed
        cloud_manager = CloudManager(repo_name)

        # Check if issue is already managed
        if cloud_manager.is_managed(issue_number):
            actions.append(f"Issue #{issue_number} is already managed by a Jules session")
            return actions

        # Initialize Jules client
        try:
            jules_client = JulesClient()
        except Exception as e:
            error_msg = f"Failed to initialize Jules client: {e}"
            logger.error(error_msg)
            actions.append(error_msg)
            return actions

        # Get issue labels for prompt selection
        issue_labels_list = issue_data.get("labels", [])

        # Create prompt using the existing issue.action template
        action_prompt = render_prompt(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_data.get("number", "unknown"),
            issue_title=issue_title,
            issue_body=issue_body[:10000],
            issue_labels=", ".join(issue_labels_list),
            issue_state=issue_data.get("state", "open"),
            issue_author=issue_data.get("author", "unknown"),
            commit_log="(No commit history)",  # Will be filled by Jules
            labels=issue_labels_list,
            label_prompt_mappings=config.label_prompt_mappings,
            label_priorities=config.label_priorities,
        )

        # Start Jules session with the prompt
        try:
            session_id = jules_client.start_session(action_prompt)
            logger.info(f"Started Jules session {session_id} for issue #{issue_number}")

            # Save issue/session mapping to cloud.csv
            success = cloud_manager.add_session(issue_number, session_id)
            if success:
                actions.append(f"Saved Jules session mapping for issue #{issue_number}: {session_id}")
            else:
                actions.append(f"Warning: Failed to save Jules session mapping for issue #{issue_number}")

            # Add comment to the GitHub issue
            comment_body = f"Jules session started: {session_id}"

            try:
                # Use gh CLI to add comment
                gh_logger = get_gh_logger()
                result = gh_logger.execute_with_logging(
                    [
                        "gh",
                        "issue",
                        "comment",
                        str(issue_number),
                        "--body",
                        comment_body,
                    ],
                    repo=repo_name,
                )

                if result.returncode == 0:
                    actions.append(f"Added Jules session comment to issue #{issue_number}")
                else:
                    actions.append(f"Warning: Failed to add comment to issue #{issue_number}: {result.stderr}")

            except Exception as e:
                actions.append(f"Warning: Failed to add comment to issue #{issue_number}: {e}")

        except Exception as e:
            error_msg = f"Failed to start Jules session for issue #{issue_number}: {e}"
            logger.error(error_msg)
            actions.append(error_msg)

    except Exception as e:
        error_msg = f"Error processing issue #{issue_number} with Jules mode: {e}"
        logger.error(error_msg)
        actions.append(error_msg)

    return actions
