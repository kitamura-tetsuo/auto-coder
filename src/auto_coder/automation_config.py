"""Configuration classes for Auto-Coder automation engine."""

from dataclasses import dataclass


@dataclass
class AutomationConfig:
    """Configuration constants for automation engine."""

    # File paths
    REPORTS_DIR: str = "reports"
    TEST_SCRIPT_PATH: str = "scripts/test.sh"

    # Limits
    MAX_PR_DIFF_SIZE: int = 2000
    MAX_PROMPT_SIZE: int = 1000
    MAX_RESPONSE_SIZE: int = 200
    # Default max attempts for fix loops
    # Note: tests expect strict default value of 3
    MAX_FIX_ATTEMPTS: int = 3

    # Git settings
    MAIN_BRANCH: str = "main"

    # Behavior flags
    # When GitHub Actions checks fail for a PR, skip merging the PR's base branch into the PR branch before LLM fixes.
    # This changes previous behavior to default-skipping to reduce noisy rebases.
    SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL: bool = True

    # Ignore Dependabot-authored PRs entirely when processing PRs
    IGNORE_DEPENDABOT_PRS: bool = False

    # GitHub CLI merge options
    MERGE_METHOD: str = "--squash"
    MERGE_AUTO: bool = True
