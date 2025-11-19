"""Configuration classes for Auto-Coder automation engine."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logger_config import get_logger

logger = get_logger(__name__)


@dataclass
class AutomationConfig:
    """Configuration constants for automation engine."""

    # File paths
    REPORTS_DIR: str = "reports"
    TEST_SCRIPT_PATH: str = "scripts/test.sh"

    # Label prompt mappings for label-based issue/PR processing
    # Maps labels to prompt template keys
    label_prompt_mappings: Dict[str, str] = field(
        default_factory=lambda: {
            # Breaking-change labels (highest priority)
            "breaking-change": "issue.breaking_change",
            "breaking": "issue.breaking_change",
            "api-change": "issue.breaking_change",
            "deprecation": "issue.breaking_change",
            "version-major": "issue.breaking_change",
            # Urgent labels
            "urgent": "issue.urgent",
            "high-priority": "issue.urgent",
            "critical": "issue.urgent",
            "blocker": "issue.urgent",
            # Bug labels
            "bug": "issue.bug",
            "bugfix": "issue.bug",
            "defect": "issue.bug",
            "error": "issue.bug",
            "fix": "issue.bug",
            # Enhancement labels
            "enhancement": "issue.enhancement",
            "feature": "issue.enhancement",
            "improvement": "issue.enhancement",
            "new-feature": "issue.enhancement",
            # Documentation labels
            "documentation": "issue.documentation",
            "docs": "issue.documentation",
            "doc": "issue.documentation",
        }
    )

    # Label priorities (highest priority first)
    # Breaking-change has highest priority, followed by urgent, bug, enhancement, documentation
    label_priorities: List[str] = field(
        default_factory=lambda: [
            # Breaking changes (highest priority)
            "breaking-change",
            "breaking",
            "api-change",
            "deprecation",
            "version-major",
            # Urgent issues
            "urgent",
            "high-priority",
            "critical",
            "blocker",
            # Bug fixes
            "bug",
            "bugfix",
            "defect",
            "error",
            "fix",
            # Enhancements
            "enhancement",
            "feature",
            "improvement",
            "new-feature",
            # Documentation
            "documentation",
            "docs",
            "doc",
        ]
    )

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

    # Skip dependency-bot PRs (Dependabot/Renovate/[bot]) for fix flows.
    # Green, mergeable dependency-bot PRs are still auto-merge candidates.
    IGNORE_DEPENDABOT_PRS: bool = False

    # Force clean workspace before PR checkout (git reset --hard + git clean -fd)
    # Default: False (do not force clean)
    FORCE_CLEAN_BEFORE_CHECKOUT: bool = False

    # Disable GitHub label operations (@auto-coder label)
    # Default: False (labels enabled)
    DISABLE_LABELS: bool = False

    # Enable check process by GitHub label (@auto-coder label)
    # Default: True (check enabled)
    CHECK_LABELS: bool = True

    # Check for and skip issues with unresolved dependencies
    # When an issue body contains "Depends on: #123" or similar patterns,
    # skip processing if dependency issue #123 is still open
    # Default: True (dependency checking enabled)
    CHECK_DEPENDENCIES: bool = True

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

    # PR label copying configuration
    # Enable or disable copying semantic labels from issues to PRs
    PR_LABEL_COPYING_ENABLED: bool = True

    # Maximum number of semantic labels to copy from issue to PR
    PR_LABEL_MAX_COUNT: int = 3

    # Priority order for semantic labels (highest to lowest priority)
    # Labels not in this list will be added after these (if space permits)
    PR_LABEL_PRIORITIES: List[str] = field(
        default_factory=lambda: [
            "urgent",
            "breaking-change",
            "bug",
            "enhancement",
            "documentation",
            "question",
        ]
    )

    # Custom label mappings (aliases) for semantic label detection
    # Maps primary label name to list of possible aliases
    PR_LABEL_MAPPINGS: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "breaking-change": [
                "breaking-change",
                "breaking change",
                "bc-breaking",
                "breaking",
                "incompatible",
                "api-change",
                "deprecation",
                "version-major",
                "major-change",
            ],
            "bug": [
                "bug",
                "bugfix",
                "fix",
                "error",
                "issue",
                "defect",
                "broken",
                "hotfix",
                "patch",
            ],
            "documentation": [
                "documentation",
                "docs",
                "doc",
                "readme",
                "guide",
            ],
            "enhancement": [
                "enhancement",
                "feature",
                "improvement",
                "feat",
                "request",
                "new-feature",
                "refactor",
                "optimization",
                "optimisation",
            ],
            "urgent": [
                "urgent",
                "high-priority",
                "critical",
                "asap",
                "priority-high",
                "blocker",
            ],
            "question": [
                "question",
                "help wanted",
                "support",
                "q&a",
            ],
        }
    )

    def validate_pr_label_config(self) -> None:
        """Validate PR label copying configuration.

        Raises:
            ValueError: If configuration values are invalid
        """
        if self.PR_LABEL_MAX_COUNT < 0 or self.PR_LABEL_MAX_COUNT > 10:
            raise ValueError("PR_LABEL_MAX_COUNT must be between 0 and 10")

        # Validate that all priority labels have mappings defined
        for label in self.PR_LABEL_PRIORITIES:
            if label not in self.PR_LABEL_MAPPINGS:
                logger.warning(f"PR_LABEL_PRIORITIES contains label '{label}' not in PR_LABEL_MAPPINGS")


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
    jules_mode: bool
    issues_processed: List[Dict[str, Any]] = field(default_factory=list)
    prs_processed: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
