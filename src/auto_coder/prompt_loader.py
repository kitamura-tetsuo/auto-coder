"""Utilities for loading and formatting LLM instruction prompts from YAML files."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

import yaml

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
    """Traverse nested dictionaries using dot-separated keys.

    Supports both flat keys with dots (e.g., "issue.action" as a single key)
    and nested keys (e.g., issue -> action). Flat keys are checked first.
    """
    # First, check if the full key exists as-is (flat key with dots)
    if key in prompts:
        return prompts[key]

    # Otherwise, traverse nested dictionaries using dot-separated path
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
    label_prompt_mappings: Optional[Dict[Any, Any]],
    label_priorities: Optional[List[Any]],
) -> Optional[Any]:
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
        # If it's a list, convert to dict where each element maps to itself
        if isinstance(label_prompt_mappings, list):
            label_prompt_mappings = {item: item for item in label_prompt_mappings}
        else:
            return None

    # Build a mapping from string representation to original mapping key
    # This allows matching non-string types (float, bool, etc.) with string comparisons
    str_to_original_key: Dict[str, Any] = {}
    for mapping_key, prompt_key in label_prompt_mappings.items():
        try:
            str_key = str(mapping_key)
            str_to_original_key[str_key] = mapping_key
        except (AttributeError, TypeError):
            # Skip keys that can't be converted to string
            continue

    # Filter to labels with configured prompt mappings (case-sensitive)
    # We use the string representation for matching but store the original mapping key
    applicable_mapping_keys = []  # List of original mapping keys that match
    for issue_label in issue_labels:
        # Ensure label is a string for comparison
        try:
            issue_label_str = str(issue_label)
        except (AttributeError, TypeError):
            # Skip labels that can't be converted to string
            continue
        # Case-sensitive matching: exact match required
        if issue_label_str in str_to_original_key:
            # Use the original mapping key from the mappings configuration
            original_mapping_key = str_to_original_key[issue_label_str]
            applicable_mapping_keys.append(original_mapping_key)

    if not applicable_mapping_keys:
        return None

    # For backward compatibility: if priorities is None, return first applicable mapping key
    # This allows old code to work with just mappings, without priority system
    if label_priorities is None:
        return applicable_mapping_keys[0] if applicable_mapping_keys else None

    # If priorities is empty list, fallback to first applicable mapping key
    if not label_priorities:
        return applicable_mapping_keys[0] if applicable_mapping_keys else None

    # Sort by priority and return highest priority mapping key
    # Check if any applicable labels match the priorities
    for priority_label in label_priorities:
        try:
            priority_str = str(priority_label)
        except (AttributeError, TypeError):
            # Skip priorities that can't be converted to string
            continue
        # Check if this priority matches any of the applicable mapping keys
        for mapping_key in applicable_mapping_keys:
            mapping_key_str = str(mapping_key)
            if priority_str == mapping_key_str:
                return mapping_key

    # If no exact match found, fall back to first applicable label
    return applicable_mapping_keys[0] if applicable_mapping_keys else None


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

    # For backward compatibility: if label_priorities is None, we can still
    # return a prompt based on mappings (will use first applicable label)
    # No longer require label_priorities to be non-empty

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
    parent_issue_body: Optional[str] = None,
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
        parent_issue_body: Optional parent issue body text for template substitution
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
    if parent_issue_body is not None:
        params["parent_issue_body"] = parent_issue_body
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
