"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import re
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Dict, Generator, List, Optional, Union

from github.GithubException import GithubException

from .automation_config import AutomationConfig
from .github_client import GitHubClient
from .logger_config import get_logger

logger = get_logger(__name__)


def _calculate_levenshtein_distance(s1: str, s2: str) -> int:
    """Calculate the Levenshtein distance between two strings.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Edit distance between the two strings
    """
    if len(s1) < len(s2):
        return _calculate_levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


@lru_cache(maxsize=10000)
def _normalize_label(label: str) -> str:
    """Normalize a label for fuzzy matching.

    Removes special characters, converts to lowercase, and standardizes common variations.

    Args:
        label: The label to normalize

    Returns:
        Normalized label string
    """
    # Convert to lowercase
    normalized = label.lower()

    # Replace common separators with hyphen
    normalized = re.sub(r"[\s_]+", "-", normalized)

    # Remove special characters except hyphens
    normalized = re.sub(r"[^\w-]", "", normalized)

    # Remove duplicate hyphens - use a more efficient pattern
    # Replace 2 or more hyphens with a single hyphen
    normalized = re.sub(r"-{2,}", "-", normalized)

    # Strip hyphens from start and end
    normalized = normalized.strip("-")

    return normalized


@lru_cache(maxsize=5000)
def _is_fuzzy_match(candidate: str, target: str, max_distance: int = 1) -> bool:
    """Check if a candidate label fuzzy matches a target label.

    Performs fuzzy matching using:
    1. Exact match (after normalization)
    2. Partial match (substring)
    3. Levenshtein distance (for typos)

    Args:
        candidate: The label from the issue
        target: The target label (alias) to match against
        max_distance: Maximum Levenshtein distance allowed for fuzzy match

    Returns:
        True if the candidate matches the target, False otherwise
    """
    # Normalize both strings
    norm_candidate = _normalize_label(candidate)
    norm_target = _normalize_label(target)

    # Exact match
    if norm_candidate == norm_target:
        return True

    # Check for common prefix/suffix patterns
    # e.g., "bug-fix" and "bugfix" should match
    if norm_candidate.replace("-", "") == norm_target.replace("-", ""):
        return True

    # Partial match - check if target is contained in candidate or vice versa
    # This handles cases like "bc-breaking" matching "breaking-change"
    # Both strings must be at least 3 characters to avoid false positives
    if len(norm_target) >= 3 and len(norm_candidate) >= 3:
        if norm_target in norm_candidate or norm_candidate in norm_target:
            return True

    # Enhanced partial matching: check if any significant part matches
    # Split by common separators and check if significant parts match
    candidate_parts = set(re.split(r"[-_\s]+", norm_candidate))
    target_parts = set(re.split(r"[-_\s]+", norm_target))

    # Remove empty strings
    candidate_parts = {p for p in candidate_parts if p}
    target_parts = {p for p in target_parts if p}

    # Check if any significant part from target matches any part from candidate
    for t_part in target_parts:
        if len(t_part) >= 3:  # Only consider parts with 3+ characters
            for c_part in candidate_parts:
                if len(c_part) >= 3:
                    # Check if one is a substring of the other
                    if t_part in c_part or c_part in t_part:
                        return True

    # Levenshtein distance for typos (only for strings of reasonable length)
    # Both strings must be at least 3 characters
    if len(norm_candidate) >= 3 and len(norm_target) >= 3 and len(norm_candidate) <= 30 and len(norm_target) <= 30:
        # Calculate threshold: 1 for short strings, 2 for longer ones
        min_len = min(len(norm_candidate), len(norm_target))
        if min_len < 8:
            max_allowed = 1
        elif min_len < 15:
            max_allowed = 2
        else:
            max_allowed = 3

        # Use the maximum of max_distance and max_allowed
        threshold = max(max_distance, max_allowed)
        distance = _calculate_levenshtein_distance(norm_candidate, norm_target)
        if distance <= threshold:
            return True

    return False


