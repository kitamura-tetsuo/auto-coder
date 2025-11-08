"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import threading
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional, Union

from .logger_config import get_logger

logger = get_logger(__name__)


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


def _check_label_exists(
    github_client: Any,
    repo_name: str,
    item_number: Union[int, str],
    label_name: str = "@auto-coder",
    item_type: str = "issue",
) -> bool:
    """Check if a specific label exists on an issue/PR.

    This is a private helper function used internally by LabelManager.

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
        if item_type.lower() == "pr":
            pr_data = github_client.get_pr_details_by_number(repo_name, item_number)
            labels = pr_data.get("labels", [])
        else:
            issue_data = github_client.get_issue_details_by_number(repo_name, item_number)
            labels = issue_data.get("labels", [])

        return label_name in labels

    except Exception as e:
        logger.error(f"Failed to check label '{label_name}' on {item_type} #{item_number}: {e}")
        return False


class LabelManager:
    _active_threads: set[int] = set()

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
        config: AutomationConfig instance
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
            config: AutomationConfig instance
            max_retries: Maximum number of retries for label operations
            retry_delay: Delay in seconds between retries
        """
        self.github_client = github_client
        self.repo_name = repo_name
        self.item_number = item_number
        self.item_type = item_type
        self.label_name = label_name
        self.config = config
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._lock = threading.Lock()
        self._label_added = False
        self._reentered = False

    def __enter__(self) -> bool:
        """Enter the context manager - add label and return whether to proceed.

        Returns:
            True if label was successfully added (proceed with processing),
            False if label already exists (another instance is processing)
        """
        # Reentrancy detection - check if this thread is already active
        ident = threading.get_ident()
        if ident in LabelManager._active_threads:
            self._reentered = True
            logger.debug(f">>> Skipping enter (already active in this thread) for {self.item_type} #{self.item_number}")
            return True
        else:
            self._reentered = False
            LabelManager._active_threads.add(ident)
            logger.debug(f">>> Entering context (first time in this thread) for {self.item_type} #{self.item_number}")

        # Use lock to ensure thread-safe operations
        with self._lock:
            # Check if labels are disabled
            if self._is_labels_disabled():
                logger.debug(f"Labels disabled - proceeding without label management for {self.item_type} #{self.item_number}")
                return True

            # Try to add the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    # In DRY_RUN mode, skip label existence check and API calls
                    if self.config.DRY_RUN:
                        logger.info(f"[DRY RUN] Would add '{self.label_name}' label to {self.item_type} #{self.item_number}")
                        self._label_added = True
                        return True

                    # In DRY_RUN mode, skip label existence check and API calls
                    if not self.config.SKIP_BY_LABELS:
                        logger.info(f"Adding '{self.label_name}' label to {self.item_type} #{self.item_number}")
                        self.github_client.try_add_work_in_progress_label(self.repo_name, self.item_number, label=self.label_name)
                        self._label_added = True
                        return True

                    # Check if label already exists
                    if _check_label_exists(
                        self.github_client,
                        self.repo_name,
                        self.item_number,
                        self.label_name,
                        self.item_type,
                    ):
                        logger.info(f"{self.item_type.capitalize()} #{self.item_number} already has '{self.label_name}' label - skipping")
                        return False

                    # Try to add the label
                    result = self.github_client.try_add_work_in_progress_label(self.repo_name, self.item_number, label=self.label_name)
                    if result:
                        logger.info(f"Added '{self.label_name}' label to {self.item_type} #{self.item_number}")
                        self._label_added = True
                        return True
                    else:
                        logger.info(f"Skipping {self.item_type} #{self.item_number} - '{self.label_name}' label was just added by another instance")
                        return False

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
        # Reentrancy detection - skip exit if this is a reentrant call
        ident = threading.get_ident()
        if self._reentered:
            logger.debug(f">>> Skipping exit (reentrant) for {self.item_type} #{self.item_number}")
            return

        logger.debug(f">>> Exiting context for {self.item_type} #{self.item_number}")
        # Always clean up thread tracking
        LabelManager._active_threads.discard(ident)

        # Use lock to ensure thread-safe operations
        with self._lock:
            # Only remove label if we added it and labels are not disabled
            if not self._label_added or self._is_labels_disabled():
                return

            # Remove the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    if self.config.DRY_RUN:
                        logger.info(f"[DRY RUN] Would remove '{self.label_name}' label from {self.item_type} #{self.item_number}")
                        return

                    self.github_client.remove_labels_from_issue(self.repo_name, self.item_number, [self.label_name])
                    logger.info(f"Removed '{self.label_name}' label from {self.item_type} #{self.item_number}")
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
        # Check if github_client has disable_labels attribute and if it's set
        if hasattr(self.github_client, "disable_labels") and self.github_client.disable_labels:
            return True

        if self.config and self.config.DISABLE_LABELS:
            return True

        return False
