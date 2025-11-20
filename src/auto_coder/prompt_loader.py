"""Utilities for loading and formatting LLM instruction prompts from YAML files."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

import yaml  # type: ignore[import-untyped]

from .logger_config import get_logger, log_calls

logger = get_logger(__name__)

DEFAULT_PROMPTS_PATH = Path(__file__).resolve().parent / "prompts.yaml"

_PROMPTS_CACHE: Dict[Path, Dict[str, Any]] = {}


def _resolve_path(path: Optional[str] = None) -> Path:
    """Resolve the prompt configuration path."""
    if path is None:
        return DEFAULT_PROMPTS_PATH
    return Path(path).expanduser().resolve()


def load_prompts(path: Optional[str] = None) -> Dict[str, Any]:
    """Load prompts from YAML file, caching the parsed mapping.

    Requirement: If prompts fail to load, subsequent processing cannot continue, so exit immediately.
    Therefore, this function raises SystemExit as a fatal error.
    """
    resolved = _resolve_path(path)
    cached = _PROMPTS_CACHE.get(resolved)
    if cached is not None:
        return cached

    try:
        with resolved.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError as exc:
        msg = f"Prompt configuration file not found: {resolved}"
        logger.critical(msg)
        raise SystemExit(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"Failed to parse prompt configuration: {resolved}: {exc}"
        logger.critical(msg)
        raise SystemExit(msg) from exc

    if not isinstance(data, dict):
        msg = f"Prompt configuration root must be a mapping: {resolved}"
        logger.critical(msg)
        raise SystemExit(msg)

    _PROMPTS_CACHE[resolved] = data
    return data


def clear_prompt_cache() -> None:
    """Clear the prompt cache (useful for tests)."""
    _PROMPTS_CACHE.clear()


def _traverse(prompts: Dict[str, Any], key: str) -> Any:
    """Traverse nested dictionaries using dot-separated keys."""
    current: Any = prompts
    for segment in key.split("."):
        if not isinstance(current, dict):
            raise KeyError(f"Prompt path '{key}' does not resolve to a mapping")
        if segment not in current:
            raise KeyError(f"Prompt '{key}' not found in configuration")
        current = current[segment]
    return current


def _resolve_label_priority(
    issue_labels: List[str],
    label_prompt_mappings: Optional[Dict[str, str]],
    label_priorities: Optional[List[str]],
) -> Optional[str]:
    """Resolve highest priority label that has a prompt mapping.

    Args:
        issue_labels: List of labels from the issue
        label_prompt_mappings: Dictionary mapping labels to prompt template keys
        label_priorities: List of labels in priority order (highest priority first)

    Returns:
        The highest priority label that has a prompt mapping, or None if no applicable labels
    """
    # Handle case where label_prompt_mappings is not a dict
    if not isinstance(label_prompt_mappings, dict):
        return None

    # Create case-insensitive mapping for matching
    # Normalized label (lowercase) -> original mapping key
    case_insensitive_to_original: Dict[str, str] = {}
    for mapping_key, prompt_key in label_prompt_mappings.items():
        # Ensure label is a string for comparison
        try:
            normalized_mapping_key = str(mapping_key).lower()
        except (AttributeError, TypeError):
            # Skip labels that can't be converted to string
            continue
        case_insensitive_to_original[normalized_mapping_key] = mapping_key

    # Filter to labels with configured prompt mappings (case-insensitive)
    # Store both issue label and mapping key to support both matching and return value
    applicable_pairs = []  # List of (issue_label, mapping_key) tuples
    for issue_label in issue_labels:
        # Ensure label is a string for comparison
        try:
            normalized_issue_label = str(issue_label).lower()
        except (AttributeError, TypeError):
            # Skip labels that can't be converted to string
            continue
        if normalized_issue_label in case_insensitive_to_original:
            # Store both the original issue label (for priority matching) and mapping key (for return)
            original_mapping_key = case_insensitive_to_original[normalized_issue_label]
            applicable_pairs.append((issue_label, original_mapping_key))

    if not applicable_pairs:
        return None

    # Extract just the issue labels for priority matching (preserves original case from issue)
    applicable_issue_labels = [pair[0] for pair in applicable_pairs]
    # Extract mapping keys for final return (ensures correct key for lookup)
    mapping_keys = [pair[1] for pair in applicable_pairs]

    # If priorities is None, return None (no priority system configured)
    if label_priorities is None:
        return None

    # If priorities is empty list, fallback to first applicable mapping key
    if not label_priorities:
        return mapping_keys[0]

    # Sort by priority and return highest priority mapping key
    # First, check for exact case-sensitive matches
    for priority_label in label_priorities:
        if priority_label in applicable_issue_labels:
            # Find the corresponding mapping key for the matched issue label
            for issue_label, mapping_key in applicable_pairs:
                if issue_label == priority_label:
                    return mapping_key

    # Check if there are any case-insensitive matches that failed due to case sensitivity
    # If so, this indicates a configuration issue (case mismatch) and we should return None
    priorities_lower = {str(p).lower() for p in label_priorities if p is not None}
    applicable_issue_labels_lower = {str(a).lower() for a in applicable_issue_labels if a is not None}

    # If there are overlapping labels when ignoring case, it means there are case mismatches
    if priorities_lower & applicable_issue_labels_lower:  # Set intersection - if not empty
        return None

    # If there are no conceptual overlaps between priorities and applicable labels,
    # return the first applicable as a fallback
    return mapping_keys[0]


def _is_breaking_change_issue(issue_labels: List[str]) -> bool:
    """Check if issue has breaking-change related labels.

    Args:
        issue_labels: List of labels from the issue

    Returns:
        True if issue has any breaking-change related labels, False otherwise
    """
    breaking_change_aliases = [
        "breaking-change",
        "breaking",
        "api-change",
        "deprecation",
        "version-major",
    ]
    return any(label.lower() in breaking_change_aliases for label in issue_labels)


def _get_prompt_for_labels(
    issue_labels: List[str],
    label_prompt_mappings: Optional[Dict[str, str]],
    label_priorities: Optional[List[str]],
) -> Optional[str]:
    """Get the appropriate prompt template key based on issue labels.

    Args:
        issue_labels: List of labels from the issue
        label_prompt_mappings: Dictionary mapping labels to prompt template keys
        label_priorities: List of labels in priority order (highest priority first)

    Returns:
        The prompt template key for the highest priority applicable label,
        or None if no label-specific prompt mapping exists
    """
    if not issue_labels:
        return None

    if not label_prompt_mappings:
        return None

    # Resolve to the highest priority applicable label
    resolved_label = _resolve_label_priority(issue_labels, label_prompt_mappings, label_priorities)

    if resolved_label:
        try:
            return label_prompt_mappings.get(resolved_label)
        except AttributeError:
            # Handle case where label_prompt_mappings is not a dict
            return None

    return None


def get_prompt_template(key: str, path: Optional[str] = None) -> str:
    """Return the raw prompt template string for the given key.

    Referencing an undefined key has no meaning even if processing continues, so exit immediately with SystemExit as a fatal error.
    """
    prompts = load_prompts(path)
    try:
        template = _traverse(prompts, key)
    except KeyError as exc:
        # Fail-fast for missing prompt keys to avoid continuing with undefined instructions
        msg = str(exc)
        logger.critical(msg)
        raise SystemExit(msg) from exc
    if not isinstance(template, str):
        raise ValueError(f"Prompt '{key}' must map to a string template")
    return template


def get_label_specific_prompt(
    labels: List[str],
    label_prompt_mappings: Optional[Dict[str, str]],
    label_priorities: Optional[List[str]],
    path: Optional[str] = None,
) -> Optional[str]:
    """Get the label-specific prompt template key.

    Args:
        labels: List of labels from the issue
        label_prompt_mappings: Dictionary mapping labels to prompt template keys
        label_priorities: List of labels in priority order (highest priority first)
        path: Optional path to prompts.yaml file

    Returns:
        The prompt template key for the highest priority applicable label,
        or None if no label-specific prompt mapping exists
    """
    # Validate configuration parameters
    if not labels:
        logger.debug("No labels provided for label-based prompt selection")
        return None

    if not label_prompt_mappings:
        logger.debug("No label-to-prompt mappings provided")
        return None

    if not label_priorities:
        logger.debug("No label priorities provided")
        return None

    # Get the prompt template key for the labels
    prompt_key = _get_prompt_for_labels(labels, label_prompt_mappings, label_priorities)

    if prompt_key:
        logger.debug(f"Resolved label-specific prompt key '{prompt_key}' for labels: {labels}")
    else:
        logger.debug(f"No label-specific prompt mapping found for labels: {labels}")

    return prompt_key


@log_calls
def render_prompt(
    key: str,
    *,
    path: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    labels: Optional[List[str]] = None,
    label_prompt_mappings: Optional[Dict[str, str]] = None,
    label_priorities: Optional[List[str]] = None,
    **kwargs: Any,
) -> str:
    """Render a prompt template identified by key with optional label-based selection.

    Args:
        key: The prompt template key (e.g., "issue.action")
        path: Optional path to prompts.yaml file
        data: Optional dictionary of data for template substitution
        labels: Optional list of issue labels for label-based prompt selection
        label_prompt_mappings: Optional dict mapping labels to prompt template keys
        label_priorities: Optional list of labels in priority order (highest priority first)
        **kwargs: Additional keyword arguments for template substitution

    Returns:
        The rendered prompt template string

    Note:
        If labels, label_prompt_mappings, and label_priorities are provided,
        the function will attempt to use a label-specific prompt template.
        If the label-specific template fails to render or doesn't exist,
        it will fall back to the default key.
    """
    # If labels and mappings are provided, attempt label-based prompt selection
    if labels and label_prompt_mappings and label_priorities:
        label_specific_key = _get_prompt_for_labels(labels, label_prompt_mappings, label_priorities)

        # Only use label-specific prompt if we found a valid mapping
        if label_specific_key:
            logger.debug(f"Using label-specific prompt '{label_specific_key}' for labels: {labels}")
            try:
                # Recursively call render_prompt with the resolved label-specific key
                result = render_prompt(
                    label_specific_key,
                    path=path,
                    data=data,
                    labels=None,  # Prevent infinite recursion
                    label_prompt_mappings=None,
                    label_priorities=None,
                    **kwargs,
                )
                return result  # type: ignore[no-any-return]
            except SystemExit:
                # Handle SystemExit (e.g., when template doesn't exist) and fall back to original key
                logger.warning(f"Label-specific prompt '{label_specific_key}' caused SystemExit, " f"falling back to '{key}'")
            except Exception:
                # Log warning and fall back to original key for other exceptions
                logger.warning(f"Failed to render label-specific prompt '{label_specific_key}', " f"falling back to '{key}'")

    # Fall back to original key-based rendering
    template_str = get_prompt_template(key, path=path)
    template = Template(template_str)

    params: Dict[str, Any] = {}
    if data:
        params.update(data)
    params.update(kwargs)

    safe_params = {name: "" if value is None else str(value) for name, value in params.items()}
    try:
        rendered_prompt = template.safe_substitute(safe_params)

        # Load prompts to get header
        prompts = load_prompts(path)
        header = prompts.get("header", "")

        # Prepend header to the rendered prompt
        if header:
            return f"{header.rstrip()}\n\n{rendered_prompt}"
        else:
            return rendered_prompt
    except Exception as exc:  # pragma: no cover - Template handles placeholders gracefully
        logger.error(f"Failed to render prompt '{key}': {exc}")
        raise
