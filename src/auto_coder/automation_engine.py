"""
Main automation engine for Auto-Coder.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import fix_to_pass_tests_runner as fix_to_pass_tests_runner_module
from .automation_config import AutomationConfig
from .fix_to_pass_tests_runner import fix_to_pass_tests
from .issue_processor import create_feature_issues, process_issues, process_single
from .logger_config import get_logger
from .pr_processor import _apply_pr_actions_directly as _pr_apply_actions
from .pr_processor import _create_pr_analysis_prompt as _engine_pr_prompt
from .pr_processor import _get_pr_diff as _pr_get_diff
from .pr_processor import get_github_actions_logs_from_url
from .pr_processor import process_pull_requests
from .utils import log_action

logger = get_logger(__name__)


class AutomationEngine:
    """Main automation engine that orchestrates GitHub and LLM integration."""

    def __init__(
        self,
        github_client,
        llm_client=None,
        dry_run: bool = False,
        config: Optional[AutomationConfig] = None,
    ):
        """Initialize automation engine."""
        self.github = github_client
        self.llm = llm_client
        self.dry_run = dry_run
        self.config = config or AutomationConfig()

        # Create reports directory if it doesn't exist
        os.makedirs(self.config.REPORTS_DIR, exist_ok=True)

    def run(self, repo_name: str, jules_mode: bool = False) -> Dict[str, Any]:
        """Run the main automation process."""
        logger.info(f"Starting automation for repository: {repo_name}")

        results = {
            "repository": repo_name,
            "timestamp": datetime.now().isoformat(),
            "dry_run": self.dry_run,
            "jules_mode": jules_mode,
            "issues_processed": [],
            "prs_processed": [],
            "errors": [],
        }

        try:
            # Process issues (with jules_mode parameter)
            issues_result = process_issues(
                self.github, self.config, self.dry_run, repo_name, jules_mode, self.llm
            )
            results["issues_processed"] = issues_result

            # Process pull requests (always use normal processing)
            prs_result = process_pull_requests(
                self.github, self.config, self.dry_run, repo_name, self.llm
            )
            results["prs_processed"] = prs_result

            # Save results report
            report_name = f"{'jules_' if jules_mode else ''}automation_report_{repo_name.replace('/', '_')}"
            self._save_report(results, report_name)

            logger.info(f"Automation completed for {repo_name}")
            return results

        except Exception as e:
            error_msg = f"Automation failed for {repo_name}: {e}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            return results

    def process_single(
        self, repo_name: str, target_type: str, number: int, jules_mode: bool = False
    ) -> Dict[str, Any]:
        """Process a single issue or PR by number."""
        return process_single(
            self.github,
            self.config,
            self.dry_run,
            repo_name,
            target_type,
            number,
            jules_mode,
            self.llm,
        )

    def create_feature_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Analyze repository and create feature enhancement issues."""
        return create_feature_issues(
            self.github, self.config, self.dry_run, repo_name, self.llm
        )

    def fix_to_pass_tests(self, max_attempts: Optional[int] = None) -> Dict[str, Any]:
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
                    fix_to_pass_tests_runner_module.apply_workspace_test_fix = (
                        apply_override
                    )
                return fix_to_pass_tests(
                    self.config, self.dry_run, max_attempts, self.llm
                )
            finally:
                fix_to_pass_tests_runner_module.run_local_tests = original_run
                fix_to_pass_tests_runner_module.apply_workspace_test_fix = (
                    original_apply
                )

        return fix_to_pass_tests(self.config, self.dry_run, max_attempts, self.llm)

    def _save_report(self, data: Dict[str, Any], filename: str) -> None:
        """Save report to file."""
        try:
            # レポートディレクトリが存在しない場合は作成
            os.makedirs(self.config.REPORTS_DIR, exist_ok=True)

            filepath = os.path.join(
                self.config.REPORTS_DIR,
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

    def get_github_actions_logs_from_url(self, url: str) -> str:
        """GitHub Actions のジョブURLから、該当 job のログを直接取得してエラーブロックを抽出する。"""
        return get_github_actions_logs_from_url(url)

    def _get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Get PR diff for analysis."""
        return _pr_get_diff(repo_name, pr_number, self.config)

    def _apply_pr_actions_directly(
        self, repo_name: str, pr_data: Dict[str, Any]
    ) -> List[str]:
        """Ask LLM CLI to apply PR fixes directly; avoid posting PR comments."""
        return _pr_apply_actions(repo_name, pr_data, self.config, self.dry_run, self.llm)
