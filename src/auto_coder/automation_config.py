"""Configuration classes for Auto-Coder automation engine."""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AutomationConfig:
    """Configuration constants for automation engine."""

    # File paths
    REPORTS_DIR: str = "reports"
    TEST_SCRIPT_PATH: str = "scripts/test.sh"

    def get_reports_dir(self, repo_name: str) -> str:
        """Get the reports directory for a specific repository.

        Args:
            repo_name: Repository name in format 'owner/repo'

        Returns:
            Path to the reports directory: ~/.auto-coder/{repository}/
        """
        # リポジトリ名から安全なディレクトリ名を生成
        safe_repo_name = repo_name.replace("/", "_")

        # ホームディレクトリ配下の .auto-coder/{repository}/ を返す
        reports_path = Path.home() / ".auto-coder" / safe_repo_name
        return str(reports_path)

    # Limits
    MAX_PR_DIFF_SIZE: int = 2000
    MAX_PROMPT_SIZE: int = 1000
    MAX_RESPONSE_SIZE: int = 200
    max_issues_per_run: int = -1
    max_prs_per_run: int = -1
    # Default max attempts for fix loops
    # Note: tests expect strict default value of 30
    MAX_FIX_ATTEMPTS: int = 30

    # Git settings
    MAIN_BRANCH: str = "main"

    # Behavior flags
    # When GitHub Actions checks fail for a PR, skip merging the PR's
    # base branch into the PR branch before LLM fixes.
    # This changes previous behavior to default-skipping
    # to reduce noisy rebases.
    SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL: bool = True

    # Ignore Dependabot-authored PRs entirely when processing PRs
    IGNORE_DEPENDABOT_PRS: bool = False

    # Force clean workspace before PR checkout (git reset --hard + git clean -fd)
    # Default: False (do not force clean)
    FORCE_CLEAN_BEFORE_CHECKOUT: bool = False

    # Disable GitHub label operations (@auto-coder label)
    # Default: False (labels enabled)
    DISABLE_LABELS: bool = False

    # Search through commit history for GitHub Actions logs when latest commit doesn't trigger Actions
    # Default: True (search history enabled)
    SEARCH_GITHUB_ACTIONS_HISTORY: bool = True

    # GitHub CLI merge options
    MERGE_METHOD: str = "--squash"
    MERGE_AUTO: bool = True
