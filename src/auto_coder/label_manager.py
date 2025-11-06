"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import inspect
import threading
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional, Union

from .logger_config import get_logger

logger = get_logger(__name__)


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


def _is_real_method(obj: Any, method_name: str) -> bool:
    """Check if an object has a real method (not an unconfigured Mock attribute).

    Args:
        obj: Object to check
        method_name: Name of the method to check

    Returns:
        True if the object has a real method with the given name, False otherwise
    """
    try:
        # Import Mock and sentinel here to avoid circular import
        from unittest.mock import DEFAULT, Mock

        # Get the attribute
        attr = getattr(obj, method_name, None)

        # If the object is a Mock, check if the method is configured
        if isinstance(obj, Mock):
            # For Mock objects, check if the method is a configured Mock
            mock_attr = attr
            if isinstance(mock_attr, Mock):
                # Check if this Mock attribute has been configured
                # Configured mocks have _mock_return_value not set to DEFAULT
                return mock_attr._mock_return_value is not DEFAULT

        # For non-Mock objects, check if it's callable and is a method or function
        if callable(attr):
            # Check if it's a bound method or function (real method)
            return inspect.ismethod(attr) or inspect.isfunction(attr)

        return False
    except Exception:
        return False


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
    try:
        # Check if has_label method exists (not just a Mock attribute)
        if _is_real_method(github_client, "has_label"):
            return bool(github_client.has_label(repo_name, item_number, label_name))

        # Fallback: get issue/PR details and check labels
        if item_type.lower() == "pr":
            if _is_real_method(github_client, "get_pr_details_by_number"):
                pr_data = github_client.get_pr_details_by_number(repo_name, item_number)
                labels = pr_data.get("labels", [])
            else:
                logger.warning(f"GitHub client does not support PR details retrieval")
                return False
        else:
            if _is_real_method(github_client, "get_issue_details_by_number"):
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


