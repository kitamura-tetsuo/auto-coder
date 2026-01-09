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

    # PR-specific label prompt mappings for label-based PR processing
    # Maps labels to PR prompt template keys
    pr_label_prompt_mappings: Dict[str, str] = field(
        default_factory=lambda: {
            "breaking-change": "pr.breaking_change",
            "breaking": "pr.breaking_change",
            "api-change": "pr.breaking_change",
            "deprecation": "pr.breaking_change",
            "version-major": "pr.breaking_change",
            "urgent": "pr.urgent",
            "high-priority": "pr.urgent",
            "critical": "pr.urgent",
            "blocker": "pr.urgent",
            "bug": "pr.bug",
            "bugfix": "pr.bug",
            "defect": "pr.bug",
            "error": "pr.bug",
            "fix": "pr.bug",
            "enhancement": "pr.enhancement",
            "feature": "pr.enhancement",
            "improvement": "pr.enhancement",
            "new-feature": "pr.enhancement",
            "documentation": "pr.documentation",
            "docs": "pr.documentation",
            "doc": "pr.documentation",
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

    def __init__(
        self,
        env_override: bool = True,
        custom_label_mappings: Optional[Dict[str, str]] = None,
        custom_priorities: Optional[List[str]] = None,
        replace_mappings: bool = False,
    ):
        """Initialize AutomationConfig with optional environment variable overrides.

        Args:
            env_override: If True, read from environment variables
            custom_label_mappings: Optional custom label prompt mappings
            custom_priorities: Optional custom label priorities
            replace_mappings: If True, custom_label_mappings will replace defaults entirely.
                If False (default), custom_label_mappings will merge with defaults.
        """
        # Store init parameters for later use
        self._env_override = env_override
        self._custom_label_mappings = custom_label_mappings
        self._custom_priorities = custom_priorities
        self._replace_mappings = replace_mappings

        # Set default values
        object.__setattr__(self, "REPORTS_DIR", "reports")
        object.__setattr__(self, "TEST_SCRIPT_PATH", "scripts/test.sh")
        object.__setattr__(self, "MAX_PR_DIFF_SIZE", 2000)
        object.__setattr__(self, "MAX_PROMPT_SIZE", 2000)
        object.__setattr__(self, "MAX_RESPONSE_SIZE", 200)
        object.__setattr__(self, "max_issues_per_run", -1)
        object.__setattr__(self, "max_prs_per_run", -1)
        object.__setattr__(self, "MAX_FIX_ATTEMPTS", 30)
        object.__setattr__(self, "MAIN_BRANCH", "main")
        object.__setattr__(self, "SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL", True)
        object.__setattr__(self, "ENABLE_MERGEABILITY_REMEDIATION", True)
        object.__setattr__(self, "IGNORE_DEPENDABOT_PRS", False)
        object.__setattr__(self, "FORCE_CLEAN_BEFORE_CHECKOUT", False)
        object.__setattr__(self, "DISABLE_LABELS", False)
        object.__setattr__(self, "CHECK_LABELS", True)
        object.__setattr__(self, "CHECK_DEPENDENCIES", True)
        object.__setattr__(self, "SEARCH_GITHUB_ACTIONS_HISTORY", True)
        object.__setattr__(self, "ENABLE_ACTIONS_HISTORY_FALLBACK", True)
        object.__setattr__(self, "ISOLATE_SINGLE_TEST_ON_FAILURE", False)
        object.__setattr__(self, "MERGE_METHOD", "--squash")
        object.__setattr__(self, "MERGE_AUTO", True)
        object.__setattr__(self, "AUTO_MERGE_DEPENDABOT_PRS", True)
        object.__setattr__(self, "PR_LABEL_COPYING_ENABLED", True)
        object.__setattr__(self, "PR_LABEL_MAX_COUNT", 3)
        object.__setattr__(
            self,
            "PR_LABEL_PRIORITIES",
            [
                "urgent",
                "breaking-change",
                "bug",
                "enhancement",
                "documentation",
                "question",
            ],
        )
        object.__setattr__(
            self,
            "PR_LABEL_MAPPINGS",
            {
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
            },
        )

        # Initialize label prompt mappings
        # These map to both issue.* and pr.* templates for label-based prompt selection
        object.__setattr__(
            self,
            "label_prompt_mappings",
            {
                "breaking-change": "issue.breaking_change",
                "breaking": "issue.breaking_change",
                "api-change": "issue.breaking_change",
                "deprecation": "issue.breaking_change",
                "version-major": "issue.breaking_change",
                "urgent": "issue.urgent",
                "high-priority": "issue.urgent",
                "critical": "issue.urgent",
                "blocker": "issue.urgent",
                "bug": "issue.bug",
                "bugfix": "issue.bug",
                "defect": "issue.bug",
                "error": "issue.bug",
                "fix": "issue.bug",
                "enhancement": "issue.enhancement",
                "feature": "issue.enhancement",
                "improvement": "issue.enhancement",
                "new-feature": "issue.enhancement",
                "documentation": "issue.documentation",
                "docs": "issue.documentation",
                "doc": "issue.documentation",
            },
        )

        # Initialize PR-specific label prompt mappings
        object.__setattr__(
            self,
            "pr_label_prompt_mappings",
            {
                "breaking-change": "pr.breaking_change",
                "breaking": "pr.breaking_change",
                "api-change": "pr.breaking_change",
                "deprecation": "pr.breaking_change",
                "version-major": "pr.breaking_change",
                "urgent": "pr.urgent",
                "high-priority": "pr.urgent",
                "critical": "pr.urgent",
                "blocker": "pr.urgent",
                "bug": "pr.bug",
                "bugfix": "pr.bug",
                "defect": "pr.bug",
                "error": "pr.bug",
                "fix": "pr.bug",
                "enhancement": "pr.enhancement",
                "feature": "pr.enhancement",
                "improvement": "pr.enhancement",
                "new-feature": "pr.enhancement",
                "documentation": "pr.documentation",
                "docs": "pr.documentation",
                "doc": "pr.documentation",
            },
        )

        # Initialize label priorities
        object.__setattr__(
            self,
            "label_priorities",
            [
                "breaking-change",
                "breaking",
                "api-change",
                "deprecation",
                "version-major",
                "urgent",
                "high-priority",
                "critical",
                "blocker",
                "bug",
                "bugfix",
                "defect",
                "error",
                "fix",
                "enhancement",
                "feature",
                "improvement",
                "new-feature",
                "documentation",
                "docs",
                "doc",
            ],
        )

        # Apply custom overrides if provided
        if custom_label_mappings:
            if replace_mappings:
                # Replace all defaults with custom mappings
                object.__setattr__(self, "label_prompt_mappings", custom_label_mappings)
            else:
                # Merge custom mappings with defaults
                self._merge_label_mappings(custom_label_mappings)

        if custom_priorities:
            object.__setattr__(self, "label_priorities", custom_priorities)

        # Apply environment variable overrides if enabled (can override both defaults and custom)
        if env_override:
            self._apply_env_overrides()

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides for label configurations."""
        # Read label prompt mappings from environment variable
        mappings_json = os.environ.get("AUTO_CODER_LABEL_PROMPT_MAPPINGS")
        if mappings_json:
            try:
                env_mappings = json.loads(mappings_json)
                if isinstance(env_mappings, dict):
                    self._merge_label_mappings(env_mappings)
                    logger.info(f"Loaded {len(env_mappings)} label prompt mappings from environment")
                else:
                    logger.warning("AUTO_CODER_LABEL_PROMPT_MAPPINGS must be a JSON object (dict)")
            except json.JSONDecodeError as exc:
                logger.error(f"Failed to parse AUTO_CODER_LABEL_PROMPT_MAPPINGS: {exc}")

        # Read label priorities from environment variable
        priorities_json = os.environ.get("AUTO_CODER_LABEL_PRIORITIES")
        if priorities_json:
            try:
                env_priorities = json.loads(priorities_json)
                if isinstance(env_priorities, list):
                    object.__setattr__(self, "label_priorities", env_priorities)
                    logger.info(f"Loaded {len(env_priorities)} label priorities from environment")
                else:
                    logger.warning("AUTO_CODER_LABEL_PRIORITIES must be a JSON array (list)")
            except json.JSONDecodeError as exc:
                logger.error(f"Failed to parse AUTO_CODER_LABEL_PRIORITIES: {exc}")

        # Read PR label mappings from environment variable
        pr_mappings_json = os.environ.get("AUTO_CODER_PR_LABEL_MAPPINGS")
        if pr_mappings_json:
            try:
                env_pr_mappings = json.loads(pr_mappings_json)
                if isinstance(env_pr_mappings, dict):
                    # Merge with existing PR label mappings
                    current_pr_mappings = dict(self.PR_LABEL_MAPPINGS)
                    current_pr_mappings.update(env_pr_mappings)
                    object.__setattr__(self, "PR_LABEL_MAPPINGS", current_pr_mappings)
                    logger.info(f"Loaded {len(env_pr_mappings)} PR label mappings from environment")
                else:
                    logger.warning("AUTO_CODER_PR_LABEL_MAPPINGS must be a JSON object (dict)")
            except json.JSONDecodeError as exc:
                logger.error(f"Failed to parse AUTO_CODER_PR_LABEL_MAPPINGS: {exc}")

        # Read PR label priorities from environment variable
        pr_priorities_json = os.environ.get("AUTO_CODER_PR_LABEL_PRIORITIES")
        if pr_priorities_json:
            try:
                env_pr_priorities = json.loads(pr_priorities_json)
                if isinstance(env_pr_priorities, list):
                    object.__setattr__(self, "PR_LABEL_PRIORITIES", env_pr_priorities)
                    logger.info(f"Loaded {len(env_pr_priorities)} PR label priorities from environment")
                else:
                    logger.warning("AUTO_CODER_PR_LABEL_PRIORITIES must be a JSON array (list)")
            except json.JSONDecodeError as exc:
                logger.error(f"Failed to parse AUTO_CODER_PR_LABEL_PRIORITIES: {exc}")

    def _merge_label_mappings(self, new_mappings: Dict[str, str]) -> None:
        """Merge new label mappings with existing ones.

        Args:
            new_mappings: Dictionary of new label-to-prompt mappings
        """
        current_mappings = dict(self.label_prompt_mappings)
        current_mappings.update(new_mappings)
        object.__setattr__(self, "label_prompt_mappings", current_mappings)

    def validate_label_config(self) -> None:
        """Validate label prompt configuration.

        Raises:
            ValueError: If configuration values are invalid
        """
        if not self.label_priorities:
            raise ValueError("label_priorities cannot be empty")

        if not self.label_prompt_mappings:
            raise ValueError("label_prompt_mappings cannot be empty")

        # Validate that all priorities reference valid mappings
        for priority_label in self.label_priorities:
            if priority_label not in self.label_prompt_mappings:
                logger.warning(f"label_priorities contains '{priority_label}' not in label_prompt_mappings")

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
    MAX_PROMPT_SIZE: int = 2000
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
    # Enable mergeability remediation flow.
    # Default: True (automatically handle non-mergeable PRs)
    ENABLE_MERGEABILITY_REMEDIATION: bool = True

    # Skip all dependency-bot PRs (Dependabot/Renovate/[bot]), including ready ones.
    # This provides a way to completely ignore dependency updates.
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

    # Isolate single test on failure
    # When multiple tests fail, extract and re-run only the first failed test in isolation
    # Default: False (run full test suite without isolation)
    ISOLATE_SINGLE_TEST_ON_FAILURE: bool = False

    # GitHub CLI merge options
    MERGE_METHOD: str = "--squash"
    MERGE_AUTO: bool = True

    # Enable/disable auto-merge feature
    # Default: True (auto-merge enabled)
    AUTO_MERGE: bool = True

    # Enable/disable auto-merge for Dependabot PRs
    # When IGNORE_DEPENDABOT_PRS is False and this is True:
    # - Only process dependency-bot PRs with passing tests and mergeable state
    # - These PRs will be auto-merged automatically
    # - Non-ready PRs are skipped (do nothing)
    # When this is False:
    # - Process all dependency-bot PRs, attempting to fix failing ones
    # Default: True (auto-merge for ready Dependabot PRs enabled)
    AUTO_MERGE_DEPENDABOT_PRS: bool = True

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
    issues_processed: List[Dict[str, Any]] = field(default_factory=list)
    prs_processed: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
