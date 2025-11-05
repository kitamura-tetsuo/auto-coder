"""
Issue processing functionality for Auto-Coder automation engine.
"""

import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from auto_coder.backend_manager import get_llm_backend_manager, run_message_prompt
from auto_coder.util.github_action import get_detailed_checks_from_history

from .automation_config import AutomationConfig
from .git_utils import commit_and_push_changes, ensure_pushed, get_commit_log, git_checkout_branch, switch_to_branch
from .logger_config import get_logger
from .progress_footer import ProgressStage, newline_progress, push_progress_stage, set_progress_item
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
) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    if jules_mode:
        return _process_issues_jules_mode(github_client, config, dry_run, repo_name)
    else:
        return _process_issues_normal(
            github_client,
            config,
            dry_run,
            repo_name,
        )


def _process_issues_normal(
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    try:
        issues = github_client.get_open_issues(repo_name, limit=config.max_issues_per_run)
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)
                issue_number = issue_data["number"]

                # Set progress item
                set_progress_item("Issue", issue_number)
                # Check if issue already has @auto-coder label (being processed by another instance)
                with ProgressStage("Checking status"):
                    current_labels = issue_data.get("labels", [])
                    if "@auto-coder" in current_labels:
                        logger.info(f"Skipping issue #{issue_number} - already has @auto-coder label")
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": ["Skipped - already being processed (@auto-coder label present)"],
                            }
                        )
                        newline_progress()
                        continue

                # Skip if issue has open sub-issues
                with ProgressStage("Checking sub-issues"):
                    open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)
                    if open_sub_issues:
                        logger.info(f"Skipping issue #{issue_number} - has {len(open_sub_issues)} open sub-issue(s): {open_sub_issues}")
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": [f"Skipped - has open sub-issues: {open_sub_issues}"],
                            }
                        )
                        newline_progress()
                        continue

                # Skip if issue already has a linked PR
                with ProgressStage("Checking linked PR"):
                    if github_client.has_linked_pr(repo_name, issue_number):
                        logger.info(f"Skipping issue #{issue_number} - already has a linked PR")
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": ["Skipped - already has a linked PR"],
                            }
                        )
                        newline_progress()
                        continue

                # Add @auto-coder label now that we're actually going to process this issue
                if not dry_run:
                    if not github_client.try_add_work_in_progress_label(repo_name, issue_number):
                        logger.info(f"Skipping issue #{issue_number} - @auto-coder label was just added by another instance")
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": ["Skipped - another instance started processing (@auto-coder label added)"],
                            }
                        )
                        newline_progress()
                        continue

                processed_issue = {
                    "issue_data": issue_data,
                    "actions_taken": [],
                }

                try:
                    # 単回実行での直接アクション（CLI）
                    with ProgressStage("Processing"):
                        actions = _take_issue_actions(
                            repo_name,
                            issue_data,
                            config,
                            dry_run,
                            github_client,
                        )
                        processed_issue["actions_taken"] = actions
                finally:
                    # Remove @auto-coder label after processing
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(repo_name, issue_number, ["@auto-coder"])
                        except Exception as e:
                            logger.warning(f"Failed to remove @auto-coder label from issue #{issue_number}: {e}")
                    # Clear progress header after processing
                    newline_progress()

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(f"Failed to process issue #{issue.number}: {e}")
                # Try to remove @auto-coder label on error
                if not dry_run:
                    try:
                        github_client.remove_labels_from_issue(repo_name, issue.number, ["@auto-coder"])
                    except Exception:
                        pass
                processed_issues.append({"issue_number": issue.number, "error": str(e)})
                # Clear progress header on error
                newline_progress()

        return processed_issues

    except Exception as e:
        logger.error(f"Failed to process issues for {repo_name}: {e}")
        return []


