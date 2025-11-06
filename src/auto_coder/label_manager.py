"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import time
import warnings
from typing import Any, Union

from .logger_config import get_logger

logger = get_logger(__name__)


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


class LabelManager:
    """Context manager for @auto-coder label operations.

    This class provides a unified interface for managing @auto-coder labels
    with proper resource cleanup, retry logic, and error handling.

    Usage:
        with LabelManager(github_client, repo_name, issue_number, "issue", config) as lm:
            # Process issue - label is guaranteed to be present
            process_issue()
        # Label is automatically removed when exiting the context

    Attributes:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        item_type: Type of item ('issue' or 'pr')
        dry_run: If True, skip actual label operations
        config: AutomationConfig instance (optional)
        label_name: Name of the label to manage (default: '@auto-coder')
        max_retries: Maximum number of retry attempts for API failures
        retry_delay: Delay in seconds between retries
    """

    def __init__(
        self,
        github_client: Any,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        dry_run: bool = False,
        config: Any = None,
        label_name: str = "@auto-coder",
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ):
        """Initialize LabelManager.

        Args:
            github_client: GitHub client instance
            repo_name: Repository name (owner/repo)
            item_number: Issue or PR number
            item_type: Type of item ('issue' or 'pr')
            dry_run: If True, skip actual label operations
            config: AutomationConfig instance (optional)
            label_name: Name of the label to manage (default: '@auto-coder')
            max_retries: Maximum number of retry attempts for API failures
            retry_delay: Delay in seconds between retries
        """
        self.github_client = github_client
        self.repo_name = repo_name
        self.item_number = item_number
        self.item_type = item_type
        self.dry_run = dry_run
        self.config = config
        self.label_name = label_name
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # State tracking
        self._label_added = False
        self._should_cleanup = False

        # Check if labels are disabled
        self._labels_disabled = self._check_labels_disabled()

    def _check_labels_disabled(self) -> bool:
        """Check if label operations are disabled.

        Returns:
            True if labels are disabled, False otherwise
        """
        if hasattr(self.github_client, "disable_labels") and self.github_client.disable_labels:
            return True

        if self.config and hasattr(self.config, "DISABLE_LABELS") and self.config.DISABLE_LABELS:
            return True

        return False

    def _retry_operation(self, operation, *args, **kwargs):
        """Retry an operation with exponential backoff.

        Args:
            operation: Callable to retry
            *args: Positional arguments for operation
            **kwargs: Keyword arguments for operation

        Returns:
            Result of the operation

        Raises:
            LabelOperationError: If all retries fail
        """
        last_exception = None
        for attempt in range(self.max_retries):
            try:
                return operation(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    wait_time = self.retry_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(
                        f"Label operation failed (attempt {attempt + 1}/{self.max_retries}): {e}. "
                        f"Retrying in {wait_time:.2f}s..."
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"Label operation failed after {self.max_retries} attempts: {e}")

        if last_exception:
            raise LabelOperationError(f"Operation failed after {self.max_retries} attempts: {last_exception}") from last_exception

    def check_and_add_label(self) -> bool:
        """Check if label exists and add it if not.

        Returns:
            True if label was added or should skip (another instance processing), False if failed to add
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would check and add '{self.label_name}' label to {self.item_type} #{self.item_number}")
            return True

        if self._labels_disabled:
            logger.debug(f"Labels disabled - skipping label check for {self.item_type} #{self.item_number}")
            return True

        try:
            # Use GitHub client's try_add_work_in_progress_label method
            if hasattr(self.github_client, "try_add_work_in_progress_label"):
                result = self._retry_operation(
                    self.github_client.try_add_work_in_progress_label,
                    self.repo_name,
                    self.item_number,
                    label=self.label_name,
                )
                if result:
                    logger.info(f"Added '{self.label_name}' label to {self.item_type} #{self.item_number}")
                    self._label_added = True
                    self._should_cleanup = True
                else:
                    logger.info(
                        f"Skipping {self.item_type} #{self.item_number} - '{self.label_name}' label was just added by another instance"
                    )
                return bool(result)

            logger.error(f"GitHub client does not support try_add_work_in_progress_label")
            # On error, allow processing to continue
            return True

        except Exception as e:
            logger.error(f"Failed to add '{self.label_name}' label to {self.item_type} #{self.item_number}: {e}")
            # On error, allow processing to continue
            return True

    def remove_label(self) -> bool:
        """Remove label from issue/PR.

        Returns:
            True if label was successfully removed or not needed, False on error
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would remove '{self.label_name}' label from {self.item_type} #{self.item_number}")
            return True

        if self._labels_disabled:
            logger.debug(f"Labels disabled - skipping remove label for {self.item_type} #{self.item_number}")
            return True

        try:
            if hasattr(self.github_client, "remove_labels_from_issue"):
                self._retry_operation(
                    self.github_client.remove_labels_from_issue,
                    self.repo_name,
                    self.item_number,
                    [self.label_name],
                )
                logger.info(f"Removed '{self.label_name}' label from {self.item_type} #{self.item_number}")
                return True
            else:
                logger.warning(f"GitHub client does not support label removal")
                return False
        except Exception as e:
            logger.warning(f"Failed to remove '{self.label_name}' label from {self.item_type} #{self.item_number}: {e}")
            return False

    def verify_label_exists(self) -> bool:
        """Check if the label exists on the issue/PR.

        Returns:
            True if label exists, False otherwise
        """
        try:
            if hasattr(self.github_client, "has_label"):
                return bool(
                    self._retry_operation(
                        self.github_client.has_label,
                        self.repo_name,
                        self.item_number,
                        self.label_name,
                    )
                )

            # Fallback: get issue/PR details and check labels
            if self.item_type.lower() == "pr":
                if hasattr(self.github_client, "get_pr_details_by_number"):
                    pr_data = self._retry_operation(
                        self.github_client.get_pr_details_by_number,
                        self.repo_name,
                        self.item_number,
                    )
                    labels = pr_data.get("labels", [])
                else:
                    logger.warning(f"GitHub client does not support PR details retrieval")
                    return False
            else:
                if hasattr(self.github_client, "get_issue_details_by_number"):
                    issue_data = self._retry_operation(
                        self.github_client.get_issue_details_by_number,
                        self.repo_name,
                        self.item_number,
                    )
                    labels = issue_data.get("labels", [])
                else:
                    logger.warning(f"GitHub client does not support issue details retrieval")
                    return False

            return self.label_name in labels

        except Exception as e:
            logger.error(f"Failed to check label '{self.label_name}' on {self.item_type} #{self.item_number}: {e}")
            # On error, return False to allow processing to continue
            return False

    def __enter__(self) -> "LabelManager":
        """Enter the context manager.

        Returns:
            Self instance

        Raises:
            LabelOperationError: If label check and add fails
        """
        logger.debug(f"Entering LabelManager context for {self.item_type} #{self.item_number}")

        # Check and add the label
        result = self.check_and_add_label()

        # If result is False, another instance is processing this item
        if not result:
            raise LabelOperationError(
                f"Another instance is already processing {self.item_type} #{self.item_number} "
                f"(label '{self.label_name}' was just added)"
            )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit the context manager and cleanup resources.

        Always removes the label if it was added and cleanup is needed.
        """
        logger.debug(f"Exiting LabelManager context for {self.item_type} #{self.item_number}")

        # Always attempt cleanup if needed
        if self._should_cleanup:
            try:
                self.remove_label()
            except Exception as e:
                logger.error(f"Error during label cleanup: {e}")

        return False  # Don't suppress exceptions


# Utility functions for centralized label management (deprecated - use LabelManager context manager)
def check_and_add_label(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    item_type: str = "issue",
    dry_run: bool = False,
    config: Any = None,
) -> bool:
    """Check if @auto-coder label exists and add it if not.

    .. deprecated::
        Use LabelManager context manager instead. This function will be removed in a future version.

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
    warnings.warn(
        "check_and_add_label() is deprecated. Use LabelManager context manager instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Use LabelManager internally
    try:
        with LabelManager(github_client, repo_name, item_number, item_type, dry_run, config) as lm:
            # Label added successfully, return True
            pass
        return True
    except LabelOperationError:
        # Another instance is processing
        return False


def remove_label(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    item_type: str = "issue",
    dry_run: bool = False,
    config: Any = None,
) -> None:
    """Remove @auto-coder label from issue/PR.

    .. deprecated::
        Use LabelManager context manager instead. This function will be removed in a future version.

    This function provides unified label removal logic across the codebase.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        item_type: Type of item ('issue' or 'pr')
        dry_run: If True, skip actual label operations
        config: AutomationConfig instance (optional)
    """
    warnings.warn(
        "remove_label() is deprecated. Use LabelManager context manager instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Use LabelManager internally
    lm = LabelManager(github_client, repo_name, item_number, item_type, dry_run, config)
    lm.remove_label()


def check_label_exists(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    label_name: str = "@auto-coder",
    item_type: str = "issue",
) -> bool:
    """Check if a specific label exists on an issue/PR.

    .. deprecated::
        Use LabelManager context manager instead. This function will be removed in a future version.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        label_name: Name of the label to check
        item_type: Type of item ('issue' or 'pr')

    Returns:
        True if label exists, False otherwise
    """
    warnings.warn(
        "check_label_exists() is deprecated. Use LabelManager context manager instead.",
        DeprecationWarning,
        stacklevel=2,
    )

    # Use LabelManager internally
    lm = LabelManager(github_client, repo_name, item_number, item_type, label_name=label_name)
    return lm.verify_label_exists()
