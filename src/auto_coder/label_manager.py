"""Label manager for @auto-coder label operations.

This module provides centralized utilities for @auto-coder label management
across the codebase, eliminating scattered label operation code and providing
consistent error handling and logging.
"""

import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Union

from .automation_config import AutomationConfig

# The exact, raw legacy processing-lock label. Once @auto-coder is retired as
# a lifecycle lock, it must still be treated as semantically inert wherever
# labels are turned into LLM prompt content or semantic PR-label decisions
# (FTR-1792). This is intentionally a literal constant, not
# The filter must always suppress the exact historical label text.
LEGACY_AUTO_CODER_LABEL = "@auto-coder"


def _raw_label_name(label: Any) -> Optional[str]:
    """Extract a label name from a raw GitHub label (string or {"name": ...} dict).

    Returns None for anything else, matching the historical behavior of the
    call sites this replaces (which silently skipped non-str/non-dict entries).
    """
    if isinstance(label, str):
        return label
    if isinstance(label, dict):
        return label.get("name", "")
    return None


def filter_legacy_auto_coder_label(raw_labels: Optional[Iterable[Any]]) -> List[str]:
    """Extract label names from raw entity labels, excluding the exact legacy label.

    This is the shared choke point required by FTR-1792 ("Make legacy
    @auto-coder labels semantically inert"): every consumer that turns entity
    labels into an LLM prompt label list/variable, a label-to-prompt selector
    input, or semantic PR-label resolution input must filter through this
    function first -- before normalization, alias lookup, fuzzy matching,
    priority ordering, or string rendering -- so the retired label can never
    be reinterpreted (via a configured alias or fuzzy match) into another
    semantic label or prompt choice. Labels are compared for an exact,
    case-sensitive match against "@auto-coder"; every other label (including
    near-misses like "auto-coder" or "@auto-coder-old") passes through
    unchanged, in original order.

    Accepts either raw GitHub label dicts (``{"name": "..."}``) or plain
    label name strings, since callers use both shapes.
    """
    names: List[str] = []
    for label in raw_labels or []:
        name = _raw_label_name(label)
        if name is None or name == LEGACY_AUTO_CODER_LABEL:
            continue
        names.append(name)
    return names


def remove_legacy_auto_coder_label(items: Optional[Iterable[Any]]) -> List[Any]:
    """Remove exact "@auto-coder" entries from a list, preserving every other
    entry's original value and type unchanged.

    Some label-to-prompt selector inputs are not pure label-name strings --
    ``prompt_loader._resolve_label_priority`` intentionally supports arbitrary
    comparison keys (e.g. floats, bools) alongside label strings. Coercing
    such entries to strings (as :func:`filter_legacy_auto_coder_label` does)
    would corrupt them, so this variant only ever drops entries that are
    themselves the exact retired label (as a string, or a raw label dict
    named "@auto-coder") and leaves everything else untouched.
    """
    result: List[Any] = []
    for item in items or []:
        if isinstance(item, str) and item == LEGACY_AUTO_CODER_LABEL:
            continue
        if isinstance(item, dict) and item.get("name") == LEGACY_AUTO_CODER_LABEL:
            continue
        result.append(item)
    return result


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
    issue_labels: List[str],
    label_mappings: Dict[str, List[str]],
    use_fuzzy_matching: bool = True,
) -> List[str]:
    """Extract semantic labels from issue labels with alias support and fuzzy matching.

    Args:
        issue_labels: List of labels from the issue
        label_mappings: Dictionary mapping primary labels to their aliases
        use_fuzzy_matching: Whether to use fuzzy matching for label detection

    Returns:
        List of primary semantic labels detected (deduplicated)
    """
    # REQ-003/REQ-004 (FTR-1792): strip the exact retired "@auto-coder" label
    # before any normalization, alias lookup, or fuzzy matching so it can
    # never be reinterpreted into a semantic label via a configured alias.
    issue_labels = filter_legacy_auto_coder_label(issue_labels)

    detected_labels = []

    # Pre-normalize all issue labels once for efficiency
    if use_fuzzy_matching:
        normalized_issue_labels = [_normalize_label(label) for label in issue_labels]
        # Create a set for O(1) lookups
        normalized_issue_set = set(normalized_issue_labels)
        # Also create a set of starting characters for quick filtering
        issue_start_chars = set(nl[0] for nl in normalized_issue_labels if nl)
    else:
        normalized_issue_labels = [label.lower() for label in issue_labels]
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
    """Retained for errors from semantic label operations."""


class LabelManagerContext:
    """Context object shared with asynchronous provider dispatch helpers."""

    def __init__(self, label_manager: "LabelManager", should_process: bool = True):
        """Initialize the context object.

        Args:
            label_manager: The LabelManager instance that created this context
            should_process: Boolean indicating whether processing should continue
        """
        self._label_manager = label_manager
        self._should_process = should_process

    def __bool__(self) -> bool:
        """Return whether durable lifecycle admission allows processing."""
        return self._should_process

    def keep_label(self) -> None:
        """Retain processing context state for asynchronous work."""

    def remove_label(self) -> None:
        """End the processing context without mutating GitHub labels."""

    def _should_remove_label(self) -> bool:
        """Historical labels are never removed by a processing scope."""
        return False


class LabelManager:
    """Side-effect-free processing scope.

    Durable admission repositories now own lifecycle exclusion.  This small scope
    remains while processor call sites use its context object for asynchronous
    session bookkeeping; it deliberately never reads or writes GitHub labels.
    """

    def __init__(
        self,
        github_client: Any,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        config: Optional[Any] = None,
        skip_label_add: bool = False,
        known_labels: Optional[List[Any]] = None,
    ):
        """Create an admitted, label-independent processing scope."""
        self._context = LabelManagerContext(self)

    def __enter__(self) -> LabelManagerContext:
        """Enter the processing scope without consulting GitHub labels."""
        return self._context

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the processing scope without mutating GitHub labels."""
        return None

    def remove_label(self) -> None:
        """Do nothing: historical processing labels are intentionally untouched."""
