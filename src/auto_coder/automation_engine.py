"""
Main automation engine for Auto-Coder.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union, cast

from . import fix_to_pass_tests_runner as fix_to_pass_tests_runner_module
from .automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ProcessResult
from .backend_manager import LLMBackendManager, get_llm_backend_manager, run_llm_prompt
from .fix_to_pass_tests_runner import fix_to_pass_tests
from .gh_logger import get_gh_logger
from .git_branch import extract_number_from_branch, git_commit_with_retry
from .git_commit import git_push
from .git_info import get_current_branch
from .github_client import GitHubClient
from .issue_processor import create_feature_issues
from .jules_engine import check_and_resume_or_archive_sessions
from .label_manager import LabelManager
from .logger_config import get_logger
from .pr_processor import _create_pr_analysis_prompt as _engine_pr_prompt
from .pr_processor import _get_pr_diff as _pr_get_diff
from .pr_processor import _should_skip_waiting_for_jules, process_pull_request
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .test_result import TestResult
from .util.dependabot_timestamp import get_last_dependabot_run, set_last_dependabot_run
from .util.github_action import check_and_handle_closed_state, get_github_actions_logs_from_url
from .util.github_cache import get_github_cache
from .utils import CommandExecutor, log_action

logger = get_logger(__name__)


class AutomationEngine:
    """Main automation engine that orchestrates GitHub and LLM integration."""

    def __init__(
        self,
        github_client: GitHubClient,
        config: Optional[AutomationConfig] = None,
    ) -> None:
        """Initialize automation engine."""
        self.github = github_client
        self.config = config or AutomationConfig()
        self.cmd = CommandExecutor()

        # Note: Report directories are created per repository,
        # so we do not create one here (created in _save_report)

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
            repo = self.github.get_repository(repo_name)
            if item_type == "issue":
                issue = repo.get_issue(item_number)
                current_item = self.github.get_issue_details(issue)
            else:
                pr = repo.get_pull(item_number)
                current_item = self.github.get_pr_details(pr)

            # Check if item is closed
            if current_item.get("state") == "closed":
                logger.info(f"{item_type.capitalize()} #{item_number} is closed, switching to main branch and calling check_and_handle_closed_state")

                # Call check_and_handle_closed_state which will:
                # 1. Switch to main branch
                # 2. Exit the program
                return check_and_handle_closed_state(
                    repo_name,
                    item_type,
                    item_number,
                    self.config,
                    self.github,
                    current_item=current_item,
                )

            # Item is not closed, continue processing
            logger.debug(f"{item_type.capitalize()} #{item_number} is open, continuing processing")
            return True

        except Exception as e:
            logger.warning(f"Failed to check/handle closed branch state: {e}")
            # Continue processing on error
            return True

    def _get_candidates(self, repo_name: str, max_items: Optional[int] = None) -> List[Candidate]:
        """Collect PR/Issue candidates with priority.

        Priority definitions:
        - 7: Breaking-change PR (breaking-change, breaking, api-change, deprecation, version-major)
        - 4: Urgent + unmergeable PR (highest priority after breaking-change)
        - 3: Urgent + mergeable PR or urgent issue
        - 2: Unmergeable PR needing conflict resolution
        - 1: PR requiring fixes (GH Actions failed but mergeable)
        - 0: Regular issues

        Sort order:
        - Priority descending (7 -> 0)
        - Creation time ascending (oldest first)
        """
        from .pr_processor import _extract_linked_issues_from_pr_body, _is_dependabot_pr
        from .util.github_action import _check_github_actions_status

        candidates: List[Candidate] = []
        candidates_count = 0

        try:
            # Check if Dependabot PRs are in a cooldown period.
            # This check is performed once before iterating through PRs for efficiency.
            last_run = get_last_dependabot_run()
            is_in_cooldown = last_run and (datetime.now(timezone.utc) - last_run) < timedelta(minutes=5)

            dependabot_pr_processed_this_run = False
            # Collect PR candidates
            prs = self.github.get_open_pull_requests(repo_name)
            for pr in prs:
                pr_data = self.github.get_pr_details(pr)
                labels = pr_data.get("labels", []) or []

                pr_number = pr_data.get("number")
                if not isinstance(pr_number, int):
                    logger.warning(f"Skipping PR missing/invalid number in data: {pr_data}")
                    continue

                # Skip if another instance is processing (@auto-coder label present) using LabelManager check
                with LabelManager(
                    self.github,
                    repo_name,
                    pr_number,
                    item_type="pr",
                    skip_label_add=True,
                    check_labels=self.config.CHECK_LABELS,
                ) as should_process:
                    if not should_process:
                        continue

                # Calculate GitHub Actions status for the PR
                checks = _check_github_actions_status(repo_name, pr_data, self.config)

                # Skip PRs with running CI processes
                if checks.in_progress:
                    logger.debug(f"Skipping PR #{pr_number} - CI checks are in progress")
                    continue

                # Check if we should skip this PR because it's waiting for Jules
                if _should_skip_waiting_for_jules(self.github, repo_name, pr_data):
                    logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
                    continue

                mergeable = pr_data.get("mergeable", True)

                # Handle dependency-bot PRs based on configuration
                is_dependency_bot = _is_dependabot_pr(pr_data)
                if is_dependency_bot:
                    if is_in_cooldown:
                        logger.info(f"Skipping Dependabot PR #{pr_number} due to cooldown period.")
                        continue
                    if dependabot_pr_processed_this_run:
                        logger.info(f"Skipping Dependabot PR #{pr_number} as one has already been processed in this run.")
                        continue
                    # Get author information for logging
                    author = pr_data.get("author", "unknown")
                    logger.debug(f"PR #{pr_number}: Detected as dependency-bot PR (author: {author})")

                    # Log the current configuration values
                    logger.debug(f"PR #{pr_number}: IGNORE_DEPENDABOT_PRS={self.config.IGNORE_DEPENDABOT_PRS}, AUTO_MERGE_DEPENDABOT_PRS={self.config.AUTO_MERGE_DEPENDABOT_PRS}")

                    # Log GitHub Actions check results
                    logger.debug(f"PR #{pr_number}: checks.success={checks.success}, mergeable={mergeable}")

                    if self.config.IGNORE_DEPENDABOT_PRS:
                        # When IGNORE_DEPENDABOT_PRS is True: Skip ALL Dependabot PRs
                        logger.debug(f"Skipping dependency-bot PR #{pr_number} - IGNORE_DEPENDABOT_PRS is enabled")
                        continue
                    elif self.config.AUTO_MERGE_DEPENDABOT_PRS:
                        # When AUTO_MERGE_DEPENDABOT_PRS is True:
                        # - If passing & mergeable: Process (allow auto-merge)
                        # - Else: Skip (ignore)
                        if not (checks.success and bool(mergeable)):
                            logger.debug(f"Skipping dependency-bot PR #{pr_number} - checks not passing (success={checks.success}) or not mergeable (mergeable={mergeable})")
                            continue
                        else:
                            logger.info(f"Processing dependency-bot PR #{pr_number} - checks passed and mergeable")
                    # If both flags are False: Process all Dependabot PRs (try to fix failing)

                # Check if PR is created by Jules and waiting for Jules update
                if pr_data.get("author") == "jules":
                    try:
                        last_interaction_time = None
                        last_interaction_type = None

                        # Check reviews
                        for review in pr.get_reviews():
                            if review.user and review.user.login != "jules":
                                if last_interaction_time is None or review.submitted_at > last_interaction_time:
                                    last_interaction_time = review.submitted_at
                                    last_interaction_type = review.state

                        # Check comments
                        for comment in pr.get_issue_comments():
                            if comment.user and comment.user.login != "jules":
                                if last_interaction_time is None or comment.created_at > last_interaction_time:
                                    last_interaction_time = comment.created_at
                                    last_interaction_type = "COMMENT"

                        if last_interaction_time and last_interaction_type != "APPROVED":
                            # Check for Jules commits after interaction
                            commits = list(pr.get_commits())
                            jules_responded = False
                            if commits:
                                for commit in reversed(commits):
                                    # Check commit date
                                    commit_date = commit.commit.author.date
                                    if commit_date > last_interaction_time:
                                        if commit.author and commit.author.login == "jules":
                                            jules_responded = True
                                            break
                                    else:
                                        break

                            if not jules_responded:
                                logger.info(f"Skipping PR #{pr_number} - Waiting for Jules to update (requested at {last_interaction_time})")
                                continue

                    except Exception as e:
                        logger.warning(f"Failed to check Jules PR status for #{pr_number}: {e}")

                # Count only PRs that we will actually consider as candidates
                candidates_count += 1

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
                else:
                    pr_priority = 2  # Mergeable with successful checks (auto-merge candidate)

                if is_dependency_bot:
                    set_last_dependabot_run()
                    dependabot_pr_processed_this_run = True

                candidates.append(
                    Candidate(
                        type="pr",
                        data=pr_data,
                        priority=pr_priority,
                        branch_name=pr_data.get("head", {}).get("ref"),
                        related_issues=_extract_linked_issues_from_pr_body(pr_data.get("body", "")),
                    )
                )

            # Collect issues if:
            # - max_items is set and we haven't reached it yet (respect the requested limit), OR
            # - we have no PR candidates, OR
            # - we have few PR candidates and they're low priority (optimization to avoid usage limits)
            should_collect_issues = (max_items is not None and candidates_count < max_items) or candidates_count == 0 or (candidates_count < 3 and max([candidate.priority for candidate in candidates]) < 2)

            if should_collect_issues:
                # Collect issue candidates
                issues = self.github.get_open_issues(repo_name)
                for issue in issues:
                    issue_data = self.github.get_issue_details(issue)
                    labels = issue_data.get("labels", []) or []

                    # Skip if has sub-issues or linked PR
                    number = issue_data.get("number")
                    if not isinstance(number, int):
                        logger.warning(f"Issue data missing or invalid number: {issue_data}")
                        continue

                    # Skip if another instance is processing (@auto-coder label present) using LabelManager check
                    with LabelManager(
                        self.github,
                        repo_name,
                        number,
                        item_type="issue",
                        skip_label_add=True,
                        check_labels=self.config.CHECK_LABELS,
                    ) as should_process:
                        if not should_process:
                            continue

                    # Skip if issue has open sub-issues (it should be processed after sub-issues are resolved)
                    if self.github.get_open_sub_issues(repo_name, number):
                        continue

                    # Check for elder sibling dependency: if this issue is a sub-issue,
                    # ensure no elder sibling (sub-issue with lower number) is still open
                    parent_issue = self.github.get_parent_issue(repo_name, number)
                    if parent_issue is not None:
                        open_sub_issues = self.github.get_open_sub_issues(repo_name, parent_issue)
                        # Filter to only sibling sub-issues (exclude current issue)
                        elder_siblings = [s for s in open_sub_issues if s < number]
                        if elder_siblings:
                            logger.debug(f"Skipping issue #{number} - elder sibling(s) still open: {elder_siblings}")
                            continue

                    if self.github.has_linked_pr(repo_name, number):
                        continue

                    # Calculate priority
                    # Priority levels:
                    # - 7: Breaking-change (breaking-change, breaking, api-change, deprecation, version-major)
                    # - 3: Urgent
                    # - 0: Regular issues
                    issue_priority = 0
                    # Check for breaking-change related labels (highest priority)
                    breaking_change_labels = [
                        "breaking-change",
                        "breaking",
                        "api-change",
                        "deprecation",
                        "version-major",
                    ]
                    if any(label in labels for label in breaking_change_labels):
                        issue_priority = 7
                    elif "urgent" in labels:
                        issue_priority = 3

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

    def _has_open_sub_issues(self, repo_name: str, candidate: Candidate) -> bool:
        """Fail-safe helper to check if target issue has unresolved sub-issues.
        - candidate is expected to be an element from _get_candidates (type: issue)
        - Returns False on exception to avoid skip suppression
        """
        try:
            if candidate.type != "issue":
                return False
            issue_data = candidate.data or {}
            issue_number = candidate.issue_number or issue_data.get("number")
            if not issue_number:
                return False
            sub_issues = self.github.get_open_sub_issues(repo_name, issue_number)
            return bool(sub_issues)
        except Exception as e:
            logger.warning(f"Failed to check open sub-issues for issue #{candidate.issue_number or issue_data.get('number', 'N/A')}: {e}")
            return False

    def _process_single_candidate_unified(
        self,
        repo_name: str,
        candidate: Candidate,
        config: AutomationConfig,
        jules_mode: bool = False,
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

        try:
            # Get item number and type
            item_number = candidate.data.get("number")
            item_type = candidate.type

            # Ensure item_number is not None
            if item_number is None:
                raise ValueError(f"Item number is missing for {item_type} #{candidate.data.get('number', 'N/A')}")

            # Use LabelManager context manager to handle @auto-coder label automatically
            with LabelManager(self.github, repo_name, item_number, item_type=item_type, config=config, check_labels=config.CHECK_LABELS) as should_process:
                if not should_process:
                    result.actions = ["Skipped - another instance started processing (@auto-coder label added)"]
                    return result

                if item_type == "issue":
                    # Issue processing
                    if jules_mode:
                        # Use Jules mode for issue processing
                        from .issue_processor import _process_issue_jules_mode

                        result.actions = _process_issue_jules_mode(
                            repo_name,
                            candidate.data,
                            config,
                            self.github,
                            label_context=should_process,
                        )

                    else:
                        # Regular issue processing
                        result.actions = self._take_issue_actions(repo_name, candidate.data)
                    result.success = True
                elif item_type == "pr":
                    # PR processing
                    pr_result = process_pull_request(self.github, config, repo_name, candidate.data)
                    result.actions = pr_result.actions_taken
                    # Check if there was an error during processing
                    if pr_result.error:
                        result.error = pr_result.error
                    result.success = True

        except Exception as e:
            result.error = str(e)
            logger.error(f"Error processing {candidate.type} #{candidate.data.get('number', 'N/A')}: {e}")

        return result

    def _process_single_candidate(self, repo_name: str, candidate: Candidate) -> CandidateProcessingResult:
        """Process a single candidate (issue/PR).

        Args:
            repo_name: Repository name
            candidate: Target candidate to process

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

        results = {
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
                # Check and resume failed Jules sessions
                check_and_resume_or_archive_sessions()

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

                        # Process the candidate
                        result = self._process_single_candidate(repo_name, candidate)

                        # Track results
                        # Convert dataclass to dict for backward compatibility with existing code
                        result_dict = {
                            "type": result.type,
                            "number": result.number,
                            "title": result.title,
                            "success": result.success,
                            "actions": result.actions,
                            "error": result.error,
                        }
                        if candidate.type == "issue":
                            results["issues_processed"].append(result_dict)  # type: ignore
                        elif candidate.type == "pr":
                            results["prs_processed"].append(result_dict)  # type: ignore

                        batch_processed += 1
                        total_processed += 1

                        logger.info(f"Successfully processed {candidate.type} #{candidate.data.get('number', 'N/A')}")
                        break

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

    def process_single(self, repo_name: str, target_type: str, number: int, jules_mode: bool = False) -> Dict[str, Any]:
        """Process a single issue or PR by number.

        Args:
            repo_name: Repository name
            target_type: Type of target ('issue' or 'pr')
            number: Issue or PR number
            jules_mode: Whether to use Jules mode for processing (default: False)

        Returns:
            Dictionary with processing results
        """
        # Check if Jules mode should be used based on configuration
        # Check if Jules mode should be used based on configuration
        from .llm_backend_config import is_jules_mode_enabled

        # Both must be true for Jules mode to be enabled
        # jules_mode parameter is requested state, and is_jules_mode_enabled checks config
        jules_mode = jules_mode and is_jules_mode_enabled()
        from datetime import datetime

        with ProgressStage("Processing single PR/IS"):
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

            logger.info(f"Processing single target: type={target_type}, number={number} for {repo_name}")
            result = ProcessResult(
                repository=repo_name,
                timestamp=datetime.now().isoformat(),
            )

            try:
                # Create a Candidate from the single item
                candidate = self._create_candidate_from_single(repo_name, target_type, number)
                if not candidate:
                    msg = f"Failed to create candidate for {target_type} #{number}"
                    logger.error(msg)
                    result.errors.append(msg)
                    return {
                        "repository": result.repository,
                        "timestamp": result.timestamp,
                        "issues_processed": result.issues_processed,
                        "prs_processed": result.prs_processed,
                        "errors": result.errors,
                    }

                # Use unified processing function
                processing_result = self._process_single_candidate_unified(
                    repo_name,
                    candidate,
                    self.config,
                    jules_mode,
                )

                # Only add to processed list if there was no error
                if processing_result.error:
                    # Add error to errors list instead of processed list
                    error_msg = f"Error processing {candidate.type} #{candidate.data.get('number', 'N/A')}: {processing_result.error}"
                    result.errors.append(error_msg)
                else:
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

                            with ProgressStage("Checking final status"):
                                repo = self.github.get_repository(repo_name)
                                if item_type == "issue":
                                    issue = repo.get_issue(item_number)
                                    current_item = self.github.get_issue_details(issue)
                                else:
                                    pr = repo.get_pull(item_number)
                                    current_item = self.github.get_pr_details(pr)

                                # Check if item is closed and handle state
                                check_and_handle_closed_state(
                                    repo_name,
                                    item_type,
                                    item_number,
                                    self.config,
                                    self.github,  # type: ignore[arg-type]
                                    current_item=current_item,
                                )
                except Exception as e:
                    logger.warning(f"Failed to check/handle closed item state: {e}")

            except Exception as e:
                msg = f"Error in process_single: {e}"
                logger.error(msg)
                result.errors.append(msg)

        # Convert dataclass to dict for backward compatibility with existing code
        return {
            "repository": result.repository,
            "timestamp": result.timestamp,
            "issues_processed": result.issues_processed,
            "prs_processed": result.prs_processed,
            "errors": result.errors,
        }

    def create_feature_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Analyze repository and create feature enhancement issues."""
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
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
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

    def _take_issue_actions(self, repo_name: str, issue_data: Dict[str, Any]) -> List[str]:
        """Take actions on an issue using direct LLM CLI analysis and implementation."""
        from .issue_processor import _take_issue_actions as _take_issue_actions_func

        return _take_issue_actions_func(
            repo_name,
            issue_data,
            self.config,
            self.github,
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
            repo = self.github.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            pr_data = self.github.get_pr_details(pr)
            base_branch = pr_data.get("base_branch", "main")

            # Clean up any existing conflicts
            self.cmd.run_command(["git", "reset", "--hard", "HEAD"])
            self.cmd.run_command(["git", "clean", "-fd"])
            self.cmd.run_command(["git", "merge", "--abort"])

            # Checkout the PR branch
            gh_logger = get_gh_logger()
            gh_logger.execute_with_logging(
                ["gh", "pr", "checkout", str(pr_number)],
                repo=repo_name,
                capture_output=True,
            )

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
        import subprocess

        test_script_path = self.config.TEST_SCRIPT_PATH
        cwd = None  # Could be extended to use repo-specific working directory

        if not os.path.exists(test_script_path):
            return {
                "success": False,
                "errors": f"Test script not found: {test_script_path}",
                "return_code": -1,
            }

        try:
            result = subprocess.run(
                ["bash", test_script_path],
                capture_output=True,
                text=True,
                timeout=3600,
                cwd=cwd,
            )

            if result.returncode == 0:
                return {"success": True, "output": result.stdout}
            else:
                return {
                    "success": False,
                    "output": result.stdout,
                    "errors": result.stderr,
                    "return_code": result.returncode,
                }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "errors": "Test execution timed out after 1 hour",
                "return_code": -1,
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
                    fix_to_pass_tests_runner_module.extract_important_errors(test_result),
                )
            # Convert legacy dict payloads to TestResult for better extraction
            tr = fix_to_pass_tests_runner_module._to_test_result(test_result)
            return cast(str, fix_to_pass_tests_runner_module.extract_important_errors(tr))
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
            prompt = render_prompt(
                "pr.github_actions_fix_direct",
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
                # Use gh CLI to list workflow runs for this commit
                try:
                    gh_logger = get_gh_logger()
                    run_result = gh_logger.execute_with_logging(
                        ["gh", "run", "list", "--commit", commit_hash, "--limit", "1"],
                        repo=repo_name,
                        timeout=10,
                    )

                    # If no runs found for this commit, skip it
                    if run_result.returncode != 0 or "no runs found" in run_result.stdout.lower():
                        logger.debug(f"Commit {commit_hash[:8]}: No GitHub Actions runs found")
                        continue

                    # Check if there are any runs (success or failure)
                    # Parse the output to check for completed runs
                    run_lines = run_result.stdout.strip().split("\n")

                    actions_status = None
                    actions_url = ""

                    for run_line in run_lines:
                        if not run_line.strip() or run_line.startswith("STATUS") or run_line.startswith("WORKFLOW"):
                            continue

                        # Parse tab-separated format
                        if "\t" in run_line:
                            parts = run_line.split("\t")
                            if len(parts) >= 3:
                                status = parts[1].strip().lower()
                                url = parts[3] if len(parts) > 3 else ""

                                # Only include commits with completed runs (success or failure)
                                # Skip queued or in-progress runs
                                if status in [
                                    "success",
                                    "completed",
                                    "failure",
                                    "failed",
                                    "cancelled",
                                    "pass",
                                ]:
                                    actions_status = status
                                    actions_url = url
                                    break

                    # Only add commits that have completed Action runs
                    if actions_status and actions_status in [
                        "success",
                        "completed",
                        "failure",
                        "failed",
                        "cancelled",
                        "pass",
                    ]:
                        commits_with_actions.append(
                            {
                                "commit_hash": commit_hash,
                                "message": commit_message,
                                "actions_status": actions_status,
                                "actions_url": actions_url,
                            }
                        )
                        logger.info(f"Commit {commit_hash[:8]}: Found Actions run with status '{actions_status}'")

                except subprocess.TimeoutExpired:
                    logger.warning(f"Timeout checking Actions for commit {commit_hash[:8]}")
                    continue

            logger.info(f"Found {len(commits_with_actions)} commits with GitHub Actions")
            return commits_with_actions

        except Exception as e:
            logger.error(f"Error parsing commit history: {e}")
            return []

    def _create_candidate_from_single(self, repo_name: str, target_type: str, number: int) -> Optional[Candidate]:
        """Create a Candidate from a single issue or PR.

        Args:
            repo_name: Repository name
            target_type: Type of target ('issue' or 'pr')
            number: Issue or PR number

        Returns:
            Candidate or None if failed
        """
        from .pr_processor import _extract_linked_issues_from_pr_body

        try:
            # Handle 'auto' type
            if target_type == "auto":
                # Prefer PR to avoid mislabeling PR issues
                try:
                    repo = self.github.get_repository(repo_name)
                    pr = repo.get_pull(number)
                    pr_data = self.github.get_pr_details(pr)
                    target_type = "pr"
                except Exception:
                    target_type = "issue"

            if target_type == "pr":
                # Get PR data
                repo = self.github.get_repository(repo_name)
                pr = repo.get_pull(number)
                pr_data = self.github.get_pr_details(pr)
                branch_name = pr_data.get("head_branch")
                pr_body = pr_data.get("body", "")
                related_issues = []
                if pr_body:
                    related_issues = _extract_linked_issues_from_pr_body(pr_body)

                return Candidate(
                    type="pr",
                    data=pr_data,
                    priority=0,  # Single processing doesn't need priority
                    branch_name=branch_name,
                    related_issues=related_issues,
                )
            elif target_type == "issue":
                # Get issue data
                repo = self.github.get_repository(repo_name)
                issue = repo.get_issue(number)
                issue_data = self.github.get_issue_details(issue)

                return Candidate(
                    type="issue",
                    data=issue_data,
                    priority=0,  # Single processing doesn't need priority
                    issue_number=number,
                )
        except Exception as e:
            logger.error(f"Failed to create candidate for {target_type} #{number}: {e}")
            return None

        return None

    # Constants
    FLAG_SKIP_ANALYSIS = "[SKIP_LLM_ANALYSIS]"