def _process_issues_jules_mode(github_client, config: AutomationConfig, dry_run: bool, repo_name: str) -> List[Dict[str, Any]]:
    """Process open issues in jules mode - only add 'jules' label."""
    try:
        issues = github_client.get_open_issues(repo_name, limit=config.max_issues_per_run)
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)
                issue_number = issue_data["number"]

                # Check if issue already has @auto-coder label (being processed by another instance)
                current_labels = issue_data.get("labels", [])
                if "@auto-coder" in current_labels:
                    logger.info(f"Skipping issue #{issue_number} - already has @auto-coder label")
                    processed_issues.append(
                        {
                            "issue_data": issue_data,
                            "actions_taken": ["Skipped - already being processed (@auto-coder label present)"],
                        }
                    )
                    continue

                # Skip if issue has open sub-issues
                open_sub_issues = github_client.get_open_sub_issues(repo_name, issue_number)
                if open_sub_issues:
                    logger.info(f"Skipping issue #{issue_number} - has {len(open_sub_issues)} open sub-issue(s): {open_sub_issues}")
                    processed_issues.append(
                        {
                            "issue_data": issue_data,
                            "actions_taken": [f"Skipped - has open sub-issues: {open_sub_issues}"],
                        }
                    )
                    continue

                # Add @auto-coder label now that we're actually going to process this issue
                if not dry_run:
                    if not github_client.try_add_work_in_progress_label(repo_name, issue_number):
                        logger.info(f"Skipping issue #{issue_number} - @auto-coder label was just added by another instance")
                        processed_issues.append(
                            {
                                "issue_data": issue_data,
                                "actions_taken": ["Skipped - another instance started processing (@auto-coder label added)"],
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
                            github_client.add_labels_to_issue(repo_name, issue_number, ["jules"])
                            processed_issue["actions_taken"].append(f"Added 'jules' label to issue #{issue_number}")
                            logger.info(f"Added 'jules' label to issue #{issue_number}")
                        else:
                            processed_issue["actions_taken"].append(f"[DRY RUN] Would add 'jules' label to issue #{issue_number}")
                            logger.info(f"[DRY RUN] Would add 'jules' label to issue #{issue_number}")
                    else:
                        processed_issue["actions_taken"].append(f"Issue #{issue_number} already has 'jules' label")
                        logger.info(f"Issue #{issue_number} already has 'jules' label")
                finally:
                    # Remove @auto-coder label after processing
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(repo_name, issue_number, ["@auto-coder"])
                        except Exception as e:
                            logger.warning(f"Failed to remove @auto-coder label from issue #{issue_number}: {e}")

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(f"Failed to process issue #{issue.number} in jules mode: {e}")
                # Try to remove @auto-coder label on error
                if not dry_run:
                    try:
                        github_client.remove_labels_from_issue(repo_name, issue.number, ["@auto-coder"])
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
    github_client,
) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation."""
    actions = []
    issue_number = issue_data["number"]

    try:
        if dry_run:
            actions.append(f"[DRY RUN] Would analyze and take actions on issue #{issue_number}")
        else:
            # Ask LLM CLI to analyze the issue and take appropriate actions
            action_results = _apply_issue_actions_directly(
                repo_name,
                issue_data,
                config,
                dry_run,
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
    github_client,
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
        github_client: GitHub client for API operations
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

        try:
            pr_message_prompt = render_prompt(
                "pr.pr_message",
                issue_number=issue_number,
                issue_title=issue_title,
                issue_body=issue_body[:500],
                changes_summary=llm_response[:500],
            )
            pr_message_response = run_message_prompt(pr_message_prompt)

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

        if dry_run:
            return f"[DRY RUN] Would create PR: {pr_title}"

        # Create PR using gh CLI
        create_pr_result = cmd.run_command(
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
            ]
        )

        if create_pr_result.success:
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

            # Propagate urgent label from issue to PR if present
            if pr_number:
                import time

                # Wait a moment for GitHub to process the PR creation
                time.sleep(2)

                # Check if source issue has urgent label and propagate to PR
                issue_labels = issue_data.get("labels", [])
                if "urgent" in issue_labels:
                    try:
                        github_client.add_labels_to_issue(repo_name, pr_number, ["urgent"])
                        logger.info(f"Propagated 'urgent' label from issue #{issue_number} to PR #{pr_number}")
                        # Add note to PR body about urgent status
                        try:
                            pr_body_with_note = pr_body + "\n\n*This PR addresses an urgent issue.*"
                            cmd.run_command(
                                [
                                    "gh",
                                    "pr",
                                    "edit",
                                    str(pr_number),
                                    "--body",
                                    pr_body_with_note,
                                ]
                            )
                            logger.info(f"Added urgent note to PR #{pr_number} body")
                        except Exception as e:
                            logger.warning(f"Failed to add urgent note to PR body: {e}")
                    except Exception as e:
                        logger.warning(f"Failed to propagate 'urgent' label to PR #{pr_number}: {e}")

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
    dry_run: bool,
    github_client,
) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
    actions = []
    issue_number = issue_data.get("number", "unknown")

    try:
        # Set progress item at the start
        set_progress_item("Issue", issue_number)

        # Ensure any unpushed commits are pushed before starting
        with ProgressStage("Checking unpushed commits"):
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
        pr_base_branch = config.MAIN_BRANCH  # PRのマージ先ブランチ（親issueがある場合は親issueブランチ）
        if "head_branch" in issue_data:
            # PRの場合はhead_branchに切り替え
            target_branch = issue_data.get("head_branch")
            logger.info(f"Switching to PR branch: {target_branch}")

            # ブランチを切り替え
            checkout_result = git_checkout_branch(target_branch)
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
            work_branch = f"issue-{issue_number}"
            logger.info(f"Creating work branch for issue: {work_branch}")

            # 親issueを確認
            parent_issue_number = github_client.get_parent_issue(repo_name, issue_number)

            base_branch = config.MAIN_BRANCH
            if parent_issue_number:
                # 親issueが存在する場合、親issueのブランチを基準にする
                parent_branch = f"issue-{parent_issue_number}"
                logger.info(f"Issue #{issue_number} has parent issue #{parent_issue_number}, using branch {parent_branch} as base")

                # 親issueのブランチが存在するか確認
                check_parent_branch = cmd.run_command(["git", "rev-parse", "--verify", parent_branch])

                if check_parent_branch.returncode == 0:
                    # 親issueのブランチが存在する場合はそれを使用
                    base_branch = parent_branch
                    pr_base_branch = parent_branch  # PRのマージ先も親issueブランチに設定
                    logger.info(f"Parent branch {parent_branch} exists, using it as base")

                    # ブランチを最新状態へ更新
                    switch_result = switch_to_branch(parent_branch, pull_after_switch=True)
                    if not switch_result.success:
                        logger.warning(f"Failed to pull latest changes from {parent_branch}: {switch_result.stderr}")
                else:
                    # 親issueのブランチが存在しない場合は作成
                    logger.info(f"Parent branch {parent_branch} does not exist, creating it")

                    # まずデフォルトブランチに切り替え
                    switch_main_result = switch_to_branch(config.MAIN_BRANCH, pull_after_switch=True)
                    if not switch_main_result.success:
                        error_msg = f"Failed to switch to main branch {config.MAIN_BRANCH}: {switch_main_result.stderr}"
                        actions.append(error_msg)
                        logger.error(error_msg)
                        return actions

                    # 親issueのブランチを作成（自動的にリモートにプッシュされる）
                    create_parent_result = git_checkout_branch(parent_branch, create_new=True)
                    if create_parent_result.success:
                        actions.append(f"Created and published parent branch: {parent_branch}")
                        logger.info(f"Successfully created and published parent branch: {parent_branch}")

                        base_branch = parent_branch
                        pr_base_branch = parent_branch  # PRのマージ先も親issueブランチに設定
                    else:
                        logger.warning(f"Failed to create parent branch {parent_branch}: {create_parent_result.stderr}")
                        # 親ブランチの作成に失敗した場合はデフォルトブランチを使用
                        base_branch = config.MAIN_BRANCH

            # 作業用ブランチが既に存在するかチェック
            check_work_branch = cmd.run_command(["git", "rev-parse", "--verify", work_branch])

            if check_work_branch.returncode == 0:
                # 作業用ブランチが既に存在する場合は、それに切り替え
                logger.info(f"Work branch {work_branch} already exists, switching to it")
                checkout_existing_result = git_checkout_branch(work_branch)
                if checkout_existing_result.success:
                    actions.append(f"Switched to existing work branch: {work_branch}")
                    logger.info(f"Switched to existing work branch: {work_branch}")
                    target_branch = work_branch
                else:
                    error_msg = f"Failed to switch to existing work branch {work_branch}: {checkout_existing_result.stderr}"
                    actions.append(error_msg)
                    logger.error(error_msg)
                    return actions
            else:
                # 作業用ブランチが存在しない場合は、ベースブランチから新規作成
                logger.info(f"Work branch {work_branch} does not exist, creating from {base_branch}")

                # ベースブランチに切り替え
                switch_base_result = switch_to_branch(base_branch, pull_after_switch=True)
                if not switch_base_result.success:
                    error_msg = f"Failed to switch to base branch {base_branch}: {switch_base_result.stderr}"
                    actions.append(error_msg)
                    logger.error(error_msg)
                    return actions

                # 作業用ブランチを作成して切り替え
                checkout_new_result = git_checkout_branch(work_branch, create_new=True)
                if checkout_new_result.success:
                    actions.append(f"Created and switched to work branch: {work_branch} from {base_branch}")
                    logger.info(f"Successfully created work branch: {work_branch} from {base_branch}")
                    target_branch = work_branch
                else:
                    error_msg = f"Failed to create work branch {work_branch}: {checkout_new_result.stderr}"
                    actions.append(error_msg)
                    logger.error(error_msg)
                    return actions

        # Get commit log since branch creation
        with ProgressStage("Getting commit log"):
            commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

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
            commit_log=commit_log or "(No commit history)",
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
            push_progress_stage("Committing changes")
            commit_action = commit_and_push_changes(
                {"summary": f"Auto-Coder: Address issue #{issue_data['number']}"},
                repo_name=repo_name,
                issue_number=issue_data["number"],
            )
            actions.append(commit_action)

            # Create PR if this is a regular issue (not a PR)
            if "head_branch" not in issue_data and target_branch:
                push_progress_stage("Creating PR")
                pr_creation_result = _create_pr_for_issue(
                    repo_name=repo_name,
                    issue_data=issue_data,
                    work_branch=target_branch,
                    base_branch=pr_base_branch,
                    llm_response=response,
                    github_client=github_client,
                    dry_run=dry_run,
                )
                actions.append(pr_creation_result)
        else:
            actions.append("LLM CLI did not provide a clear response for issue analysis")

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
                    logger.info(f"Created feature issue #{issue.number}: {suggestion['title']}")
                except Exception as e:
                    logger.error(f"Failed to create feature issue: {e}")
            else:
                logger.info(f"[DRY RUN] Would create feature issue: {suggestion['title']}")
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
    github_client,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    target_type: str,
    number: int,
    jules_mode: bool = False,
) -> Dict[str, Any]:
    """Process a single issue or PR by number.

    target_type: 'issue' | 'pr' | 'auto'
    When 'auto', try PR first then fall back to issue.
    """
    with ProgressStage("Processing single PR/IS"):
        logger.info(f"Processing single target: type={target_type}, number={number} for {repo_name}")
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
                    from .pr_processor import _check_github_actions_status, _extract_linked_issues_from_pr_body, _take_pr_actions

                    pr_data = github_client.get_pr_details_by_number(repo_name, number)

                    # Extract branch name and related issues from PR data
                    branch_name = pr_data.get("head", {}).get("ref")
                    pr_body = pr_data.get("body", "")
                    related_issues = []
                    if pr_body:
                        # Extract linked issues from PR body
                        related_issues = _extract_linked_issues_from_pr_body(pr_body)

                    set_progress_item("PR", number, related_issues, branch_name)

                    # Check GitHub Actions status before processing
                    github_checks = _check_github_actions_status(repo_name, pr_data, config)
                    detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)

                    # If GitHub Actions are still in progress, switch to main and exit
                    if detailed_checks.has_in_progress:
                        logger.info(f"GitHub Actions checks are still in progress for PR #{number}, switching to main branch")

                        # Switch to main branch with pull
                        switch_result = switch_to_branch(config.MAIN_BRANCH, pull_after_switch=True)
                        if switch_result.success:
                            logger.info(f"Successfully switched to {config.MAIN_BRANCH} branch")
                            # Exit the program
                            logger.info(f"Exiting due to GitHub Actions in progress for PR #{number}")
                            sys.exit(0)
                        else:
                            logger.error(f"Failed to switch to {config.MAIN_BRANCH} branch: {switch_result.stderr}")
                            result["errors"].append(f"Failed to switch to main branch: {switch_result.stderr}")
                            return result

                    actions = _take_pr_actions(repo_name, pr_data, config, dry_run)
                    processed_pr = {
                        "pr_data": pr_data,
                        "actions_taken": actions,
                        "priority": "single",
                    }
                    result["prs_processed"].append(processed_pr)
                    newline_progress()
                except Exception as e:
                    msg = f"Failed to process PR #{number}: {e}"
                    logger.error(msg)
                    result["errors"].append(msg)
                    newline_progress()
            else:
                try:
                    set_progress_item("Issue", number)
                    push_progress_stage("Getting issue details")
                    issue_data = github_client.get_issue_details_by_number(repo_name, number)

                    # Add @auto-coder label now that we're actually going to process this issue
                    if not dry_run:
                        if not github_client.try_add_work_in_progress_label(repo_name, number):
                            msg = f"Skipping issue #{number} - @auto-coder label was just added by another instance"
                            logger.info(msg)
                            result["errors"].append(msg)
                            newline_progress()
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
                            push_progress_stage("Adding jules label")
                            current_labels = issue_data.get("labels", [])
                            if "jules" not in current_labels:
                                if not dry_run:
                                    github_client.add_labels_to_issue(repo_name, number, ["jules"])
                                    processed_issue["actions_taken"].append(f"Added 'jules' label to issue #{number}")
                                else:
                                    processed_issue["actions_taken"].append(f"[DRY RUN] Would add 'jules' label to issue #{number}")
                            else:
                                processed_issue["actions_taken"].append(f"Issue #{number} already has 'jules' label")
                        else:
                            push_progress_stage("Processing")
                            actions = _take_issue_actions(repo_name, issue_data, config, dry_run, github_client)
                            processed_issue["actions_taken"] = actions
                    finally:
                        # Remove @auto-coder label after processing
                        if not dry_run:
                            try:
                                github_client.remove_labels_from_issue(repo_name, number, ["@auto-coder"])
                            except Exception as e:
                                logger.warning(f"Failed to remove @auto-coder label from issue #{number}: {e}")
                        # Clear progress header after processing
                        newline_progress()

                    result["issues_processed"].append(processed_issue)
                except Exception as e:
                    msg = f"Failed to process issue #{number}: {e}"
                    logger.error(msg)
                    # Try to remove @auto-coder label on error
                    if not dry_run:
                        try:
                            github_client.remove_labels_from_issue(repo_name, number, ["@auto-coder"])
                        except Exception:
                            pass
                    result["errors"].append(msg)
                    newline_progress()
        except Exception as e:
            msg = f"Error in process_single: {e}"
            logger.error(msg)
            result["errors"].append(msg)

        # After processing, check if the single PR/issue is now closed
        # If so, switch to main branch, pull, and exit
        try:
            # Check if we processed exactly one item
            if not dry_run and (result["issues_processed"] or result["prs_processed"]):
                # Get the processed item
                processed_item = None
                item_number = None
                item_type = None

                if result["issues_processed"]:
                    processed_item = result["issues_processed"][0]
                    issue_data = processed_item.get("issue_data", {})
                    item_number = issue_data.get("number")
                    item_type = "issue"
                elif result["prs_processed"]:
                    processed_item = result["prs_processed"][0]
                    pr_data = processed_item.get("pr_data", {})
                    item_number = pr_data.get("number")
                    item_type = "pr"

                if item_number and item_type:
                    # Check the current state of the item
                    with ProgressStage("Checking final status"):
                        if item_type == "issue":
                            current_item = github_client.get_issue_details_by_number(repo_name, item_number)
                        else:
                            current_item = github_client.get_pr_details_by_number(repo_name, item_number)

                        if current_item.get("state") == "closed":
                            logger.info(f"{item_type.capitalize()} #{item_number} is closed, switching to main branch")

                            # Switch to main branch with pull
                            switch_result = switch_to_branch(config.MAIN_BRANCH, pull_after_switch=True)
                            if switch_result.success:
                                logger.info(f"Successfully switched to {config.MAIN_BRANCH} branch")
                                # Exit the program
                                logger.info(f"Exiting after closing {item_type} #{item_number}")
                                sys.exit(0)
                            else:
                                logger.error(f"Failed to switch to {config.MAIN_BRANCH} branch: {switch_result.stderr}")
        except Exception as e:
            logger.warning(f"Failed to check/handle closed item state: {e}")

        return result
