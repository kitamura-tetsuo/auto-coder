"""
Main automation engine for Auto-Coder.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union, cast

from auto_coder.backend_manager import LLMBackendManager, get_llm_backend_manager, run_llm_prompt
from auto_coder.github_client import GitHubClient
from auto_coder.prompt_loader import render_prompt
from auto_coder.util.github_action import get_github_actions_logs_from_url

from . import fix_to_pass_tests_runner as fix_to_pass_tests_runner_module
from .automation_config import AutomationConfig, Candidate, CandidateProcessingResult, ProcessResult
from .fix_to_pass_tests_runner import fix_to_pass_tests
from .gh_logger import get_gh_logger
from .git_branch import git_commit_with_retry
from .git_commit import git_push
from .issue_processor import create_feature_issues
from .logger_config import get_logger
from .pr_processor import _create_pr_analysis_prompt as _engine_pr_prompt
from .pr_processor import _get_pr_diff as _pr_get_diff
from .pr_processor import process_pull_request
from .progress_footer import ProgressStage
from .test_result import TestResult
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
            if not self.github.check_should_process_with_label_manager(repo_name, pr_number, item_type="pr"):
                continue

            # Calculate GitHub Actions status for the PR
            checks = _check_github_actions_status(repo_name, pr_data, self.config)

            # Skip PRs with running CI processes
            if checks.in_progress:
                logger.debug(f"Skipping PR #{pr_number} - CI checks are in progress")
                continue

            mergeable = pr_data.get("mergeable", True)

            # Handle dependency-bot PRs based on configuration
            is_dependency_bot = _is_dependabot_pr(pr_data)
            if self.config.IGNORE_DEPENDABOT_PRS and is_dependency_bot:
                # When IGNORE_DEPENDABOT_PRS is enabled, only process dependency-bot
                # PRs that are fully green and mergeable (auto-merge candidates).
                # Non-ready dependency-bot PRs are skipped to avoid expensive fix loops.
                if not (checks.success and bool(mergeable)):
                    logger.debug(f"Skipping dependency-bot PR #{pr_number} - IGNORE_DEPENDABOT_PRS enabled and PR " "is not green/mergeable")
                    continue

            # Count only PRs that we will actually consider as candidates
            candidates_count += 1

            # Calculate priority
            # Enhanced priority logic to distinguish unmergeable PRs
            if any(label in labels for label in ["breaking-change", "breaking", "api-change", "deprecation", "version-major"]):
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

            candidates.append(
                Candidate(
                    type="pr",
                    data=pr_data,
                    priority=pr_priority,
                    branch_name=pr_data.get("head", {}).get("ref"),
                    related_issues=_extract_linked_issues_from_pr_body(pr_data.get("body", "")),
                )
            )

        if candidates_count < 5:
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
                if not self.github.check_should_process_with_label_manager(repo_name, number, item_type="issue"):
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
            jules_mode: Whether Jules mode is enabled

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
            with LabelManager(self.github, repo_name, item_number, item_type=item_type, config=config) as should_process:
                if not should_process:
                    result.actions = ["Skipped - another instance started processing (@auto-coder label added)"]
                    return result

                if jules_mode and item_type == "issue":
                    # Jules mode: only add 'jules' label
                    from .issue_processor import _process_issue_jules_mode

                    jules_result = _process_issue_jules_mode(self.github, config, repo_name, candidate.data)
                    result.actions = jules_result.actions_taken
                    result.success = True
                elif item_type == "issue":
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

    def _process_single_candidate(self, repo_name: str, candidate: Candidate, jules_mode: bool = False) -> CandidateProcessingResult:
        """Process a single candidate (issue/PR).

        Args:
            repo_name: Repository name
            candidate: Target candidate to process
            jules_mode: Whether Jules mode is enabled

        Returns:
            Processing result
        """
        return self._process_single_candidate_unified(
            repo_name,
            candidate,
            self.config,
            jules_mode=jules_mode,
        )

    def run(self, repo_name: str, jules_mode: bool = False) -> Dict[str, Any]:
        """Run the main automation process."""
        logger.info(f"Starting automation for repository: {repo_name}")

        # Get LLM backend information
        llm_backend_info = self._get_llm_backend_info()

        results = {
            "repository": repo_name,
            "timestamp": datetime.now().isoformat(),
            "jules_mode": jules_mode,
            "llm_backend": llm_backend_info["backend"],
            "llm_model": llm_backend_info["model"],
            "issues_processed": [],
            "prs_processed": [],
            "errors": [],
        }

        try:
            # Get initial candidates
            total_processed = 0

            while True:
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
                        result = self._process_single_candidate(repo_name, candidate, jules_mode)

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
            jules_mode: Whether Jules mode is enabled

        Returns:
            Dictionary with processing results
        """
        from datetime import datetime

        with ProgressStage("Processing single PR/IS"):
            logger.info(f"Processing single target: type={target_type}, number={number} for {repo_name}")
            result = ProcessResult(
                repository=repo_name,
                timestamp=datetime.now().isoformat(),
                jules_mode=jules_mode,
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
                        "jules_mode": result.jules_mode,
                        "issues_processed": result.issues_processed,
                        "prs_processed": result.prs_processed,
                        "errors": result.errors,
                    }

                # Use unified processing function
                processing_result = self._process_single_candidate_unified(
                    repo_name,
                    candidate,
                    self.config,
                    jules_mode=jules_mode,
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
                                if item_type == "issue":
                                    current_item = self.github.get_issue_details_by_number(repo_name, item_number)
                                else:
                                    current_item = self.github.get_pr_details_by_number(repo_name, item_number)

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
            "jules_mode": result.jules_mode,
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

    def fix_to_pass_tests(self, max_attempts: Optional[int] = None, message_backend_manager: Optional[Any] = None) -> Dict[str, Any]:
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
                    max_attempts,
                )
            finally:
                fix_to_pass_tests_runner_module.run_local_tests = original_run
                fix_to_pass_tests_runner_module.apply_workspace_test_fix = original_apply

        return fix_to_pass_tests(self.config, max_attempts)

    def _get_llm_backend_info(self) -> Dict[str, Optional[str]]:
        """Get LLM backend and model information.

        Returns:
            Dictionary with 'backend' and 'model' keys.
        """
        try:
            # Try to get the manager using get_llm_backend_manager() to ensure proper initialization
            try:
                manager = get_llm_backend_manager()
                if manager is not None:
                    backend, model = manager.get_last_backend_and_model()
                    return {"backend": backend, "model": model}
            except (RuntimeError, AttributeError):
                # get_llm_backend_manager() fails if not initialized, fall back to direct access
                try:
                    manager = LLMBackendManager.get_llm_instance()
                    if manager is not None:
                        backend, model = manager.get_last_backend_and_model()
                        return {"backend": backend, "model": model}
                except (RuntimeError, AttributeError):
                    # Also try direct instance access
                    llm_instance: Optional[Any] = LLMBackendManager._instance
                    if llm_instance is not None:
                        backend, model = llm_instance.get_last_backend_and_model()
                        return {"backend": backend, "model": model}

            # Manager not initialized
            return {"backend": None, "model": None}
        except Exception as e:
            # Any other exceptions
            logger.debug(f"Error getting LLM backend info: {e}")
            return {"backend": None, "model": None}

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
            pr_data = self.github.get_pr_details_by_number(repo_name, pr_number)
            base_branch = pr_data.get("base", {}).get("ref", "main")

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
                ["git", "rev-list", "--count", f"HEAD..refs/remotes/origin/{base_branch}"],
                capture_output=True,
                text=True,
            )

            if rev_list_result.returncode == 0:
                commits_behind = int(rev_list_result.stdout.strip())
                if commits_behind > 0:
                    actions.append(f"{commits_behind} commits behind {base_branch}")

                    # Merge the base branch
                    merge_result = subprocess.run(
                        ["git", "merge", f"refs/remotes/origin/{base_branch}", "--no-edit"],
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
                return cast(str, fix_to_pass_tests_runner_module.extract_important_errors(test_result))
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
            error_summary = cast(str, fix_to_pass_tests_runner_module.extract_important_errors(test_result))
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
                    pr_data = self.github.get_pr_details_by_number(repo_name, number)
                    target_type = "pr"
                except Exception:
                    target_type = "issue"

            if target_type == "pr":
                # Get PR data
                pr_data = self.github.get_pr_details_by_number(repo_name, number)
                branch_name = pr_data.get("head", {}).get("ref")
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
                issue_data = self.github.get_issue_details_by_number(repo_name, number)

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