def get_semantic_labels_from_issue(
    issue_labels: List[Union[str, Dict[str, Any]]],
    label_mappings: Dict[str, List[str]],
    use_fuzzy_matching: bool = True,
) -> List[str]:
    """Extract semantic labels from issue labels with alias support and fuzzy matching.

    Args:
        issue_labels: List of labels from the issue (can be strings or dicts)
        label_mappings: Dictionary mapping primary labels to their aliases
        use_fuzzy_matching: Whether to use fuzzy matching for label detection

    Returns:
        List of primary semantic labels detected (deduplicated)
    """
    detected_labels = []

    # Normalize issue_labels to a list of strings
    label_names = []
    for label in issue_labels:
        if isinstance(label, dict):
            label_names.append(label.get("name", ""))
        elif isinstance(label, str):
            label_names.append(label)

    # Pre-normalize all issue labels once for efficiency
    if use_fuzzy_matching:
        normalized_issue_labels = [_normalize_label(label) for label in label_names]
        # Create a set for O(1) lookups
        normalized_issue_set = set(normalized_issue_labels)
        # Also create a set of starting characters for quick filtering
        issue_start_chars = set(nl[0] for nl in normalized_issue_labels if nl)
    else:
        normalized_issue_labels = [label.lower() for label in label_names]
        normalized_issue_set = set(normalized_issue_labels)
        issue_start_chars = set(nl[0] for nl in normalized_issue_labels if nl)

    # Pre-normalize all aliases for efficiency
    normalized_mappings = {}
    for primary_label, aliases in label_mappings.items():
        normalized_mappings[primary_label] = [_normalize_label(alias) for alias in aliases]

    for primary_label, normalized_aliases in normalized_mappings.items():
        # Check if any alias matches (case-insensitive or fuzzy)
        matched = False

        # Quick exact match first using set lookup
        if any(alias in normalized_issue_set for alias in normalized_aliases):
            detected_labels.append(primary_label)
            matched = True
            continue  # Skip fuzzy matching if exact match found

        # Only do fuzzy matching if exact match fails
        if not matched and use_fuzzy_matching:
            for alias in normalized_aliases:
                # Quick filter: skip if starting character is completely different
                if len(alias) >= 3 and alias[0] in issue_start_chars:
                    # Try fuzzy matching using cached function
                    for issue_label in issue_labels:
                        if _is_fuzzy_match(issue_label, alias):
                            detected_labels.append(primary_label)
                            matched = True
                            break

                if matched:
                    break

    # Remove duplicates while preserving order
    return list(dict.fromkeys(detected_labels))


def resolve_pr_labels_with_priority(
    issue_labels: List[str],
    config: AutomationConfig,
) -> List[str]:
    """Resolve PR labels with priority-based selection.

    Args:
        issue_labels: List of labels from the source issue
        config: AutomationConfig instance with PR label configuration

    Returns:
        List of semantic labels for the PR, sorted by priority and limited to max count
    """
    # Extract semantic labels from issue
    semantic_labels = get_semantic_labels_from_issue(issue_labels, config.PR_LABEL_MAPPINGS)

    if not semantic_labels:
        return []

    # Sort labels by priority
    priority_order = {label: idx for idx, label in enumerate(config.PR_LABEL_PRIORITIES)}

    # Separate labels into prioritized and unprioritized
    prioritized = []

    for label in semantic_labels:
        if label in priority_order:
            prioritized.append((label, priority_order[label]))
        else:
            # Use a high priority value for unprioritized labels
            prioritized.append((label, 999))

    # Sort by priority value
    sorted_labels = [label for label, _ in sorted(prioritized, key=lambda x: x[1])]

    # Limit to max labels
    max_labels = config.PR_LABEL_MAX_COUNT
    if max_labels >= 0:
        sorted_labels = sorted_labels[:max_labels]

    return sorted_labels


class LabelOperationError(Exception):
    """Exception raised when label operations fail."""

    pass


