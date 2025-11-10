"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import threading
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional, Union

from github.GithubException import GithubException

from .automation_config import AutomationConfig
from .github_client import GitHubClient
from .logger_config import get_logger

logger = get_logger(__name__)


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


class LabelManager:
    _active_items: set[tuple[int, Union[int, str]]] = set()

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
        In skip_label_add mode: True if label does not exist (should process),
              False if label exists (should not process)

    Raises:
        LabelOperationError: If label operations fail after all retries
    """

    def __init__(
        self,
        github_client: GitHubClient,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        label_name: str = "@auto-coder",
        config: Optional[AutomationConfig] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        skip_label_add: bool = False,
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
            skip_label_add: When True, only check for existing labels without adding.
        Returns True if label does not exist (should process), False if label exists (should not process).
        """
        self.github_client = github_client
        self.repo_name = repo_name
        self.item_number = item_number
        self.item_type = item_type
        self.label_name = label_name
        self.config = config
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.skip_label_add = skip_label_add
        self._lock = threading.Lock()
        self._label_added = False
        self._reentered = False

    def __enter__(self) -> bool:
        """Enter the context manager - add label and return whether to proceed.

        Returns:
            True if process should continue (label added or does not exist in check-only mode),
            False if label already exists (another instance is processing or exists in check-only mode)
        """
        # Reentrancy detection - check if this (thread, item) combination is already active
        ident = threading.get_ident()
        item_key = (ident, self.item_number)
        if item_key in LabelManager._active_items:
            self._reentered = True
            logger.debug(f">>> Should process. Already active for this item in this thread for {self.item_type} #{self.item_number}")
            return True
        else:
            self._reentered = False
            LabelManager._active_items.add(item_key)
            logger.debug(f">>> Entering context (first time for this item in this thread) for {self.item_type} #{self.item_number}")

        # Use lock to ensure thread-safe operations
        with self._lock:
            # Check if labels are disabled
            if self._is_labels_disabled():
                logger.debug(f"Labels disabled - proceeding without label management for {self.item_type} #{self.item_number}")
                # In check-only mode, always return True when labels are disabled
                if self.skip_label_add:
                    return True
                return True

            # Check-only mode: only verify label existence without adding
            if self.skip_label_add:
                logger.debug(f"Check-only mode: verifying if '{self.label_name}' label exists on {self.item_type} #{self.item_number}")
                label_exists = self.github_client.has_label(
                    self.repo_name,
                    int(self.item_number),
                    self.label_name,
                    self.item_type,
                )
                if label_exists:
                    logger.info(f"{self.item_type.capitalize()} #{self.item_number} already has '{self.label_name}' label - skipping")
                    return False  # Return False to indicate label exists (skip processing)
                else:
                    logger.info(f"{self.item_type.capitalize()} #{self.item_number} does not have '{self.label_name}' label - will process")
                    return True  # Return True to indicate label doesn't exist (continue processing)

            # Normal mode: add label with retry logic
            # Try to add the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    result = self.github_client.try_add_labels(self.repo_name, int(self.item_number), [self.label_name], self.item_type)
                    if result:
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
        item_key = (ident, self.item_number)
        if self._reentered:
            logger.debug(f">>> Skipping exit (reentrant) for {self.item_type} #{self.item_number}")
            return

        logger.debug(f">>> Exiting context for {self.item_type} #{self.item_number}")
        # Always clean up thread tracking
        LabelManager._active_items.discard(item_key)

        # In check-only mode, never remove labels
        if self.skip_label_add:
            logger.debug(f"Check-only mode: skipping label removal for {self.item_type} #{self.item_number}")
            return

        # Use lock to ensure thread-safe operations
        with self._lock:
            # Only remove label if we added it and labels are not disabled
            if not self._label_added or self._is_labels_disabled():
                return

            # Remove the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    self.github_client.remove_labels(self.repo_name, int(self.item_number), [self.label_name], self.item_type)
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
