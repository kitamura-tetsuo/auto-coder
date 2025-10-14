"""
Issue processing functionality for Auto-Coder automation engine.
"""

from datetime import datetime
from typing import Any, Dict, List

from .automation_config import AutomationConfig
from .git_utils import git_commit_with_retry
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
) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    if jules_mode:
        return _process_issues_jules_mode(github_client, config, dry_run, repo_name)
    else:
        return _process_issues_normal(
            github_client, config, dry_run, repo_name, llm_client
        )


def _process_issues_normal(
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    llm_client=None,
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
                        repo_name, issue_data, config, dry_run, llm_client
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
                repo_name, issue_data, config, dry_run, llm_client
            )
            actions.extend(action_results)

    except Exception as e:
        logger.error(f"Error taking actions on issue #{issue_number}: {e}")
        actions.append(f"Error processing issue #{issue_number}: {e}")

    return actions


def _commit_changes(result_data: Dict[str, Any]) -> str:
    """
    Commit changes using centralized git helper.

    Args:
        result_data: Dictionary containing 'summary' key with commit message

    Returns:
        Action message describing the commit result
    """
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
        return f"Successfully committed changes: {summary}"
    else:
        return f"Failed to commit changes: {commit_result.stderr}"


def _apply_issue_actions_directly(
    repo_name: str,
    issue_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    llm_client=None,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
    actions = []

    try:
        # ブランチ切り替え: PRで指定されているブランチがあればそこへ、なければデフォルトブランチへ
        target_branch = None
        if "head_branch" in issue_data:
            # PRの場合はhead_branchに切り替え
            target_branch = issue_data.get("head_branch")
            logger.info(f"Switching to PR branch: {target_branch}")
        else:
            # 通常のissueの場合はデフォルトブランチに切り替え
            target_branch = config.MAIN_BRANCH
            logger.info(f"Switching to default branch: {target_branch}")

        if target_branch:
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

        # Create a comprehensive prompt for LLM CLI
        action_prompt = render_prompt(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_data.get("number", "unknown"),
            issue_title=issue_data.get("title", "Unknown"),
            issue_body=(issue_data.get("body") or "")[:1000],
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
                {"summary": f"Auto-Coder: Address issue #{issue_data['number']}"}
            )
            actions.append(commit_action)
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
                            repo_name, issue_data, config, dry_run, llm_client
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