class LabelManagerContext:
    """Context object returned by LabelManager.__enter__.

    This class maintains backward compatibility by implementing __bool__,
    while also providing a keep_label() method to allow retaining the label.

    Usage:
        with LabelManager(...) as context:
            if not context:
                return
            # Process
            context.keep_label()  # Keep the label on exit
    """

    def __init__(self, label_manager: "LabelManager", should_process: bool):
        """Initialize the context object.

        Args:
            label_manager: The LabelManager instance that created this context
            should_process: Boolean indicating whether processing should continue
        """
        self._label_manager = label_manager
        self._should_process = should_process
        self._keep_label_on_exit = False

    def __bool__(self) -> bool:
        """Return whether processing should continue (backward compatibility)."""
        return self._should_process

    def keep_label(self) -> None:
        """Set flag to keep the label on exit instead of removing it."""
        self._keep_label_on_exit = True

    def remove_label(self) -> None:
        """Explicitly remove the label."""
        self._label_manager.remove_label()

    def _should_remove_label(self) -> bool:
        """Check if the label should be removed on exit.

        Returns:
            True if label should be removed, False otherwise
        """
        return not self._keep_label_on_exit


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
        github_client: Any,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        label_name: str = "@auto-coder",
        config: Optional[AutomationConfig] = None,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        skip_label_add: bool = False,
        check_labels: bool = True,
        known_labels: Optional[List[Any]] = None,
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
            check_labels: When False, skip the existing label check to bypass label verification.
            known_labels: Optional list of known labels to avoid API calls for existence check.
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
        self.check_labels = check_labels

        # Normalize known_labels to a list of strings
        self.known_labels: Optional[List[str]] = None
        if known_labels is not None:
            self.known_labels = []
            for label in known_labels:
                if isinstance(label, str):
                    self.known_labels.append(label)
                elif isinstance(label, dict) and "name" in label:
                    self.known_labels.append(label["name"])
                elif hasattr(label, "name"):
                    self.known_labels.append(label.name)

        self._lock = threading.Lock()
        self._label_added = False
        self._reentered = False
        self._context: Optional[LabelManagerContext] = None

    def __enter__(self) -> LabelManagerContext:
        """Enter the context manager - add label and return context object.

        Returns:
            LabelManagerContext: Context object with __bool__ for backward compatibility
                                and keep_label() method for retaining the label.
        """
        # Reentrancy detection - check if this (thread, item) combination is already active
        ident = threading.get_ident()
        item_key = (ident, self.item_number)
        if item_key in LabelManager._active_items:
            self._reentered = True
            logger.debug(f">>> Should process. Already active for this item in this thread for {self.item_type} #{self.item_number}")
            # Create context with should_process=True for reentrancy
            self._context = LabelManagerContext(self, True)
            return self._context
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
                    self._context = LabelManagerContext(self, True)
                    return self._context
                self._context = LabelManagerContext(self, True)
                return self._context

            # Check-only mode: only verify label existence without adding
            if self.skip_label_add:
                logger.debug(f"Check-only mode: verifying if '{self.label_name}' label exists on {self.item_type} #{self.item_number}")
                # Use helper that fails-open (returns True to continue on errors)
                should_process = self._check_label_exists()
                self._context = LabelManagerContext(self, should_process)
                return self._context

            # Normal mode: add label with retry logic
            # When check_labels=False (WIP mode), skip pre-check and proceed
            if self.check_labels:
                # First, pre-check if the label already exists to avoid redundant edits
                try:
                    should_process = self._check_label_exists()
                    if not should_process:
                        logger.info(f"Skipping {self.item_type} #{self.item_number} - '{self.label_name}' label already exists")
                        self._context = LabelManagerContext(self, False)
                        return self._context
                except Exception:
                    # _check_label_exists() is defensive and should not raise, but guard anyway
                    pass
            else:
                logger.debug(f"check_labels=False - skipping existing label check for {self.item_type} #{self.item_number}")

            # Try to add the label with retry logic
            for attempt in range(self.max_retries):
                try:
                    # Use the generic method for adding labels
                    result = self.github_client.try_add_labels(
                        self.repo_name,
                        int(self.item_number),
                        [self.label_name],
                        item_type=self.item_type,
                    )
                    if result:
                        self._label_added = True
                        self._context = LabelManagerContext(self, True)
                        return self._context
                    else:
                        logger.info(f"Skipping {self.item_type} #{self.item_number} - '{self.label_name}' label was just added by another instance")
                        self._context = LabelManagerContext(self, False)
                        return self._context

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        logger.warning(f"Failed to add '{self.label_name}' label to {self.item_type} #{self.item_number} " f"(attempt {attempt + 1}/{self.max_retries}): {e}. Retrying in {self.retry_delay}s...")
                        time.sleep(self.retry_delay)
                    else:
                        logger.error(f"Failed to add '{self.label_name}' label to {self.item_type} #{self.item_number} " f"after {self.max_retries} attempts: {e}")
                        # On error, allow processing to continue
                        self._context = LabelManagerContext(self, True)
                        return self._context

            # Should not reach here, but just in case
            self._context = LabelManagerContext(self, True)
            return self._context

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the context manager - remove label if it was added and not retained.

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

        # Check if the context has the keep_label flag set
        if hasattr(self, "_context") and self._context and not self._context._should_remove_label():
            logger.debug(f"Keeping '{self.label_name}' label on exit as requested for {self.item_type} #{self.item_number}")
            return

        # Use lock to ensure thread-safe operations
        with self._lock:
            # Only remove label if we added it and labels are not disabled
            if not self._label_added or self._is_labels_disabled():
                return

            self._remove_label_internal()

    def remove_label(self) -> None:
        """Explicitly remove the managed label."""
        with self._lock:
            self._remove_label_internal()

    def _remove_label_internal(self) -> None:
        """Internal helper to remove label with retry logic."""
        # Remove the label with retry logic
        for attempt in range(self.max_retries):
            try:
                # Respect the item type ("issue" vs "pr") so logs and GitHub API paths are consistent
                self.github_client.remove_labels(
                    self.repo_name,
                    self.item_number,
                    [self.label_name],
                    self.item_type,
                )
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

    def _check_label_exists(self) -> bool:
        """Check whether the target label exists and decide if processing should continue.

        Returns:
            True: proceed with processing (label does NOT exist or check failed)
            False: skip processing (label already exists)
        """
        try:
            # Optimization: Use known_labels if provided to avoid API calls
            if self.known_labels is not None:
                if self.label_name in self.known_labels:
                    logger.info(f"{self.item_type.capitalize()} #{self.item_number} already has '{self.label_name}' label (checked via known_labels) - skipping")
                    return False
                else:
                    logger.info(f"{self.item_type.capitalize()} #{self.item_number} does not have '{self.label_name}' label (checked via known_labels) - will process")
                    return True

            # Prefer dedicated has_label() when using a real GitHubClient instance or a mock with has_label
            # Check if client is a GitHubClient instance OR has a callable has_label method
            if isinstance(self.github_client, GitHubClient) or (hasattr(self.github_client, "has_label") and callable(getattr(self.github_client, "has_label", None))):
                exists = self.github_client.has_label(
                    self.repo_name,
                    int(self.item_number),
                    self.label_name,
                    self.item_type,
                )
                # In tests, a Mock(spec=GitHubClient) may return a Mock here; only trust booleans
                if isinstance(exists, bool):
                    if exists:
                        logger.info(f"{self.item_type.capitalize()} #{self.item_number} already has '{self.label_name}' label - skipping")
                        return False
                    else:
                        logger.info(f"{self.item_type.capitalize()} #{self.item_number} does not have '{self.label_name}' label - will process")
                        return True
                # Fall through to fallback path if result is not boolean

            # Fallback when using a mocked client (spec=GitHubClient) or when has_label is unavailable.
            # Use object-based detail getters that tests commonly patch (get_pr_details / get_issue_details).
            if self.item_type.lower() == "pr":
                pr_labels: list[str] = []
                try:
                    repo = self.github_client.get_repository(self.repo_name)
                    pr_obj = repo.get_pull(int(self.item_number))
                    pr_details = self.github_client.get_pr_details(pr_obj)
                    if isinstance(pr_details, dict):
                        pr_labels = pr_details.get("labels", []) or []
                except Exception:
                    # Ignore and keep labels as empty list
                    pass

                return self.label_name not in pr_labels

            # Issue path
            issue_labels: list[str] = []
            try:
                repo = self.github_client.get_repository(self.repo_name)
                issue_obj = repo.get_issue(int(self.item_number))
                issue_details = self.github_client.get_issue_details(issue_obj)
                if isinstance(issue_details, dict):
                    issue_labels = issue_details.get("labels", []) or []
            except Exception:
                # Ignore and keep labels as empty list
                pass

            return self.label_name not in issue_labels

        except Exception as e:
            # Fail-open: on errors, allow processing to continue
            logger.warning(f"Label existence check failed for {self.item_type} #{self.item_number}: {e}. Proceeding.")
            return True

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
