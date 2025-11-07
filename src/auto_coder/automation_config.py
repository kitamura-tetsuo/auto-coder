"""Configuration classes for Auto-Coder automation engine."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        # Generate safe directory name from repository name
        safe_repo_name = repo_name.replace("/", "_")

        # Return .auto-coder/{repository}/ under home directory
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

    # Check for and skip issues with unresolved dependencies
    # When an issue body contains "Depends on: #123" or similar patterns,
    # skip processing if dependency issue #123 is still open
    # Default: True (dependency checking enabled)
    CHECK_DEPENDENCIES: bool = True

    # Run automation in dry-run mode without making changes
    # Default: False (make actual changes)
    DRY_RUN: bool = False

    # Search through commit history for GitHub Actions logs when latest commit doesn't trigger Actions
    # Default: True (search history enabled)
    SEARCH_GITHUB_ACTIONS_HISTORY: bool = True

    # Enable historical fallback for GitHub Actions status checks
    # When current PR checks fail or are not available, search through recent runs to determine status
    # Default: True (fallback enabled)
    ENABLE_ACTIONS_HISTORY_FALLBACK: bool = True

    # GitHub CLI merge options
    MERGE_METHOD: str = "--squash"
    MERGE_AUTO: bool = True


@dataclass
class Candidate:
    """Represents a candidate (issue or PR) for processing.

    Provides type-safe structure for candidate data with required and optional fields.
    """

    type: str  # "issue" or "pr"
    data: Dict[str, Any]  # issue_data or pr_data
    priority: int
    branch_name: Optional[str] = None
    related_issues: List[int] = field(default_factory=list)
    issue_number: Optional[int] = None


@dataclass
class CandidateProcessingResult:
    """Result of processing a single candidate (issue or PR).

    Provides type-safe structure for processing results.
    """

    type: str  # "issue" or "pr"
    number: Optional[int] = None
    title: Optional[str] = None
    success: bool = False
    actions: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ProcessedPRResult:
    """Result of processing a pull request.

    Provides type-safe structure for PR processing results.
    """

    pr_data: Dict[str, Any]
    actions_taken: List[str] = field(default_factory=list)
    priority: Optional[str] = None  # "merge", "fix", "single", "error"
    analysis: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class ProcessedIssueResult:
    """Result of processing an issue.

    Provides type-safe structure for issue processing results.
    """

    issue_data: Dict[str, Any]
    actions_taken: List[str] = field(default_factory=list)
    error: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = None
    solution: Optional[Dict[str, Any]] = None


@dataclass
class ProcessResult:
    """Result of processing a single issue or PR.

    Provides type-safe structure for process_single return values.
    """

    repository: str
    timestamp: str
    dry_run: bool
    jules_mode: bool
    issues_processed: List[Dict[str, Any]] = field(default_factory=list)
    prs_processed: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