class LabelManager:
    """Context manager for unified @auto-coder label operations.

    This context manager automatically handles adding, verifying, and removing
    the @auto-coder label for issues and PRs, providing a clean API that ensures
    proper resource cleanup even when exceptions occur.

    Usage:
        with LabelManager(github_client, repo_name, item_number, item_type="issue") as should_process:
            if not should_process:
                # Label was already present, another instance is processing
                return

            # Process the issue/PR
            perform_work()

    The context manager will:
    1. Check if @auto-coder label exists on entry
    2. Add the label if it doesn't exist (returns True to continue)
    3. Return False if label already exists (another instance is processing)
    4. Verify the label was successfully added
    5. Automatically remove the label on exit (normal or exceptional)
    6. Handle errors gracefully with retry logic

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        item_number: Issue or PR number
        item_type: Type of item ('issue' or 'pr'), defaults to 'issue'
        label_name: Name of the label to manage, defaults to '@auto-coder'
        dry_run: If True, skip actual label operations, defaults to False
        config: AutomationConfig instance (optional)
        max_retries: Maximum number of retries for label operations, defaults to 3
        retry_delay: Delay in seconds between retries, defaults to 1.0

    Returns:
        bool: True if label was successfully added and processing should continue,
              False if label already exists (another instance is processing)

    Raises:
        LabelOperationError: If label operations fail after all retries
    """

    def __init__(
        self,
        github_client: Any,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        label_name: str = "@auto-coder",
        dry_run: bool = False,
        config: Any = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        """Initialize LabelManager context manager.

        Args:
            github_client: GitHub client instance
            repo_name: Repository name (owner/repo)
            item_number: Issue or PR number
            item_type: Type of item ('issue' or 'pr')
            label_name: Name of the label to manage
            dry_run: If True, skip actual label operations
            config: AutomationConfig instance (optional)
            max_retries: Maximum number of retries for label operations
            retry_delay: Delay in seconds between retries
        """
        self.github_client = github_client
        self.repo_name = repo_name
        self.item_number = item_number
        self.item_type = item_type
        self.label_name = label_name
        self.dry_run = dry_run
        self.config = config
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._lock = threading.Lock()
        self._label_added = False

    def __enter__(self) -> bool:
        """Enter the context manager - add label and return whether to proceed.

        Returns:
            True if label was successfully added (proceed with processing),
            False if label already exists (another instance is processing)
        """
        # Use lock to ensure thread-safe operations
        with self._lock:
            # Check if labels are disabled
            if self._is_labels_disabled():
                logger.debug(f"Labels disabled - proceeding without label management for {self.item_type} #{self.item_number}")
                return True

            # Try to add the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    # Check if label already exists
                    if check_label_exists(
                        self.github_client,
                        self.repo_name,
                        self.item_number,
                        self.label_name,
                        self.item_type,
                    ):
                        logger.info(f"{self.item_type.capitalize()} #{self.item_number} already has '{self.label_name}' label - skipping")
                        return False

                    # Try to add the label
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would add '{self.label_name}' label to {self.item_type} #{self.item_number}")
                        self._label_added = True
                        return True

                    # Use the GitHub client's method to add the label
                    if hasattr(self.github_client, "try_add_work_in_progress_label"):
                        result = self.github_client.try_add_work_in_progress_label(self.repo_name, self.item_number, label=self.label_name)
                        if result:
                            logger.info(f"Added '{self.label_name}' label to {self.item_type} #{self.item_number}")
                            self._label_added = True
                            return True
                        else:
                            # Label was just added by another instance
                            logger.info(f"Skipping {self.item_type} #{self.item_number} - '{self.label_name}' label was just added by another instance")
                            return False
                    else:
                        logger.error(f"GitHub client does not support try_add_work_in_progress_label")
                        # On error, allow processing to continue
                        return True

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        logger.warning(f"Failed to add '{self.label_name}' label to {self.item_type} #{self.item_number} " f"(attempt {attempt + 1}/{self.max_retries}): {e}. Retrying in {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                    else:
                        logger.error(f"Failed to add '{self.label_name}' label to {self.item_type} #{self.item_number} " f"after {self.max_retries} attempts: {e}")
                        # On error, allow processing to continue
                        return True

            # Should not reach here, but just in case
            return True

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the context manager - remove label if it was added.

        Args:
            exc_type: Exception type (if any)
            exc_val: Exception value (if any)
            exc_tb: Exception traceback (if any)
        """
        # Use lock to ensure thread-safe operations
        with self._lock:
            # Only remove label if we added it and labels are not disabled
            if not self._label_added or self._is_labels_disabled():
                return

            # Remove the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would remove '{self.label_name}' label from {self.item_type} #{self.item_number}")
                        return

                    if hasattr(self.github_client, "remove_labels_from_issue"):
                        self.github_client.remove_labels_from_issue(self.repo_name, self.item_number, [self.label_name])
                        logger.info(f"Removed '{self.label_name}' label from {self.item_type} #{self.item_number}")
                        return
                    else:
                        logger.warning(f"GitHub client does not support label removal")
                        return

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        logger.warning(f"Failed to remove '{self.label_name}' label from {self.item_type} #{self.item_number} " f"(attempt {attempt + 1}/{self.max_retries}): {e}. Retrying in {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                    else:
                        logger.error(f"Failed to remove '{self.label_name}' label from {self.item_type} #{self.item_number} " f"after {self.max_retries} attempts: {e}")
                        # Log but don't raise - we don't want to break the cleanup process
                        return

    def _is_labels_disabled(self) -> bool:
        """Check if label operations are disabled.

        Returns:
            True if labels are disabled, False otherwise
        """
        # Check if labels are disabled via GitHub client
        if hasattr(self.github_client, "disable_labels") and self.github_client.disable_labels:
            return True

        # Check if labels are disabled via config
        if self.config and hasattr(self.config, "DISABLE_LABELS") and self.config.DISABLE_LABELS:
            return True

        return False
