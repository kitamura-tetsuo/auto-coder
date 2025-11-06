"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

from typing import Any, Union

from .logger_config import get_logger

logger = get_logger(__name__)


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


# Utility functions for centralized label management
def check_and_add_label(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    item_type: str = "issue",
    dry_run: bool = False,
    config: Any = None,
) -> bool:
    """Check if @auto-coder label exists and add it if not.

    This function provides unified label check/add logic across the codebase.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        item_type: Type of item ('issue' or 'pr')
        dry_run: If True, skip actual label operations
        config: AutomationConfig instance (optional)

    Returns:
        True if label was added or already exists (should skip), False if another instance will process
    """
    if dry_run:
        logger.info(f"[DRY RUN] Would check and add @auto-coder label to {item_type} #{item_number}")
        return True

    # Check if labels are disabled
    if hasattr(github_client, "disable_labels") and github_client.disable_labels:
        logger.debug(f"Labels disabled - skipping label check for {item_type} #{item_number}")
        return True

    if config and hasattr(config, "DISABLE_LABELS") and config.DISABLE_LABELS:
        logger.debug(f"Labels disabled via config - skipping label check for {item_type} #{item_number}")
        return True

    try:
        # Use GitHub client's try_add_work_in_progress_label method
        if hasattr(github_client, "try_add_work_in_progress_label"):
            result = github_client.try_add_work_in_progress_label(repo_name, item_number, label="@auto-coder")
            if result:
                logger.info(f"Added @auto-coder label to {item_type} #{item_number}")
            else:
                logger.info(f"Skipping {item_type} #{item_number} - @auto-coder label was just added by another instance")
            return bool(result)

        logger.error(f"GitHub client does not support try_add_work_in_progress_label")
        # On error, allow processing to continue
        return True

    except Exception as e:
        logger.error(f"Failed to add @auto-coder label to {item_type} #{item_number}: {e}")
        # On error, allow processing to continue
        return True


def remove_label(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    item_type: str = "issue",
    dry_run: bool = False,
    config: Any = None,
) -> None:
    """Remove @auto-coder label from issue/PR.

    This function provides unified label removal logic across the codebase.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        item_type: Type of item ('issue' or 'pr')
        dry_run: If True, skip actual label operations
        config: AutomationConfig instance (optional)
    """
    if dry_run:
        logger.info(f"[DRY RUN] Would remove @auto-coder label from {item_type} #{item_number}")
        return

    # Check if labels are disabled
    if hasattr(github_client, "disable_labels") and github_client.disable_labels:
        logger.debug(f"Labels disabled - skipping remove label for {item_type} #{item_number}")
        return

    if config and hasattr(config, "DISABLE_LABELS") and config.DISABLE_LABELS:
        logger.debug(f"Labels disabled via config - skipping remove label for {item_type} #{item_number}")
        return

    try:
        if hasattr(github_client, "remove_labels_from_issue"):
            github_client.remove_labels_from_issue(repo_name, item_number, ["@auto-coder"])
            logger.info(f"Removed @auto-coder label from {item_type} #{item_number}")
        else:
            logger.warning(f"GitHub client does not support label removal")
    except Exception as e:
        logger.warning(f"Failed to remove @auto-coder label from {item_type} #{item_number}: {e}")


def check_label_exists(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    label_name: str = "@auto-coder",
    item_type: str = "issue",
) -> bool:
    """Check if a specific label exists on an issue/PR.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        label_name: Name of the label to check
        item_type: Type of item ('issue' or 'pr')

    Returns:
        True if label exists, False otherwise
    """
    try:
        if hasattr(github_client, "has_label"):
            return bool(github_client.has_label(repo_name, item_number, label_name))

        # Fallback: get issue/PR details and check labels
        if item_type.lower() == "pr":
            if hasattr(github_client, "get_pr_details_by_number"):
                pr_data = github_client.get_pr_details_by_number(repo_name, item_number)
                labels = pr_data.get("labels", [])
            else:
                logger.warning(f"GitHub client does not support PR details retrieval")
                return False
        else:
            if hasattr(github_client, "get_issue_details_by_number"):
                issue_data = github_client.get_issue_details_by_number(repo_name, item_number)
                labels = issue_data.get("labels", [])
            else:
                logger.warning(f"GitHub client does not support issue details retrieval")
                return False

        return label_name in labels

    except Exception as e:
        logger.error(f"Failed to check label '{label_name}' on {item_type} #{item_number}: {e}")
        # On error, return False to allow processing to continue
        return False
