"""
Issue processing functionality for Auto-Coder automation engine.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .automation_config import AutomationConfig
from .git_utils import ensure_pushed, git_commit_with_retry, git_push, save_commit_failure_history
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()


def process_issues(
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    jules_mode: bool = False,
    llm_client=None,
    message_backend_manager=None,
) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    if jules_mode:
        return _process_issues_jules_mode(github_client, config, dry_run, repo_name)
    else:
        return _process_issues_normal(
            github_client, config, dry_run, repo_name, llm_client, message_backend_manager
        )


def _process_issues_normal(
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    llm_client=None,
    message_backend_manager=None,
) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    try:
        issues = github_client.get_open_issues(
            repo_name, limit=config.max_issues_per_run
        )
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)
                issue_number = issue_data["number"]

                # Skip if issue already has @auto-coder label (being processed by another instance)
                if not dry_run:
                    if not github_client.try_add_work_in_progress_label(
                        repo_name, issue_number
                    ):
                        logger.info(
                            f"Skipping issue #{issue_number} - already has @auto-coder label"
                        )
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": [
                                    "Skipped - already being processed (@auto-coder label present)"
                                ],
                            }
                        )
                        continue

                # Skip if issue has open sub-issues
                open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)
                if open_sub_issues:
                    logger.info(
                        f"Skipping issue #{issue_number} - has {len(open_sub_issues)} open sub-issue(s): {open_sub_issues}"
                    )
                    if not dry_run:
                        # Remove @auto-coder label since we're not processing it
                        github_client.remove_labels_from_issue(
                            repo_name, issue_number, ["@auto-coder"]
                        )
                    processed_issues.append(
                        {
                            "issue_data": issue_data,
                            "actions_taken": [
                                f"Skipped - has open sub-issues: {open_sub_issues}"
                            ],
                        }
                    )
                    continue

                # Skip if issue already has a linked PR
                if github_client.has_linked_pr(repo_name, issue_number):
                    logger.info(
                        f"Skipping issue #{issue_number} - already has a linked PR"
                    )
                    if not dry_run:
                        # Remove @auto-coder label since we're not processing it
                        github_client.remove_labels_from_issue(
                            repo_name, issue_number, ["@auto-coder"]
                        )
                    processed_issues.append(
                        {
                            "issue_data": issue_data,
                            "actions_taken": ["Skipped - already has a linked PR"],
                        }
                    )
                    continue

                processed_issue = {
                    "issue_data": issue_data,
                    "actions_taken": [],
                }

                try:
                    # 単回実行での直接アクション（CLI）
                    actions = _take_issue_actions(
                        repo_name, issue_data, config, dry_run, llm_client, message_backend_manager
                    )
                    processed_issue["actions_taken"] = actions
                finally:
                    # Remove @auto-coder label after processing
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(
                                repo_name, issue_number, ["@auto-coder"]
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to remove @auto-coder label from issue #{issue_number}: {e}"
                            )

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(f"Failed to process issue #{issue.number}: {e}")
                # Try to remove @auto-coder label on error
                if not dry_run:
                    try:
                        github_client.remove_labels_from_issue(
                            repo_name, issue.number, ["@auto-coder"]
                        )
                    except Exception:
                        pass
                processed_issues.append({"issue_number": issue.number, "error": str(e)})

        return processed_issues

    except Exception as e:
        logger.error(f"Failed to process issues for {repo_name}: {e}")
        return []


def _process_issues_jules_mode(
    github_client, config: AutomationConfig, dry_run: bool, repo_name: str
) -> List[Dict[str, Any]]:
    """Process open issues in jules mode - only add 'jules' label."""
    try:
        issues = github_client.get_open_issues(
            repo_name, limit=config.max_issues_per_run
        )
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)
                issue_number = issue_data["number"]

                # Skip if issue already has @auto-coder label (being processed by another instance)
                if not dry_run:
                    if not github_client.try_add_work_in_progress_label(
                        repo_name, issue_number
                    ):
                        logger.info(
                            f"Skipping issue #{issue_number} - already has @auto-coder label"
                        )
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": [
                                    "Skipped - already being processed (@auto-coder label present)"
                                ],
                            }
                        )
                        continue

                # Skip if issue has open sub-issues
                open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)
                if open_sub_issues:
                    logger.info(
                        f"Skipping issue #{issue_number} - has {len(open_sub_issues)} open sub-issue(s): {open_sub_issues}"
                    )
                    if not dry_run:
                        # Remove @auto-coder label since we're not processing it
                        github_client.remove_labels_from_issue(
                            repo_name, issue_number, ["@auto-coder"]
                        )
                    processed_issues.append(
                        {
                            "issue_data": issue_data,
                            "actions_taken": [
                                f"Skipped - has open sub-issues: {open_sub_issues}"
                            ],
                        }
                    )
                    continue

                processed_issue = {"issue_data": issue_data, "actions_taken": []}

                try:
                    # Check if 'jules' label already exists
                    current_labels = issue_data.get("labels", [])
                    if "jules" not in current_labels:
                        if not dry_run:
                            # Add 'jules' label to the issue
                            github_client.add_labels_to_issue(
                                repo_name, issue_number, ["jules"]
                            )
                            processed_issue["actions_taken"].append(
                                f"Added 'jules' label to issue #{issue_number}"
                            )
                            logger.info(f"Added 'jules' label to issue #{issue_number}")
                        else:
                            processed_issue["actions_taken"].append(
                                f"[DRY RUN] Would add 'jules' label to issue #{issue_number}"
                            )
                            logger.info(
                                f"[DRY RUN] Would add 'jules' label to issue #{issue_number}"
                            )
                    else:
                        processed_issue["actions_taken"].append(
                            f"Issue #{issue_number} already has 'jules' label"
                        )
                        logger.info(f"Issue #{issue_number} already has 'jules' label")
                finally:
                    # Remove @auto-coder label after processing
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(
                                repo_name, issue_number, ["@auto-coder"]
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to remove @auto-coder label from issue #{issue_number}: {e}"
                            )

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(
                    f"Failed to process issue #{issue.number} in jules mode: {e}"
                )
                # Try to remove @auto-coder label on error
                if not dry_run:
                    try:
                        github_client.remove_labels_from_issue(
                            repo_name, issue.number, ["@auto-coder"]
                        )
                    except Exception:
                        pass
                processed_issues.append({"issue_number": issue.number, "error": str(e)})

        return processed_issues

    except Exception as e:
        logger.error(f"Failed to process issues in jules mode for {repo_name}: {e}")
        return []


def _take_issue_actions(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    llm_client=None,
    message_backend_manager=None,
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation."""
    actions = []
    issue_number = issue_data["number"]

    try:
        if dry_run:
            actions.append(
                f"[DRY RUN] Would analyze and take actions on issue #{issue_number}"
            )
        else:
            # Ask LLM CLI to analyze the issue and take appropriate actions
            action_results = _apply_issue_actions_directly(
                repo_name, issue_data, config, dry_run, llm_client, message_backend_manager
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
    message_backend_manager=None,
    dry_run: bool = False,
) -> str:
    """
    Create a pull request for the issue.

    Args:
        repo_name: Repository name (e.g., 'owner/repo')
        issue_data: Issue data dictionary
        work_branch: Work branch name
        base_branch: Base branch name (e.g., 'main')
        llm_response: LLM response containing changes summary
        message_backend_manager: Backend manager for PR message generation
        dry_run: Whether this is a dry run

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

        if message_backend_manager:
            try:
                pr_message_prompt = render_prompt(
                    "pr.pr_message",
                    issue_number=issue_number,
                    issue_title=issue_title,
                    issue_body=issue_body[:500],
                    changes_summary=llm_response[:500],
                )
                pr_message_response = message_backend_manager._run_llm_cli(pr_message_prompt)

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

        if dry_run:
            return f"[DRY RUN] Would create PR: {pr_title}"

        # Create PR using gh CLI
        create_pr_result = cmd.run_command(
            [
                "gh", "pr", "create",
                "--base", base_branch,
                "--head", work_branch,
                "--title", pr_title,
                "--body", pr_body,
            ],
            check_success=False
        )

        if create_pr_result.success:
            logger.info(f"Successfully created PR for issue #{issue_number}")
            return f"Successfully created PR for issue #{issue_number}: {pr_title}"
        else:
            logger.error(f"Failed to create PR for issue #{issue_number}: {create_pr_result.stderr}")
            return f"Failed to create PR for issue #{issue_number}: {create_pr_result.stderr}"

    except Exception as e:
        logger.error(f"Error creating PR for issue #{issue_number}: {e}")
        return f"Error creating PR for issue #{issue_number}: {e}"


def _commit_changes(
    result_data: Dict[str, Any], repo_name: Optional[str] = None, issue_number: Optional[int] = None
) -> str:
    """
    Commit changes using centralized git helper.

    Args:
        result_data: Dictionary containing 'summary' key with commit message
        repo_name: Repository name (e.g., 'owner/repo') for history saving
        issue_number: Issue number for context in history

    Returns:
        Action message describing the commit result
    """
    # Push llm commited changes.
    push_result = git_push()

    summary = result_data.get("summary", "Auto-Coder: Automated changes")

    # Check if there are any changes to commit
    status_result = cmd.run_command(
        ["git", "status", "--porcelain"], check_success=False
    )
    if not status_result.stdout.strip():
        return "No changes to commit"

    # Stage all changes
    add_result = cmd.run_command(["git", "add", "-A"], check_success=False)
    if not add_result.success:
        return f"Failed to stage changes: {add_result.stderr}"

    # Commit using centralized helper with dprint retry logic
    commit_result = git_commit_with_retry(summary)

    if commit_result.success:
        # Push changes to remote with retry
        push_result = git_push()
        if push_result.success:
            return f"Successfully committed and pushed changes: {summary}"
        else:
            # Push failed - try one more time after a brief pause
            logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
            import time
            time.sleep(2)
            retry_push_result = git_push()
            if retry_push_result.success:
                return f"Successfully committed and pushed changes (after retry): {summary}"
            else:
                logger.error(f"Failed to push changes after retry: {retry_push_result.stderr}")
                # This is a critical error - we have committed changes but can't push them
                # Log the error but don't exit, as the commit is safe locally
                return f"CRITICAL: Successfully committed changes but failed to push: {summary}. Error: {retry_push_result.stderr}"
    else:
        # Save history and exit immediately
        context = {
            "type": "issue",
            "issue_number": issue_number,
            "commit_message": summary,
        }
        save_commit_failure_history(commit_result.stderr, context, repo_name)
        # This line will never be reached due to sys.exit in save_commit_failure_history
        return f"Failed to commit changes: {commit_result.stderr}"


def _apply_issue_actions_directly(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    llm_client=None,
    message_backend_manager=None,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
    actions = []

    try:
        # Ensure any unpushed commits are pushed before starting
        logger.info("Checking for unpushed commits before processing issue...")
        push_result = ensure_pushed()
        if push_result.success and "No unpushed commits" not in push_result.stdout:
            actions.append(f"Pushed unpushed commits: {push_result.stdout}")
            logger.info("Successfully pushed unpushed commits")
        elif not push_result.success:
            logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")
            actions.append(f"Warning: Failed to push unpushed commits: {push_result.stderr}")

        # ブランチ切り替え: PRで指定されているブランチがあればそこへ、なければ作業用ブランチを作成
        target_branch = None
        if "head_branch" in issue_data:
            # PRの場合はhead_branchに切り替え
            target_branch = issue_data.get("head_branch")
            logger.info(f"Switching to PR branch: {target_branch}")

            # ブランチを切り替え
            checkout_result = cmd.run_command(
                ["git", "checkout", target_branch], check_success=False
            )
            if checkout_result.success:
                actions.append(f"Switched to branch: {target_branch}")
                logger.info(f"Successfully switched to branch: {target_branch}")
            else:
                # ブランチ切り替えに失敗した場合は処理を終了
                error_msg = f"Failed to switch to branch {target_branch}: {checkout_result.stderr}"
                actions.append(error_msg)
                logger.error(error_msg)
                return actions
        else:
            # 通常のissueの場合は作業用ブランチを作成
            issue_number = issue_data.get("number", "unknown")
            work_branch = f"issue-{issue_number}"
            logger.info(f"Creating work branch for issue: {work_branch}")

            # まずデフォルトブランチに切り替え
            checkout_main_result = cmd.run_command(
                ["git", "checkout", config.MAIN_BRANCH], check_success=False
            )
            if not checkout_main_result.success:
                error_msg = f"Failed to switch to main branch {config.MAIN_BRANCH}: {checkout_main_result.stderr}"
                actions.append(error_msg)
                logger.error(error_msg)
                return actions

            # 最新の状態を取得
            pull_result = cmd.run_command(["git", "pull"], check_success=False)
            if not pull_result.success:
                logger.warning(f"Failed to pull latest changes: {pull_result.stderr}")

            # 作業用ブランチを作成して切り替え
            checkout_new_result = cmd.run_command(
                ["git", "checkout", "-b", work_branch], check_success=False
            )
            if checkout_new_result.success:
                actions.append(f"Created and switched to work branch: {work_branch}")
                logger.info(f"Successfully created work branch: {work_branch}")
                target_branch = work_branch
            else:
                # ブランチが既に存在する場合は切り替えのみ
                checkout_existing_result = cmd.run_command(
                    ["git", "checkout", work_branch], check_success=False
                )
                if checkout_existing_result.success:
                    actions.append(f"Switched to existing work branch: {work_branch}")
                    logger.info(f"Switched to existing work branch: {work_branch}")
                    target_branch = work_branch
                else:
                    error_msg = f"Failed to create or switch to work branch {work_branch}: {checkout_new_result.stderr}"
                    actions.append(error_msg)
                    logger.error(error_msg)
                    return actions

        # Create a comprehensive prompt for LLM CLI
        action_prompt = render_prompt(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_data.get("number", "unknown"),
            issue_title=issue_data.get("title", "Unknown"),
            issue_body=(issue_data.get("body") or "")[:10000],
            issue_labels=", ".join(issue_data.get("labels", [])),
            issue_state=issue_data.get("state", "open"),
            issue_author=issue_data.get("author", "unknown"),
        )
        logger.debug(
            "Prepared issue-action prompt for #%s (preview: %s)",
            issue_data.get("number", "unknown"),
            action_prompt[:160].replace("\n", " "),
        )

        # Use LLM CLI to analyze and take actions
        logger.info(
            f"Applying issue actions directly for issue #{issue_data['number']}"
        )

        # Call LLM client
        response = llm_client._run_llm_cli(action_prompt)

        # Parse the response
        if response and len(response.strip()) > 0:
            actions.append(
                f"LLM CLI analyzed and took action on issue: {response[:200]}..."
            )

            # Check if LLM indicated the issue should be closed
            if (
                "closed" in response.lower()
                or "duplicate" in response.lower()
                or "invalid" in response.lower()
            ):
                # Close the issue
                # github_client.close_issue(repo_name, issue_data['number'], f"Auto-Coder Analysis: {response[:500]}...")
                actions.append(
                    f"Closed issue #{issue_data['number']} based on analysis"
                )
            else:
                # Add analysis comment
                # github_client.add_comment_to_issue(repo_name, issue_data['number'], f"## 🤖 Auto-Coder Analysis\n\n{response}")
                actions.append(
                    f"Added analysis comment to issue #{issue_data['number']}"
                )

            # Commit any changes made
            commit_action = _commit_changes(
                {"summary": f"Auto-Coder: Address issue #{issue_data['number']}"},
                repo_name=repo_name,
                issue_number=issue_data['number']
            )
            actions.append(commit_action)

            # Create PR if this is a regular issue (not a PR)
            if "head_branch" not in issue_data and target_branch:
                pr_creation_result = _create_pr_for_issue(
                    repo_name=repo_name,
                    issue_data=issue_data,
                    work_branch=target_branch,
                    base_branch=config.MAIN_BRANCH,
                    llm_response=response,
                    message_backend_manager=message_backend_manager,
                    dry_run=dry_run,
                )
                actions.append(pr_creation_result)
        else:
            actions.append(
                "LLM CLI did not provide a clear response for issue analysis"
            )

    except Exception as e:
        logger.error(f"Error applying issue actions directly: {e}")

    return actions


def create_feature_issues(
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    gemini_client=None,
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
        suggestions = []  # gemini_client.suggest_features(repo_context)

        created_issues = []
        for suggestion in suggestions:
            if not dry_run:
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
                    logger.info(
                        f"Created feature issue #{issue.number}: {suggestion['title']}"
                    )
                except Exception as e:
                    logger.error(f"Failed to create feature issue: {e}")
            else:
                logger.info(
                    f"[DRY RUN] Would create feature issue: {suggestion['title']}"
                )
                created_issues.append({"title": suggestion["title"], "dry_run": True})

        return created_issues

    except Exception as e:
        logger.error(f"Failed to create feature issues for {repo_name}: {e}")
        return []


def _get_repository_context(github_client, repo_name: str) -> Dict[str, Any]:
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
            "recent_issues": [
                github_client.get_issue_details(issue) for issue in recent_issues
            ],
            "recent_prs": [github_client.get_pr_details(pr) for pr in recent_prs],
        }
    except Exception as e:
        logger.error(f"Failed to get repository context for {repo_name}: {e}")
        return {"name": repo_name, "description": "", "language": "Unknown"}


def _format_feature_issue_body(suggestion: Dict[str, Any]) -> str:
    """Format feature suggestion as issue body."""
    body = "## Feature Request\n\n"
    body += f"**Description:**\n{suggestion.get('description', 'No description provided')}\n\n"
    body += (
        f"**Rationale:**\n{suggestion.get('rationale', 'No rationale provided')}\n\n"
    )
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
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    target_type: str,
    number: int,
    jules_mode: bool = False,
    llm_client=None,
    message_backend_manager=None,
) -> Dict[str, Any]:
    """Process a single issue or PR by number.

    target_type: 'issue' | 'pr' | 'auto'
    When 'auto', try PR first then fall back to issue.
    """
    logger.info(
        f"Processing single target: type={target_type}, number={number} for {repo_name}"
    )
    result = {
        "repository": repo_name,
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "jules_mode": jules_mode,
        "issues_processed": [],
        "prs_processed": [],
        "errors": [],
    }
    try:
        resolved_type = target_type
        if target_type == "auto":
            # Prefer PR to avoid mislabeling PR issues
            try:
                pr_data = github_client.get_pr_details_by_number(repo_name, number)
                resolved_type = "pr"
            except Exception:
                resolved_type = "issue"
        if resolved_type == "pr":
            try:
                from .pr_processor import _take_pr_actions

                pr_data = github_client.get_pr_details_by_number(repo_name, number)
                actions = _take_pr_actions(
                    repo_name, pr_data, config, dry_run, llm_client
                )
                processed_pr = {
                    "pr_data": pr_data,
                    "actions_taken": actions,
                    "priority": "single",
                }
                result["prs_processed"].append(processed_pr)
            except Exception as e:
                msg = f"Failed to process PR #{number}: {e}"
                logger.error(msg)
                result["errors"].append(msg)
        else:
            try:
                issue_data = github_client.get_issue_details_by_number(
                    repo_name, number
                )

                # Skip if issue already has @auto-coder label (being processed by another instance)
                if not dry_run:
                    if not github_client.try_add_work_in_progress_label(
                        repo_name, number
                    ):
                        msg = (
                            f"Skipping issue #{number} - already has @auto-coder label"
                        )
                        logger.info(msg)
                        result["errors"].append(msg)
                        return result

                processed_issue = {
                    "issue_data": issue_data,
                    "analysis": None,
                    "solution": None,
                    "actions_taken": [],
                }

                try:
                    if jules_mode:
                        # Mimic jules mode behavior
                        current_labels = issue_data.get("labels", [])
                        if "jules" not in current_labels:
                            if not dry_run:
                                github_client.add_labels_to_issue(
                                    repo_name, number, ["jules"]
                                )
                                processed_issue["actions_taken"].append(
                                    f"Added 'jules' label to issue #{number}"
                                )
                            else:
                                processed_issue["actions_taken"].append(
                                    f"[DRY RUN] Would add 'jules' label to issue #{number}"
                                )
                        else:
                            processed_issue["actions_taken"].append(
                                f"Issue #{number} already has 'jules' label"
                            )
                    else:
                        actions = _take_issue_actions(
                            repo_name, issue_data, config, dry_run, llm_client, message_backend_manager
                        )
                        processed_issue["actions_taken"] = actions
                finally:
                    # Remove @auto-coder label after processing
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(
                                repo_name, number, ["@auto-coder"]
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to remove @auto-coder label from issue #{number}: {e}"
                            )

                result["issues_processed"].append(processed_issue)
            except Exception as e:
                msg = f"Failed to process issue #{number}: {e}"
                logger.error(msg)
                # Try to remove @auto-coder label on error
                if not dry_run:
                    try:
                        github_client.remove_labels_from_issue(
                            repo_name, number, ["@auto-coder"]
                        )
                    except Exception:
                        pass
                result["errors"].append(msg)
    except Exception as e:
        msg = f"Error in process_single: {e}"
        logger.error(msg)
        result["errors"].append(msg)
    return result
